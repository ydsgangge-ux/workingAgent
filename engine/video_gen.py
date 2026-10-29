"""
视频生成模块
============
多后端文生视频：
  1. 智谱 CogVideoX-Flash（免费，默认首选）
  2. 智谱 CogVideoX-3
  3. MiniMax 海螺
  4. 可灵 Kling
  5. Runway
  6. Luma Dream Machine
  7. 阿里通义万相（DashScope）
  8. OpenAI 兼容视频接口（自定义 base_url）

配置键（config.json，可在 18765 管理后台或桌面端设置里改）：
  video_model      模型/后端，默认 cogvideox-flash
  video_api_key    专用 API Key（可留空继承主 LLM / 智谱 key）
  video_base_url   自定义接口地址（仅 openai_compat 使用）
"""

import os
import json
import time
import uuid
import base64
import hashlib
import hmac
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Tuple


# 后端清单（设置页下拉框使用）
VIDEO_PROVIDERS = {
    "cogvideox-flash": "智谱 CogVideoX-Flash（免费）",
    "cogvideox-3": "智谱 CogVideoX-3（付费）",
    "minimax": "MiniMax 海螺",
    "kling": "可灵 Kling",
    "runway": "Runway Gen-4",
    "luma": "Luma Dream Machine",
    "wanx": "阿里通义万相",
    "openai_compat": "OpenAI 兼容视频接口",
}

DEFAULT_MODEL = "cogvideox-flash"
GLM_BASE = "https://open.bigmodel.cn/api/paas/v4"
POLL_TIMEOUT = 900      # 最长等待 15 分钟（免费队列可能较慢）
POLL_INTERVAL = 6


def get_video_dir() -> Path:
    """视频输出目录：项目根/videos"""
    d = Path(__file__).parent.parent / "videos"
    try:
        d.mkdir(parents=True, exist_ok=True)
        _t = d / ".write_test"
        _t.write_text("ok")
        _t.unlink()
        return d
    except Exception:
        root = (Path(os.environ.get("APPDATA", str(Path.home()))) / "workingAgent"
                if os.name == "nt" else Path.home() / ".workingagent")
        d = root / "videos"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _load_cfg() -> Dict:
    try:
        from desktop.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _get_key(cfg: Dict, provider: str) -> str:
    """按优先级取视频 API Key"""
    key = cfg.get("video_api_key", "") or ""
    if key:
        return key
    if provider in ("cogvideox-flash", "cogvideox-3"):
        env = os.environ.get("ZHIPU_API_KEY", "")
        if env:
            return env
        if cfg.get("api_provider") == "zhipu" and cfg.get("api_key"):
            return cfg["api_key"]
        if cfg.get("vision_provider") == "zhipu" and cfg.get("vision_api_key"):
            return cfg["vision_api_key"]
    return cfg.get("api_key", "") or ""


def _request_json(req: urllib.request.Request, timeout: int = 60) -> Dict:
    """带 429/5xx 退避重试的 JSON 请求（免费模型高峰期经常 429）"""
    last = ""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            last = f"HTTP {e.code}: {body}"
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 5 * (attempt + 1)
                print(f"[视频生成] 接口繁忙（HTTP {e.code}），{wait}s 后重试")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError(last or "请求失败")


def _post_json(url: str, payload: Dict, headers: Dict, timeout: int = 60) -> Dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **headers})
    return _request_json(req, timeout=timeout)


def _get_json(url: str, headers: Dict, timeout: int = 60) -> Dict:
    req = urllib.request.Request(url, headers=headers)
    return _request_json(req, timeout=timeout)


def _download(url: str, model: str) -> Optional[str]:
    if not url:
        return None
    path = get_video_dir() / (
        f"{model}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.mp4")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
        if len(data) < 10000:
            print(f"[视频生成] 下载内容过小: {len(data)} bytes")
            return None
        path.write_bytes(data)
        print(f"[视频生成] 已保存: {path} ({len(data)//1024}KB)")
        return str(path)
    except Exception as e:
        print(f"[视频生成] 下载失败: {e}")
        return None


