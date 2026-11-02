"""LLM 原始请求/响应捕获器

用途：定位 MiMo 返回 "The request was rejected because it was considered high risk"
这类异常输出。它会包住 urllib，把每次大模型调用的**完整请求体、HTTP 状态、
响应头和原始响应全文**写入 logs/llm_capture/，然后正常启动主程序。

用法：
    python llm_capture.py            # 带捕获地启动（等同 python main.py）
    python llm_capture.py --list     # 列出最近 10 条捕获
    python llm_capture.py --show 5   # 打印第 5 条（按列表编号）的完整内容

说明：
  - 只有 /chat/completions 之类的模型调用会被记录，图片、网页下载不记录
  - Authorization 只保留前缀，避免把 Key 写到磁盘
  - 命中审核拒绝时控制台会带 ⚠ 标记
"""

import io
import json
import os
import runpy
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURE_DIR = ROOT / "logs" / "llm_capture"
MAX_FILES = 300                      # 超过后删除最旧的
MAX_RAW = 8 * 1024 * 1024            # 单条响应最多保留 8MB

_lock = threading.Lock()
_seq = 0
_orig_urlopen = urllib.request.urlopen


# ── 工具函数 ────────────────────────────────────────────────
def _mask_headers(headers) -> dict:
    out = {}
    try:
        items = headers.items() if hasattr(headers, "items") else dict(headers or {}).items()
    except Exception:
        return out
    for k, v in items:
        if k.lower() == "authorization":
            v = str(v)
            out[k] = v[:16] + "…" if len(v) > 16 else "…"
        else:
            out[k] = str(v)
    return out


def _body_bytes(url, data) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if hasattr(url, "data") and url.data:
        return bytes(url.data)
    return b""


def _is_llm_call(req_url: str, raw_body: bytes) -> bool:
    u = (req_url or "").lower()
    if "chat/completions" in u or "completions" in u:
        return True
    return b'"messages"' in raw_body[:200000]


def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _request_url(url) -> str:
    if hasattr(url, "full_url"):
        return url.full_url
    return str(url)


def _save_record(record: dict) -> str:
    global _seq
    with _lock:
        _seq += 1
        seq = _seq
    model = record.get("model") or "unknown"
    model = "".join(c if c.isalnum() or c in "-._" else "_" for c in model)[:40]
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{seq:03d}_{model}.json"
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = CAPTURE_DIR / name
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        files = sorted(p for p in CAPTURE_DIR.glob("*.json"))
        for old in files[:-MAX_FILES]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    return name


class _CachedResponse:
    """读完响应后仍能像原对象一样被调用方使用"""

    def __init__(self, raw, body: bytes):
        self._buf = io.BytesIO(body)
        self.status = getattr(raw, "status", None) or getattr(raw, "code", None)
        self.code = self.status
        self.headers = getattr(raw, "headers", None)
        self.url = getattr(raw, "url", None)
        self.reason = getattr(raw, "reason", None)

    def read(self, *a, **kw):
        return self._buf.read(*a, **kw)

    def readinto(self, buf):
        return self._buf.readinto(buf)

    def getcode(self):
        return self.status

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def close(self):
        try:
            self._buf.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _record(url, data, status, resp_headers, body: bytes, elapsed_ms, error: str = ""):
    raw_text = ""
    try:
        raw_text = body.decode("utf-8", "replace")
    except Exception:
        raw_text = ""
    if len(raw_text) > MAX_RAW:
        raw_text = raw_text[:MAX_RAW] + "\n…[已截断]"

    req_body_raw = _body_bytes(url, data)
    req_text = req_body_raw.decode("utf-8", "replace")
    req_json = _safe_json(req_text)
    parsed = req_json if isinstance(req_json, dict) else None

    model = (parsed or {}).get("model") or ""
    resp_json = _safe_json(raw_text)
    choice = {}
    if isinstance(resp_json, dict) and resp_json.get("choices"):
        choice = resp_json["choices"][0] or {}
    message = choice.get("message") or {}

    flagged = any(k in raw_text.lower() for k in
                  ("high risk", "rejected because", "risk control", "content_filter"))

    record = {
        "seq": _seq,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "elapsed_ms": round(elapsed_ms, 1),
        "url": _request_url(url),
        "model": model,
        "http_status": status,
        "error": error,
        "flagged_as_risk": flagged,
        "request": {
            "headers": _mask_headers(getattr(url, "headers", None)),
            "body": parsed if parsed is not None else req_text,
        },
        "response": {
            "headers": _mask_headers(resp_headers),
            "finish_reason": choice.get("finish_reason"),
            "usage": resp_json.get("usage") if isinstance(resp_json, dict) else None,
            "content": message.get("content"),
            "refusal": message.get("refusal"),
            "raw": raw_text,
        },
    }
    name = _save_record(record)

    head = (message.get("content") or raw_text or "").replace("\n", " ")[:60]
    mark = "⚠ 命中审核拒绝 " if flagged else ""
    print(f"[LLM捕获] #{_seq:03d} {mark}HTTP {status} {model or '?'} "
          f"{elapsed_ms/1000:.1f}s → {name} | {head}")


