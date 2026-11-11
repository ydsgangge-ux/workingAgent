"""自动抠像引擎 —— 把纯色背景（绿幕/白幕/蓝幕）的视频变成透明 APNG。

和 ffmpeg 的 colorkey 相比，这里做的是"按色度分割"：
  * 在 Lab 空间只比较 a/b 两个色度通道，忽略亮度 —— 所以 backdrop 上的阴影
    （只是变暗、色度不变）会被一起当背景抠掉，不会再留一片绿影。
  * 形态学开/闭 + 填洞：角色内部的同色小洞（比如衣服上的绿色花纹）会被补回来。
  * 连通域：只保留与主体相邻的前景块，右下角孤立的水印文字会被丢掉。
  * 自动阈值：用边缘像素的色度标准差推出来，不需要手调。
  * 自动裁剪：取所有帧前景的并集包围盒。

依赖：opencv-python + numpy（缺依赖时 media_pet 会把"自动抠像"菜单项藏起来）。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

MAX_WORK_SIDE = 400      # 处理分辨率上限（桌宠最多显示 300px，够用）
MAX_FRAMES = 450         # 最多处理这么多帧，防止超长视频卡死


@dataclass
class MatteResult:
    ok: bool
    output: str = ""
    frames: int = 0
    size: tuple[int, int] = (0, 0)
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    message: str = ""


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _no_window() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def probe_video(path: str | Path) -> tuple[int, int, float]:
    import re

    try:
        proc = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, errors="replace", timeout=20, **_no_window(),
        )
        text = proc.stderr or ""
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"无法启动 ffmpeg：{e}") from e

    size = None
    for m in re.finditer(r"(\d{2,5})x(\d{2,5})", text):
        w, h = int(m.group(1)), int(m.group(2))
        if 16 <= w <= 8192 and 16 <= h <= 8192:
            size = (w, h)
            break
    if size is None:
        raise RuntimeError("读不出视频尺寸，可能不是有效视频文件")

    fps = 15.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        try:
            fps = float(m.group(1))
        except ValueError:
            pass
    if not (1.0 <= fps <= 120.0):
        fps = 15.0
    return size[0], size[1], fps


# ──────────────────────────────────────────────────────────
#  解码 / 编码
# ──────────────────────────────────────────────────────────

def _decode_frames(path: str, max_side: int, fps: int,
                   progress: Callable[[int, str], None] | None = None,
                   should_stop: Callable[[], bool] | None = None) -> tuple[list[np.ndarray], int, int]:
    """把视频解码成 BGR 帧列表（已按 max_side 缩小）。"""
    src_w, src_h, _ = probe_video(path)
    out_h = min(max_side, src_h)
    out_w = max(2, int(round(src_w * out_h / src_h / 2)) * 2)

    cmd = [
        ffmpeg_exe(), "-v", "error", "-i", path, "-an",
        "-vf", f"scale=-2:min({max_side}\\,ih),fps={fps}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_no_window())
    frame_bytes = out_w * out_h * 3
    frames: list[np.ndarray] = []
    try:
        while True:
            if should_stop is not None and should_stop():
                break
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w, 3).copy()
            frames.append(frame)
            if len(frames) >= MAX_FRAMES:
                break
            if progress is not None and len(frames) % 5 == 0:
                progress(min(40, 5 + len(frames) // 8), f"解码中… {len(frames)} 帧")
    finally:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    if not frames:
        raise RuntimeError("没有解码出任何画面")
    return frames, out_w, out_h


def _encode_apng(frames, sizes, dst: str, fps: int,
                 progress: Callable[[int, str], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
    w, h = sizes
    cmd = [
        ffmpeg_exe(), "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(fps),
        "-i", "-",
        "-c:v", "apng", "-pix_fmt", "rgba", "-plays", "0", "-compression_level", "9",
        dst,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, **_no_window())
    total = max(1, len(frames))
    try:
        for i, frame in enumerate(frames):
            if should_stop is not None and should_stop():
                break
            proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            if progress is not None and i % 5 == 0:
                progress(60 + int(40 * i / total), f"编码中… {i}/{total}")
        proc.stdin.close()
    finally:
        try:
            rc = proc.wait(timeout=120)
        except Exception:
            proc.kill()
            rc = -1
        if rc != 0 or not Path(dst).exists():
            err = ""
            try:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")[-400:]
            except Exception:
                pass
            raise RuntimeError(f"编码失败（ffmpeg 返回 {rc}）{err}")


# ──────────────────────────────────────────────────────────
#  背景估计 / 分割
# ──────────────────────────────────────────────────────────

def _estimate_background(frames: list[np.ndarray]) -> tuple[np.ndarray, float]:
    """从画面边缘取背景色（Lab），并给出色度的离散度。"""
    samples = []
    picks = [frames[0], frames[len(frames) // 2], frames[-1]]
    for f in picks:
        h, w = f.shape[:2]
        band = max(4, int(min(h, w) * 0.03))
        edges = np.concatenate([
            f[:band].reshape(-1, 3), f[-band:].reshape(-1, 3),
            f[:, :band].reshape(-1, 3), f[:, -band:].reshape(-1, 3),
        ])
        lab = cv2.cvtColor(edges.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
        samples.append(lab)
    all_lab = np.concatenate(samples, axis=0)

    bg = np.median(all_lab, axis=0)
    # 只保留靠近中位数的样本，避免边缘蹭到角色把估计带偏
    dist = np.abs(all_lab[:, 1:] - bg[1:]).max(axis=1)
    keep = all_lab[dist < 25]
    if len(keep) > 50:
        bg = np.median(keep, axis=0)
        all_lab = keep
    sigma = float(np.max(np.std(all_lab[:, 1:], axis=0)))
    return bg, sigma


def _segment(frame: np.ndarray, bg: np.ndarray, thresh: float) -> tuple[np.ndarray, np.ndarray]:
    """返回前景 mask（0/1）。只比 a/b 色度，忽略亮度，阴影也会被归到背景。"""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.int16)
    da = lab[:, :, 1] - int(round(bg[1]))
    db = lab[:, :, 2] - int(round(bg[2]))
    dist = np.sqrt(da.astype(np.float32) ** 2 + db.astype(np.float32) ** 2)

    kernel = np.ones((3, 3), np.uint8)
    # 宽松阈值：把 backdrop 上的阴影（色度略偏、亮度更暗）也纳入"候选背景"
    bg_cand = (dist < thresh * 2.2).astype(np.uint8)
    bg_cand = cv2.morphologyEx(bg_cand, cv2.MORPH_OPEN, kernel)

    fg = (1 - bg_cand).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel, iterations=2)

    # 深色绿影（腿间/桌下那种）：色相仍朝背景的绿、饱和度够、亮度明显更暗
    # → 判成背景。角色的粉色/白色/肤色色相都差得很远，不会被误伤。
    bg_ang = float(np.arctan2(bg[2] - 128.0, bg[1] - 128.0))
    ang = np.arctan2(lab[:, :, 2] - 128.0, lab[:, :, 1] - 128.0)
    chroma = np.sqrt((lab[:, :, 1] - 128.0) ** 2 + (lab[:, :, 2] - 128.0) ** 2)
    dang = np.abs(np.arctan2(np.sin(ang - bg_ang), np.cos(ang - bg_ang)))
    shadow = (dang < 0.40) & (chroma > thresh * 1.2) & (lab[:, :, 0] < bg[0] * 0.92)
    if shadow.any():
        fg[shadow] = 0

    # 补小洞：把面积很小的"背景色小块"判回角色（衣服上的绿纹之类）。
    # 大块背景（两腿之间、臂弯、桌下）本来就是看得见的背景，必须保持透明，
    # 所以这里只按面积判断，绝不能对整片洞做填充。
    total_px = fg.size
    n_bg, bg_labels, bg_stats, _ = cv2.connectedComponentsWithStats(bg_cand, connectivity=4)
    fill = np.zeros_like(fg)
    for i in range(1, n_bg):
        if bg_stats[i, cv2.CC_STAT_AREA] < total_px * 0.0025:
            fill[bg_labels == i] = 1
    if fill.any():
        fg = (fg | fill).astype(np.uint8)

    # 连通域：只留主体以及紧贴主体的块（桌椅、绳子），孤立的水印/噪点丢掉
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if n <= 1:
        soft = np.clip((dist - thresh) / (thresh * 1.1), 0.0, 1.0)
        return fg.astype(np.uint8), soft.astype(np.float32)
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = int(np.argmax(areas)) + 1
    main = (labels == largest).astype(np.uint8)
    near = cv2.dilate(main, np.ones((31, 31), np.uint8)) > 0
    total = fg.size
    keep = main.astype(bool)
    for i in range(1, n):
        if i == largest:
            continue
        comp = labels == i
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < total * 0.0002:          # 再小的直接丢
            continue
        if (comp & near).any():            # 贴着主体的保留
            keep |= comp

    # 软 alpha：按"离背景色有多远"做渐变，越靠近背景越透明。
    # 这样轮廓边缘是平滑过渡，不会出现二值锯齿，也不会留一圈绿边。
    soft = np.clip((dist - thresh) / (thresh * 1.1), 0.0, 1.0)
    return keep.astype(np.uint8), soft.astype(np.float32)


def _despill(frame: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """只在半透明边缘上压制绿色，避免边缘发绿。"""
    edge = (alpha > 0) & (alpha < 250)
    if not edge.any():
        return frame
    b, g, r = cv2.split(frame)
    g_i, r_i, b_i = g.astype(np.int16), r.astype(np.int16), b.astype(np.int16)
    limit = (r_i + b_i) // 2
    fix = edge & (g_i > r_i) & (g_i > b_i) & (g_i > limit)
    if not fix.any():
        return frame
    g2 = np.where(fix, np.minimum(g_i, limit).astype(np.uint8), g)
    return cv2.merge([b, g2, r])


# ──────────────────────────────────────────────────────────
#  主流程
# ──────────────────────────────────────────────────────────

def auto_matte(src: str | Path, dst: str | Path,
               max_side: int = 320, fps: int = 15,
               progress: Callable[[int, str], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> MatteResult:
    """把纯色背景视频抠成透明 APNG。"""
    src, dst = str(src), str(dst)
    if not os.path.isfile(src):
        return MatteResult(False, message="源文件不存在")

    try:
        frames, w, h = _decode_frames(src, max_side, fps, progress, should_stop)
    except Exception as e:  # noqa: BLE001
        return MatteResult(False, message=f"解码失败：{e}")

    if should_stop is not None and should_stop():
        return MatteResult(False, message="已取消")

    bg, sigma = _estimate_background(frames)
    thresh = float(np.clip(sigma * 2.5 + 4.0, 6.0, 22.0))
    if progress is not None:
        progress(45, f"识别背景色：Lab({bg[0]:.0f},{bg[1]:.0f},{bg[2]:.0f}) 阈值 {thresh:.1f}")

    alphas: list[np.ndarray] = []
    x0, y0, x1, y1 = w, h, 0, 0
    for i, frame in enumerate(frames):
        if should_stop is not None and should_stop():
            return MatteResult(False, message="已取消")
        keep, soft = _segment(frame, bg, thresh)
        alpha = (soft * keep * 255.0).astype(np.uint8)
        alpha = cv2.GaussianBlur(alpha, (3, 3), 0)
        alpha[alpha < 60] = 0          # 削掉很淡的绿边雾气
        alphas.append(alpha)
        ys, xs = np.nonzero(alpha > 24)
        if len(xs):
            x0, y0 = min(x0, int(xs.min())), min(y0, int(ys.min()))
            x1, y1 = max(x1, int(xs.max())), max(y1, int(ys.max()))
        if progress is not None and i % 4 == 0:
            progress(45 + int(15 * i / max(1, len(frames))), f"抠像中… {i + 1}/{len(frames)}")

    if x1 <= x0 or y1 <= y0:
        return MatteResult(False, message="没有识别到主体（背景可能不是纯色）")

    # 留一点边距，并对齐到偶数尺寸
    pad_x = max(2, int((x1 - x0) * 0.03))
    pad_y = max(2, int((y1 - y0) * 0.03))
    x0, y0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    x1, y1 = min(w - 1, x1 + pad_x), min(h - 1, y1 + pad_y)
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    if cw % 2:
        cw = min(w - x0, cw + 1)
    if ch % 2:
        ch = min(h - y0, ch + 1)

    out_frames = []
    for frame, alpha in zip(frames, alphas):
        crop = frame[y0:y0 + ch, x0:x0 + cw]
        a = alpha[y0:y0 + ch, x0:x0 + cw]
        crop = _despill(crop, a)
        rgba = cv2.cvtColor(crop, cv2.COLOR_BGR2RGBA)
        rgba[:, :, 3] = a
        out_frames.append(rgba)

    if should_stop is not None and should_stop():
        return MatteResult(False, message="已取消")

    try:
        _encode_apng(out_frames, (cw, ch), dst, fps, progress, should_stop)
    except Exception as e:  # noqa: BLE001
        return MatteResult(False, message=f"编码失败：{e}")

    if progress is not None:
        progress(100, "完成")
    return MatteResult(True, output=dst, frames=len(out_frames), size=(cw, ch),
                       bbox=(x0, y0, cw, ch),
                       message=f"{cw}x{ch}，{len(out_frames)} 帧，背景 Lab({bg[0]:.0f},{bg[1]:.0f},{bg[2]:.0f})")
