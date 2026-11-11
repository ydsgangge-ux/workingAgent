"""
素材驱动桌宠（视频 / 动图）

设计沿用旧项目 vrm_module/media_pet.py：
  - 按"情绪动作"分目录放素材，丢文件进去即可生效
  - 无边框透明置顶窗，拖动移动，点击戳一下
  - Ctrl+滚轮缩放，右键菜单切换表情 / 打开素材目录 / 恢复自带
  - 位置与大小写入配置，下次启动还原
  - 情绪由 A 层结果驱动：set_emotion(emotion, intensity, ...)
  - 空闲 180 秒自动进入 sleepy，情绪动作播完自动回 idle
  - 右键「一键抠视频」：纯色背景素材自动抠成透明 APNG（pet_matte）
  - 帧率按素材走（最高 24fps），循环在解码线程内部完成，不闪烁

素材目录：<数据根>/pet/<动作>/xxx.webm|gif|png
自带素材：vrm_module/static/pet_assets/**，首次运行自动复制到数据根

为什么用 imageio_ffmpeg 而不是 ffmpeg 命令行：
  VP9 的 alpha 通道必须显式指定解码器 -c:v libvpx-vp9，
  否则 ffmpeg 自动选解码器会丢 alpha，画面变黑底。
"""

import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QGuiApplication, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QWidget

try:
    from desktop.config import DATA_ROOT, load_config, save_config
except Exception:  # 脱离主程序单独调试时兜底
    DATA_ROOT = Path.home() / ".working_agent"
    def load_config():
        return {}
    def save_config(cfg):
        pass


# ── 参数 ──────────────────────────────────────────
MAX_SIDE = 512              # 解码后最长边（512 源素材可 1:1 保留细节，再由绘制端一次缩放）
MAX_FPS = 24                # 播放帧率上限
IDLE_SLEEP_SECONDS = 180    # 空闲多久进入 sleepy
REACTION_MS = 30000         # 情绪动作最长停留，到点回 idle

SIZES = [120, 160, 220, 300]
DEFAULT_SIZE = 220

LOOP_ACTIONS = ("idle", "thinking", "speaking", "sleepy")
ACTIONS = ("idle", "happy", "sad", "angry", "surprised", "thinking", "speaking", "sleepy")
MEDIA_EXTS = (
    ".webm", ".mp4", ".mov", ".mkv", ".avi", ".m4v",
    ".gif", ".apng", ".webp", ".png", ".jpg", ".jpeg",
)
# 自带透明通道、或本身就是动图的格式，优先播放（抠像产物 apng 会排在最前）
ALPHA_EXTS = {".apng", ".webm", ".mov", ".gif", ".png", ".webp"}
# 只有这些是真静态图；gif/apng/webp 交给 ffmpeg 解码，否则只会显示第一帧
STILL_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
# 能用自动抠像处理的"实心背景"素材（webm/mov/gif 通常自带透明通道，不参与抠像）
VIDEO_EXTS = (".mp4", ".avi", ".m4v", ".mkv")

# 情绪 → 动作目录
EMOTION_TO_ACTION = {
    "joy": "happy", "happy": "happy", "trust": "happy",
    "sadness": "sad", "sad": "sad", "disgust": "sad", "nostalgic": "sad",
    "anger": "angry", "angry": "angry",
    "fear": "surprised", "surprise": "surprised", "surprised": "surprised",
    "anticipation": "thinking", "curious": "thinking", "thinking": "thinking",
    "sleepy": "sleepy", "tired": "sleepy",
    "neutral": "idle",
}

ACTION_LABELS = {
    "idle": "待机", "happy": "开心", "sad": "低落", "angry": "生气",
    "surprised": "惊讶", "thinking": "思考", "speaking": "说话",
    "sleepy": "困了",
}


# ══════════════════════════════════════════════════
# 素材管理
# ══════════════════════════════════════════════════