# ── 包装 urlopen ────────────────────────────────────────────
def _urlopen(url, data=None, timeout=None, *args, **kwargs):
    req_url = _request_url(url)
    raw_body = _body_bytes(url, data)
    capture = _is_llm_call(req_url, raw_body)
    t0 = time.time()

    try:
        resp = _orig_urlopen(url, data, timeout, *args, **kwargs)
    except urllib.error.HTTPError as e:
        if capture:
            try:
                body = e.read()
            except Exception:
                body = b""
            _record(url, data, e.code, getattr(e, "headers", None), body,
                    (time.time() - t0) * 1000, error=f"HTTPError {e.code} {e.msg}")
            raise urllib.error.HTTPError(e.url, e.code, e.msg, e.hdrs, io.BytesIO(body))
        raise

    if not capture:
        return resp

    try:
        body = resp.read()
        _record(url, data, getattr(resp, "status", None) or resp.getcode(),
                getattr(resp, "headers", None), body, (time.time() - t0) * 1000)
        return _CachedResponse(resp, body)
    except Exception as e:
        print(f"[LLM捕获] 读取响应失败: {e}")
        return resp


def install():
    """打补丁；重复调用无副作用"""
    if urllib.request.urlopen is not _urlopen:
        urllib.request.urlopen = _urlopen


# ── 查看捕获 ────────────────────────────────────────────────
def _capture_files():
    if not CAPTURE_DIR.exists():
        return []
    return sorted(CAPTURE_DIR.glob("*.json"))


def _list():
    files = _capture_files()
    if not files:
        print(f"暂无捕获，目录: {CAPTURE_DIR}")
        return
    print(f"目录: {CAPTURE_DIR}（共 {len(files)} 条，显示最近 10 条）\n")
    for i, p in enumerate(files[-10:], 1):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        flag = "⚠ " if d.get("flagged_as_risk") else "  "
        content = (d.get("response", {}).get("content") or "")[:50].replace("\n", " ")
        print(f"{flag}{i}. {p.name} | HTTP {d.get('http_status')} | {d.get('model')} | {content}")


def _show(n: int):
    files = _capture_files()
    if not files:
        print("暂无捕获记录")
        return
    tail = files[-10:]
    if not (1 <= n <= len(tail)):
        print(f"编号超出范围，当前可用 1~{len(tail)}")
        return
    print(json.dumps(json.loads(tail[n - 1].read_text(encoding="utf-8")),
                     ensure_ascii=False, indent=2))


def main():
    args = sys.argv[1:]
    if "--list" in args:
        _list()
        return
    if "--show" in args:
        i = args.index("--show")
        _show(int(args[i + 1]) if i + 1 < len(args) else 1)
        return

    install()
    entry = ROOT / "main.py"
    sys.path.insert(0, str(ROOT))
    sys.argv = [str(entry)] + args
    print(f"[LLM捕获] 已启用，记录目录: {CAPTURE_DIR}")
    print(f"[LLM捕获] 启动 {entry}")
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