def _wait_url(check, timeout: int = POLL_TIMEOUT, interval: int = POLL_INTERVAL) -> Optional[str]:
    """轮询直到 check() 返回视频 URL；超时或失败返回 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = check()
            if url:
                return url
        except Exception as e:
            print(f"[视频生成] 轮询异常: {e}")
        time.sleep(interval)
    print("[视频生成] 轮询超时")
    return None


# ── 各后端实现 ──────────────────────────────────────────────

def _gen_glm(prompt: str, model: str, size: str, duration: int,
             image_url: str, key: str) -> Optional[str]:
    payload = {"model": model, "prompt": prompt}
    if size:
        payload["size"] = size
    if image_url:
        payload["image_url"] = image_url
    if not key:
        print("[视频生成] 缺少智谱 API Key")
        return None
    headers = {"Authorization": f"Bearer {key}"}
    result = _post_json(f"{GLM_BASE}/videos/generations", payload, headers)
    task_id = result.get("id") or (result.get("data") or {}).get("id")
    if not task_id:
        print(f"[视频生成] 智谱未返回任务 ID: {str(result)[:200]}")
        return None

    def check():
        r = _get_json(f"{GLM_BASE}/async-result/{task_id}", headers)
        status = str(r.get("task_status", "")).upper()
        if status in ("SUCCESS", "SUCCEEDED"):
            videos = r.get("video_result") or (r.get("data") or {}).get("video_result") or []
            if videos:
                return videos[0].get("url") or videos[0].get("cover_url")
            return r.get("url")
        if status in ("FAIL", "FAILED", "ERROR"):
            print(f"[视频生成] 智谱任务失败: {str(r)[:200]}")
            return ""
        return None

    return _wait_url(check)


def _gen_minimax(prompt: str, key: str, duration: int) -> Optional[str]:
    if not key:
        print("[视频生成] 缺少 MiniMax API Key")
        return None
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": "MiniMax-Hailuo-02",
        "prompt": prompt,
        "duration": 6 if duration <= 6 else 10,
        "resolution": "1080P",
    }
    r = _post_json("https://api.minimax.chat/v1/video_generation", payload, headers)
    task_id = r.get("task_id")
    if not task_id:
        print(f"[视频生成] MiniMax 未返回 task_id: {str(r)[:200]}")
        return None

    def check():
        q = _get_json(
            f"https://api.minimax.chat/v1/query/video_generation?task_id={task_id}", headers)
        if str(q.get("status", "")).lower() == "success":
            file_id = q.get("file_id")
            f = _get_json(
                f"https://api.minimax.chat/v1/files/retrieve?file_id={file_id}", headers)
            return (f.get("file") or {}).get("download_url")
        if str(q.get("status", "")).lower() == "fail":
            print(f"[视频生成] MiniMax 任务失败: {str(q)[:200]}")
            return ""
        return None

    return _wait_url(check)


def _kling_jwt(ak: str, sk: str) -> str:
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iss": ak, "exp": now + 1800, "nbf": now - 5}).encode())
    sig = b64(hmac.new(sk.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _gen_kling(prompt: str, key: str, duration: int, size: str) -> Optional[str]:
    # video_api_key 格式：ak|sk 或 ak:sk
    sep = "|" if "|" in key else ":"
    parts = key.split(sep, 1) if key else []
    if len(parts) != 2:
        print("[视频生成] 可灵需要在 video_api_key 中配置 ak|sk")
        return None
    token = _kling_jwt(parts[0].strip(), parts[1].strip())
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "model_name": "kling-v2-1",
        "prompt": prompt,
        "duration": "5" if duration <= 5 else "10",
        "aspect_ratio": "16:9",
    }
    r = _post_json("https://api-beijing.klingai.com/v1/videos/text2video", payload, headers)
    task_id = (r.get("data") or {}).get("task_id")
    if not task_id:
        print(f"[视频生成] 可灵未返回 task_id: {str(r)[:200]}")
        return None

    def check():
        q = _get_json(
            f"https://api-beijing.klingai.com/v1/videos/text2video/{task_id}", headers)
        data = q.get("data") or {}
        status = str(data.get("task_status", "")).lower()
        if status == "succeed":
            videos = (data.get("task_result") or {}).get("videos") or []
            return videos[0].get("url") if videos else None
        if status == "failed":
            print(f"[视频生成] 可灵任务失败: {str(q)[:200]}")
            return ""
        return None

    return _wait_url(check)


def _gen_runway(prompt: str, key: str, duration: int, size: str) -> Optional[str]:
    if not key:
        print("[视频生成] 缺少 Runway API Key")
        return None
    w, h = (size or "1280x720").replace("*", "x").split("x")[:2]
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Runway-Version": "2024-11-06",
    }
    payload = {
        "promptText": prompt,
        "model": "gen4_turbo",
        "ratio": f"{w}:{h}",
        "duration": 5 if duration <= 5 else 10,
    }
    r = _post_json("https://api.dev.runwayml.com/v1/text_to_video", payload, headers)
    task_id = r.get("id")
    if not task_id:
        print(f"[视频生成] Runway 未返回任务 ID: {str(r)[:200]}")
        return None

    def check():
        q = _get_json(f"https://api.dev.runwayml.com/v1/tasks/{task_id}", headers)
        status = str(q.get("status", "")).upper()
        if status == "SUCCEEDED":
            out = q.get("output") or []
            return out[0] if out else None
        if status in ("FAILED", "CANCELLED"):
            print(f"[视频生成] Runway 任务失败: {str(q)[:200]}")
            return ""
        return None

    return _wait_url(check)


def _gen_luma(prompt: str, key: str, duration: int) -> Optional[str]:
    if not key:
        print("[视频生成] 缺少 Luma API Key")
        return None
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "prompt": prompt,
        "model": "ray-2",
        "duration": f"{5 if duration <= 5 else 9}s",
        "aspect_ratio": "16:9",
    }
    r = _post_json("https://api.lumalabs.ai/dream-machine/v1/generations", payload, headers)
    gen_id = r.get("id")
    if not gen_id:
        print(f"[视频生成] Luma 未返回任务 ID: {str(r)[:200]}")
        return None

    def check():
        q = _get_json(
            f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}", headers)
        state = str(q.get("state", "")).lower()
        if state == "completed":
            return (q.get("assets") or {}).get("video")
        if state == "failed":
            print(f"[视频生成] Luma 任务失败: {str(q)[:200]}")
            return ""
        return None

    return _wait_url(check)


def _gen_wanx(prompt: str, key: str, duration: int, size: str) -> Optional[str]:
    if not key:
        print("[视频生成] 缺少阿里 DashScope API Key")
        return None
    headers = {
        "Authorization": f"Bearer {key}",
        "X-DashScope-Async": "enable",
    }
    w, h = (size or "1280*720").replace("x", "*").split("*")[:2]
    payload = {
        "model": "wan2.1-t2v-turbo",
        "input": {"prompt": prompt},
        "parameters": {"size": f"{w}*{h}", "duration": duration},
    }
    r = _post_json(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
        payload, headers)
    task_id = (r.get("output") or {}).get("task_id")
    if not task_id:
        print(f"[视频生成] 通义万相未返回 task_id: {str(r)[:200]}")
        return None

    def check():
        q = _get_json(
            f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
            {"Authorization": f"Bearer {key}"})
        out = q.get("output") or {}
        status = str(out.get("task_status", "")).upper()
        if status == "SUCCEEDED":
            return out.get("video_url")
        if status in ("FAILED", "CANCELED"):
            print(f"[视频生成] 通义万相任务失败: {str(q)[:200]}")
            return ""
        return None

    return _wait_url(check)


def _gen_openai_compat(prompt: str, key: str, base: str, duration: int, size: str) -> Optional[str]:
    base = (base or "").rstrip("/")
    if not base:
        print("[视频生成] openai_compat 需要在设置里填写自定义地址")
        return None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {"model": "video", "prompt": prompt, "duration": duration, "size": size}
    try:
        r = _post_json(f"{base}/videos/generations", payload, headers)
    except urllib.error.HTTPError as e:
        print(f"[视频生成] 自定义接口 HTTP {e.code}: {e.read()[:200]}")
        return None
    # 直接返回 URL 的情况
    if r.get("url"):
        return r["url"]
    if r.get("data") and isinstance(r["data"], list) and r["data"][0].get("url"):
        return r["data"][0]["url"]
    task_id = r.get("id") or r.get("task_id")
    if not task_id:
        print(f"[视频生成] 自定义接口未返回任务 ID: {str(r)[:200]}")
        return None

    def check():
        q = _get_json(f"{base}/videos/generations/{task_id}", headers)
        status = str(q.get("status", "")).lower()
        if status in ("succeeded", "success", "completed"):
            if q.get("url"):
                return q["url"]
            out = q.get("output") or []
            return out[0] if out else None
        if status in ("failed", "error"):
            print(f"[视频生成] 自定义接口任务失败: {str(q)[:200]}")
            return ""
        return None

    return _wait_url(check)


# ── 主入口 ──────────────────────────────────────────────────

def generate_video(prompt: str, duration: int = 5, size: str = "1280x720",
                   image_path: str = "") -> Dict:
    """
    文生视频（也可传首帧图片）。返回：
      {"ok": True, "path": 本地路径, "model": 使用的后端, "prompt": prompt}
      或 {"ok": False, "error": 错误信息}
    """
    cfg = _load_cfg()
    model = (cfg.get("video_model") or DEFAULT_MODEL).strip()
    base = (cfg.get("video_base_url") or "").strip()
    key = _get_key(cfg, model)
    image_url = ""
    if image_path:
        # 图生视频：智谱等接口接受公网图片 URL；本地文件暂不支持直接上传
        image_url = image_path if str(image_path).startswith("http") else ""

    print(f"[视频生成] 使用后端 {model}，时长 {duration}s，尺寸 {size}")
    try:
        if model in ("cogvideox-flash", "cogvideox-3"):
            url = _gen_glm(prompt, model, size, duration, image_url, key)
        elif model == "minimax":
            url = _gen_minimax(prompt, key, duration)
        elif model == "kling":
            url = _gen_kling(prompt, key, duration, size)
        elif model == "runway":
            url = _gen_runway(prompt, key, duration, size)
        elif model == "luma":
            url = _gen_luma(prompt, key, duration)
        elif model == "wanx":
            url = _gen_wanx(prompt, key, duration, size)
        elif model == "openai_compat":
            url = _gen_openai_compat(prompt, key, base, duration, size)
        else:
            return {"ok": False, "error": f"未知视频后端: {model}"}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        if e.code == 429:
            return {"ok": False,
                    "error": f"视频接口当前限流（HTTP 429，免费模型访问量过大），"
                             f"请稍后重试。{body}"}
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not url:
        return {"ok": False, "error": f"{model} 生成失败或超时"}

    path = _download(url, model)
    if not path:
        return {"ok": False, "error": "视频下载失败"}
    return {"ok": True, "path": path, "model": model, "prompt": prompt}