class PetAssets:
    """素材目录：用户可自由增删，自带素材作为兜底"""

    def __init__(self, root: Optional[Path] = None):
        self.bundled = Path(__file__).resolve().parent / "static" / "pet_assets"
        self.root = Path(root) if root else (DATA_ROOT / "pet")
        self.ensure()

    def ensure(self):
        """首次运行复制自带素材，并保证所有动作文件夹都存在"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            has_file = any(p.is_file() for p in self.root.rglob("*"))
            if not has_file and self.bundled.exists():
                shutil.copytree(self.bundled, self.root, dirs_exist_ok=True)
        except Exception as e:
            print(f"[Pet] 初始化素材目录失败: {e}", flush=True)
        self._ensure_dirs()

    def _ensure_dirs(self):
        """把 8 个动作文件夹全部建好，用户打开目录就知道该放哪"""
        for act in ACTIONS:
            try:
                (self.root / act).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"[Pet] 创建素材目录 {act} 失败: {e}", flush=True)
        readme = self.root / "说明.txt"
        if readme.exists():
            return
        try:
            readme.write_text(
                "桌宠素材目录（每个情绪一个文件夹，丢文件进去即可）\n"
                "\n"
                "idle\\       待机（必须有，其他动作没素材时会回落到它）\n"
                "happy\\      开心\n"
                "sad\\        低落\n"
                "angry\\      生气\n"
                "surprised\\  惊讶\n"
                "thinking\\   思考\n"
                "speaking\\   说话\n"
                "sleepy\\     困了\n"
                "\n"
                "支持格式：webm / apng / gif / png / jpg / mp4 / mov / mkv / avi / m4v\n"
                "  - 想只看到角色本身：用带透明通道的 webm（VP9+alpha）或 apng/png\n"
                "  - 实心背景的 mp4/avi/mkv：右键桌宠 →「✨ 一键抠视频」，\n"
                "    会自动生成 <动作>_01.apng，原视频改名 .source 留档\n"
                "  - 同一动作可放多个文件，会轮换播放；文件名建议 idle_01.webm 这种\n"
                "\n"
                "添加素材后，右键桌宠重新选一次动作即可生效。\n",
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[Pet] 写入素材说明失败: {e}", flush=True)

    def files(self, action: str) -> List[Path]:
        d = self.root / action
        if not d.is_dir():
            return []
        try:
            items = [
                p for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in MEDIA_EXTS
            ]
            # 透明/动图格式优先，避免抠好的 apng 被同目录的实心视频盖掉
            return sorted(
                items,
                key=lambda p: (0 if p.suffix.lower() in ALPHA_EXTS else 1, p.name.lower()),
            )
        except Exception:
            return []

    def actions(self) -> Dict[str, int]:
        """各动作可用素材数量"""
        out = {}
        try:
            for d in self.root.iterdir():
                if d.is_dir():
                    n = len(self.files(d.name))
                    if n:
                        out[d.name] = n
        except Exception:
            pass
        return out

    def open_folder(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root)))
        except Exception as e:
            print(f"[Pet] 打开素材目录失败: {e}", flush=True)

    def restore_bundled(self):
        """把自带素材补回去（不删除用户新增的）"""
        try:
            if self.bundled.exists():
                shutil.copytree(self.bundled, self.root, dirs_exist_ok=True)
        except Exception as e:
            print(f"[Pet] 恢复自带素材失败: {e}", flush=True)
        self._ensure_dirs()


# ══════════════════════════════════════════════════
# 边缘抗锯齿
# ══════════════════════════════════════════════════

_NP_CACHE = None


def _numpy():
    global _NP_CACHE
    if _NP_CACHE is None:
        try:
            import numpy as _np
            _NP_CACHE = _np
        except Exception:
            _NP_CACHE = False
    return _NP_CACHE or None


def feather_alpha(img: QImage, passes: int = 2) -> QImage:
    """只对 alpha 做 2 像素左右的高斯羽化，消除抠像硬边的台阶。

    颜色不参与模糊，避免边缘泛出一圈背景色；没装 numpy 时原样返回。
    """
    np = _numpy()
    if np is None:
        return img
    w, h = img.width(), img.height()
    if w < 8 or h < 8:
        return img

    try:
        img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        stride = img.bytesPerLine()
        raw = img.constBits().asstring(img.sizeInBytes())
        if not raw:
            return img
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size < stride * h:
            return img
        arr = arr.reshape(h, stride // 4, 4)[:, :w, :]

        alpha = arr[:, :, 3].astype(np.float32)

        def _blur1(x):
            """先横后竖各做一次 [1,2,1]/4，等效 3x3 高斯"""
            p = np.pad(x, ((1, 1), (0, 0)), mode="edge")
            x = (p[:-2, :] + 2.0 * p[1:-1, :] + p[2:, :]) * 0.25
            p = np.pad(x, ((0, 0), (1, 1)), mode="edge")
            return (p[:, :-2] + 2.0 * p[:, 1:-1] + p[:, 2:]) * 0.25

        for _ in range(max(1, passes)):
            alpha = _blur1(alpha)

        out = arr.copy()
        out[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
        return QImage(out.tobytes(), w, h, w * 4,
                      QImage.Format.Format_RGBA8888).copy()
    except Exception:
        # 羽化只是画质优化，失败就用原图
        return img


# ══════════════════════════════════════════════════
# 解码线程
# ══════════════════════════════════════════════════

class FrameThread(QThread):
    """在后台把视频解成 RGBA 帧，通过信号送到界面线程。

    节奏按素材帧率走（不再能解多快就播多快），并且循环在解码线程内部完成：
    播到结尾直接重新开一个解码器，画面不会回到空白，避免循环时闪一下。
    """

    frame = pyqtSignal(QImage)
    failed = pyqtSignal(str)
    loaded = pyqtSignal(int)

    def __init__(self, path: Path, loop: bool = True, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self._loop = loop
        self._stop = threading.Event()
        self._ack = threading.Event()
        self._fps = MAX_FPS
        self.frame_size = QSize(0, 0)
        self.frame_count = 0

    def ack(self):
        self._ack.set()

    def stop(self):
        self._stop.set()
        self._ack.set()

    # ── 静态图：直接加载一帧 ──
    def _load_still(self) -> bool:
        if self.path.suffix.lower() not in STILL_EXTS:
            return False
        img = QImage(str(self.path))
        if img.isNull():
            return False
        if max(img.width(), img.height()) > MAX_SIDE:
            img = img.scaled(
                MAX_SIDE, MAX_SIDE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        img = feather_alpha(img)
        self.frame_size = QSize(img.width(), img.height())
        self.frame_count = 1
        self.frame.emit(img)
        self.loaded.emit(1)
        return True

    def run(self):
        try:
            if self._load_still():
                return
            self._decode_loop()
        except Exception as e:
            self.failed.emit(str(e))

    def _decode_loop(self):
        import imageio_ffmpeg as iff

        params = {}
        # VP9/WebM 必须显式指定解码器，否则透明通道会丢
        if self.path.suffix.lower() == ".webm":
            params["input_params"] = ["-c:v", "libvpx-vp9"]
        # 和旧项目一致：超过 24fps 的素材重采样到 24，保持真实时长
        params["output_params"] = ["-r", str(MAX_FPS)]

        total = 0
        while not self._stop.is_set():
            gen = iff.read_frames(
                str(self.path), pix_fmt="rgba", bits_per_pixel=32, **params
            )
            try:
                played = self._play_once(gen)
            finally:
                try:
                    gen.close()
                except Exception:
                    pass
            total += played
            # 一次都没播出来（解码失败）或一次性动作：结束
            if self._stop.is_set() or not self._loop or played <= 1:
                break
            # 循环之间稍作喘息，避免 CPU 空转
            self.msleep(40)
        self.frame_count = total
        self.loaded.emit(total)

    def _play_once(self, gen) -> int:
        """按素材帧率播一遍，返回实际播出的帧数。"""
        meta = next(gen)
        src_w, src_h = meta.get("source_size") or meta.get("size") or (0, 0)
        if not src_w or not src_h:
            self.failed.emit("无法读取视频尺寸")
            return 0

        scale = min(1.0, MAX_SIDE / max(src_w, src_h))
        out_w, out_h = int(src_w * scale), int(src_h * scale)
        self.frame_size = QSize(out_w, out_h)

        fps = float(meta.get("fps") or MAX_FPS)
        self._fps = max(1.0, min(float(MAX_FPS), fps))
        interval = 1.0 / self._fps

        count = 0
        next_at = time.perf_counter()
        for raw in gen:
            if self._stop.is_set():
                break
            img = QImage(raw, src_w, src_h, src_w * 4,
                         QImage.Format.Format_RGBA8888).copy()
            if scale < 1.0:
                img = img.scaled(
                    out_w, out_h,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            # 抠像素材的边缘通常是硬切，羽化 2px 后缩小显示就不会有台阶感
            img = feather_alpha(img)
            count += 1
            self._ack.clear()
            self.frame.emit(img)
            # 等界面画完再取下一帧，避免事件队列堆积
            self._ack.wait(timeout=0.25)
            if self._stop.is_set():
                break
            # 关键：按真实帧率节流，否则会"能解多快就播多快"
            next_at += interval
            wait = next_at - time.perf_counter()
            if wait > 0:
                self.usleep(int(wait * 1_000_000))
            else:
                next_at = time.perf_counter()
        return count


# ══════════════════════════════════════════════════
# 一键抠视频（纯色背景 → 透明 APNG）
# ══════════════════════════════════════════════════

def load_matte():
    """找 pet_matte 模块；没装 opencv / numpy 时返回 None。"""
    try:
        from . import pet_matte
        return pet_matte
    except Exception:
        pass
    try:
        import pet_matte  # type: ignore[import-not-found]
        return pet_matte
    except Exception:
        return None


def matte_available() -> bool:
    """自动抠像依赖 cv2 / numpy，缺了就把菜单项藏起来。"""
    return load_matte() is not None


class MatteThread(QThread):
    """后台批量抠像：把各动作文件夹里的实心背景视频转成透明 APNG。

    成功后把原视频改名为 xxx.mp4.source —— 后缀不在 MEDIA_EXTS 里，
    桌宠不会再去播放那块不透明矩形，但用户随时可以改回来。
    """

    progress = pyqtSignal(int, str)
    done = pyqtSignal(int, int)

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self._root = Path(root)
        self._stop = False
        self.sources_done: List[Path] = []   # 抠好的原视频，交给界面线程再改名

    def stop(self):
        self._stop = True

    def _jobs(self):
        jobs = []
        for action in ACTIONS:
            folder = self._root / action
            if not folder.is_dir():
                continue
            try:
                items = sorted(folder.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for p in items:
                if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                    continue
                # 同一目录里已有比它新的 apng，说明已经抠过，跳过避免重复生成
                if not self._needs_matte(folder, p):
                    continue
                jobs.append((action, p))
        return jobs

    @staticmethod
    def _needs_matte(folder: Path, video: Path) -> bool:
        try:
            newest_apng = max(
                (q.stat().st_mtime for q in folder.glob("*.apng")), default=0.0
            )
            return video.stat().st_mtime > newest_apng
        except OSError:
            return False

    @staticmethod
    def _target(action: str, src: Path) -> Path:
        """输出名沿用 idle_01.apng 这种格式，已存在就往后顺延。"""
        used = set()
        try:
            used = {p.name.lower() for p in src.parent.iterdir()}
        except OSError:
            pass
        n = 1
        while f"{action}_{n:02d}.apng" in used:
            n += 1
        return src.parent / f"{action}_{n:02d}.apng"

    def run(self):
        jobs = self._jobs()
        if not jobs:
            self.done.emit(0, 0)
            return

        matte = load_matte()
        if matte is None:
            self.progress.emit(0, "缺少抠像依赖（需要 opencv-python 和 numpy）")
            self.done.emit(0, 0)
            return

        ok = 0
        for idx, (action, src) in enumerate(jobs):
            if self._stop:
                break
            base = int(idx * 100 / len(jobs))
            span = max(1, int(100 / len(jobs)))

            def report(pct: int, msg: str, _b=base, _s=span, _n=src.name):
                self.progress.emit(min(99, _b + int(pct * _s / 100)), f"{_n} · {msg}")

            result = matte.auto_matte(
                src, self._target(action, src),
                max_side=480,   # 比 320 更清晰，缩小到 220/300 显示时边缘更细
                progress=report, should_stop=lambda: self._stop,
            )
            if not result.ok:
                if self._stop:
                    break
                self.progress.emit(min(99, base + span), f"{src.name} 抠像失败：{result.message}")
                continue

            ok += 1
            # 改名交给界面线程（此刻解码线程还占着这个文件）
            self.sources_done.append(src)
            self.progress.emit(min(99, base + span), f"完成 {src.name} → {Path(result.output).name}")
        self.done.emit(ok, len(jobs))


# ══════════════════════════════════════════════════
# 桌宠窗口
# ══════════════════════════════════════════════════

class MediaPet(QWidget):
    """无边框透明置顶的素材桌宠"""

    closed = pyqtSignal()   # 用户关掉浮窗（右键菜单 / 关闭按钮），供主界面同步按钮状态

    def __init__(self, parent=None, assets_root: Optional[Path] = None):
        super().__init__(parent)

        self.assets = PetAssets(assets_root)
        self.cfg = load_config()

        self._size = int(self.cfg.get("pet_size", DEFAULT_SIZE) or DEFAULT_SIZE)
        self._size = self._size if self._size in SIZES else DEFAULT_SIZE

        self._thread: Optional[FrameThread] = None
        self._pixmap: Optional[QPixmap] = None
        self._action = ""
        self._looping = True
        self._dragging = False
        self._drag_offset = QPoint()
        self._moved = False
        self._matte_thread: Optional[MatteThread] = None
        self._matte_note = ""     # 抠像进度提示，画在画面底部

        # 情绪动作停留计时 / 空闲进入 sleepy
        self._revert_timer = QTimer(self)
        self._revert_timer.setSingleShot(True)
        self._revert_timer.timeout.connect(lambda: self.play("idle", loop=True))

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._on_idle_timeout)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_geometry)

        self._setup_window()
        self.play("idle", loop=True)

    # ── 窗口属性 ──
    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setWindowTitle("桌宠")
        self.resize(self._size, self._size)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self._restore_position()
        self._idle_timer.start(IDLE_SLEEP_SECONDS * 1000)

    def _restore_position(self):
        x = self.cfg.get("pet_pos_x")
        y = self.cfg.get("pet_pos_y")
        if isinstance(x, int) and isinstance(y, int):
            self.move(x, y)
            return
        # 默认右下角，留 40px 边距
        screen = QGuiApplication.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            self.move(g.right() - self._size - 40,
                      g.bottom() - self._size - 40)

    def _save_geometry(self):
        try:
            cfg = load_config()
            cfg["pet_pos_x"] = self.x()
            cfg["pet_pos_y"] = self.y()
            cfg["pet_size"] = self._size
            save_config(cfg)
        except Exception:
            pass

    # ── 播放控制 ──
    def play(self, action: str, loop: Optional[bool] = None):
        """切换到某个动作；没有该动作素材时回落到 idle"""
        if loop is None:
            loop = action in LOOP_ACTIONS
        if action == self._action and self._thread and self._thread.isRunning():
            return

        files = self.assets.files(action)
        if not files and action != "idle":
            # 没有该动作素材时回落到 idle
            files = self.assets.files("idle")
            action = "idle"
        if not files:
            self._action = action
            self._looping = loop
            self._pixmap = None
            self.update()
            return

        # 同一动作多份素材时轮换，避免每次都是同一段
        self._play_index = getattr(self, "_play_index", -1) + 1
        path = files[self._play_index % len(files)]

        self._stop_thread()
        self._action = action
        self._looping = loop
        self._pixmap = None

        t = FrameThread(path, loop=loop, parent=self)
        t.frame.connect(self._on_frame)
        t.failed.connect(lambda msg: print(f"[Pet] 解码失败 {path.name}: {msg}", flush=True))
        t.finished.connect(lambda: self._on_thread_finished(t))
        self._thread = t
        t.start()
        self.update()

    def _stop_thread(self):
        t = self._thread
        self._thread = None
        if t is not None:
            try:
                t.stop()
                t.wait(1500)
            except Exception:
                pass

    def _on_thread_finished(self, t: FrameThread):
        # 循环在解码线程内部完成；这里只处理一次性动作播完回 idle
        if t is not self._thread:
            return
        if not self._looping:
            QTimer.singleShot(10, lambda: self.play("idle", loop=True))

    def _on_frame(self, img: QImage):
        self._pixmap = QPixmap.fromImage(img)
        self.update()
        t = self._thread
        if t is not None:
            t.ack()

    def _on_idle_timeout(self):
        if self._action == "idle":
            self.play("sleepy", loop=True)

    # ── 绘制 ──
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # 脚底对齐：贴住底边、水平居中
            x = (self.width() - scaled.width()) // 2
            y = self.height() - scaled.height()
            p.drawPixmap(x, y, scaled)
        if not self._pixmap or self._pixmap.isNull():
            # 占位：告诉用户素材目录在哪
            p.setPen(Qt.GlobalColor.gray)
            p.drawText(
                self.rect().adjusted(8, 8, -8, -8),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                f"暂无「{ACTION_LABELS.get(self._action, self._action)}」素材\n"
                f"右键 → 打开素材目录\n\n{self.assets.root}",
            )
        # 抠像进度条（底部一条半透明提示）
        if self._matte_note:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            f = p.font()
            f.setPointSize(8)
            p.setFont(f)
            box = QRect(6, self.height() - 32, self.width() - 12, 24)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawRoundedRect(box, 6, 6)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self._matte_note)
        p.end()
        t = self._thread
        if t is not None:
            t.ack()

    # ── 交互 ──
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._moved = False
            self._drag_offset = e.globalPosition().toPoint() - self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()

    def mouseMoveEvent(self, e):
        if self._dragging:
            new_pos = e.globalPosition().toPoint() - self._drag_offset
            if (new_pos - self.pos()).manhattanLength() > 3:
                self._moved = True
            self.move(new_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if self._moved:
                self._save_timer.start(400)   # 拖动结束稍后落盘
            else:
                self._poke()
            e.accept()

    def _poke(self):
        """点一下：随机做一个表情反应"""
        for act in ("surprised", "happy", "angry"):
            if self.assets.files(act):
                self.play(act, loop=False)
                self._revert_timer.start(REACTION_MS)
                return

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            idx = SIZES.index(self._size) if self._size in SIZES else 2
            idx = min(len(SIZES) - 1, idx + (1 if e.angleDelta().y() > 0 else -1))
            self._size = SIZES[idx]
            self.resize(self._size, self._size)
            self._save_timer.start(400)
            e.accept()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        counts = self.assets.actions()

        for act in ("idle", "happy", "sad", "angry", "surprised",
                    "thinking", "speaking", "sleepy"):
            n = counts.get(act, 0)
            label = f"{ACTION_LABELS.get(act, act)}"
            if n:
                label += f"（{n}）"
            a = QAction(label, self)
            a.setEnabled(n > 0)
            a.triggered.connect(lambda _=False, x=act: self._menu_play(x))
            menu.addAction(a)

        menu.addSeparator()
        menu.addAction("📂 打开素材目录", self.assets.open_folder)
        menu.addAction("♻️ 恢复自带素材", self._restore)
        if matte_available():
            act_matte = QAction("✨ 一键抠视频（纯色背景 → 透明）", self)
            act_matte.setEnabled(self._has_pending_video() or self._matte_thread is not None)
            act_matte.triggered.connect(self._start_matte)
            menu.addAction(act_matte)

        size_menu = menu.addMenu("📐 大小")
        for s in SIZES:
            a = QAction(f"{s}px", self)
            a.setCheckable(True)
            a.setChecked(s == self._size)
            a.triggered.connect(lambda _=False, v=s: self._set_size(v))
            size_menu.addAction(a)

        menu.addSeparator()
        menu.addAction("关闭桌宠", self.close)
        menu.exec(e.globalPos())

    def _menu_play(self, action: str):
        if action in LOOP_ACTIONS:
            self.play(action, loop=True)
            self._revert_timer.stop()
        else:
            self.play(action, loop=False)
            self._revert_timer.start(REACTION_MS)

    def _restore(self):
        self.assets.restore_bundled()
        self.play("idle", loop=True)

    def _set_size(self, size: int):
        self._size = size
        self.resize(size, size)
        self._save_timer.start(400)

    # ── 一键抠视频 ──
    def _has_pending_video(self) -> bool:
        """素材目录里是否还有没抠过的实心背景视频"""
        for act in ACTIONS:
            folder = self.assets.root / act
            if not folder.is_dir():
                continue
            try:
                for p in folder.iterdir():
                    if (p.is_file() and p.suffix.lower() in VIDEO_EXTS
                            and MatteThread._needs_matte(folder, p)):
                        return True
            except OSError:
                continue
        return False

    def _start_matte(self):
        if self._matte_thread is not None:
            return
        if not self._has_pending_video():
            self._flash_note("素材目录里没有需要抠像的视频")
            return
        t = MatteThread(self.assets.root, self)
        t.progress.connect(self._on_matte_progress)
        t.done.connect(self._on_matte_finished)
        self._matte_thread = t
        self._matte_note = "开始自动抠像…"
        self.update()
        t.start()

    def _on_matte_progress(self, pct: int, msg: str):
        self._matte_note = f"抠像 {pct}% · {msg}"
        self.update()

    def _on_matte_finished(self, ok: int, total: int):
        t = self._matte_thread
        self._matte_thread = None
        if not self.isVisible():
            self._matte_note = ""
            return
        # 先停当前解码，释放原视频句柄，再改名留档，否则 Windows 会报占用
        self._stop_thread()
        failed = 0
        for src in list(getattr(t, "sources_done", []) or []):
            if not self._backup_source(src):
                failed += 1
        msg = f"抠像完成：成功 {ok} / 共 {total}" if total else "没有需要抠像的视频"
        if failed:
            msg += f"（{failed} 个原视频改名失败）"
        self._matte_note = msg
        self.update()
        QTimer.singleShot(4000, self._clear_matte_note)
        # 重新加载当前动作，让新生成的透明素材立即生效
        action, loop = self._action or "idle", self._looping
        self._action = ""
        self.play(action, loop=loop)

    @staticmethod
    def _backup_source(src: Path) -> bool:
        dst = src.with_name(src.name + ".source")
        if dst.exists():
            return True
        for _ in range(10):
            try:
                src.rename(dst)
                return True
            except OSError:
                time.sleep(0.15)
        return False

    def _clear_matte_note(self):
        self._matte_note = ""
        self.update()

    def _flash_note(self, text: str, ms: int = 3000):
        self._matte_note = text
        self.update()
        QTimer.singleShot(ms, self._clear_matte_note)

    # ── 对外 API（与 A 层结果对接）──
    def set_emotion(self, emotion: str, intensity: float = 1.0,
                    secondary: Optional[str] = None,
                    valence: float = 0.0):
        """情绪驱动：强度太低时保持当前状态，避免频繁切换"""
        self._idle_timer.start(IDLE_SLEEP_SECONDS * 1000)
        emo = (emotion or "neutral").lower()
        action = EMOTION_TO_ACTION.get(emo)
        if not action:
            action = EMOTION_TO_ACTION.get((secondary or "").lower(), "idle")

        # 低强度不打断当前表情
        if float(intensity or 0) < 0.25 and self._action not in ("idle", "sleepy"):
            return

        # 记住最近一次情绪，TTS 说话结束后恢复
        self._last_emotion = (emo, intensity, secondary, valence)

        if action == "idle":
            self.play("idle", loop=True)
            self._revert_timer.stop()
            return

        self.play(action, loop=False)
        self._revert_timer.start(REACTION_MS)

    def set_speaking(self, speaking: bool):
        self._idle_timer.start(IDLE_SLEEP_SECONDS * 1000)
        if speaking:
            self.play("speaking", loop=True)
            self._revert_timer.stop()
        else:
            self.play("idle", loop=True)

    # 与 Live2D 版本保持同名，方便上层统一调用
    def start_lip_sync(self):
        self.set_speaking(True)

    def stop_lip_sync(self):
        self.set_speaking(False)
        # 说话结束后把刚才的情绪动作补回来
        last = getattr(self, "_last_emotion", None)
        if last:
            self.set_emotion(*last)

    def set_state(self, state: str):
        self.play(state, loop=state in LOOP_ACTIONS)

    # ── 生命周期 ──
    def closeEvent(self, e):
        try:
            self._save_geometry()
        except Exception:
            pass
        if self._matte_thread is not None:
            try:
                self._matte_thread.stop()
                self._matte_thread.wait(1000)
            except Exception:
                pass
            self._matte_thread = None
        self._stop_thread()
        super().closeEvent(e)
        try:
            self.closed.emit()
        except Exception:
            pass
