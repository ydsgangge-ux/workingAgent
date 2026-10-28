"""统一入站附件目录

所有渠道（QQ / 飞书 / 企业微信 / Web）收到的文件、图片都落到项目根下的
``inbox/``，文件名保留原名，重名自动加序号。这样用户能在固定位置找到
发过来的文件，agent 也能拿到稳定、可跨轮复用的路径。
"""
import re
from pathlib import Path
from typing import Optional

INBOX_DIR = Path(__file__).resolve().parent.parent / "inbox"

_UNSAFE_RE = re.compile(r"[^\w.\-\u4e00-\u9fff]")


def inbox_dir() -> Path:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    return INBOX_DIR


def safe_name(name: str, fallback: str = "file") -> str:
    """只保留文件名部分，去掉路径分隔符和非法字符"""
    name = (name or fallback).strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_RE.sub("_", name).strip("._")
    return name or fallback


def _unique_path(directory: Path, name: str) -> Path:
    dst = directory / name
    if not dst.exists():
        return dst
    stem = Path(name).stem
    suffix = Path(name).suffix
    i = 2
    while dst.exists():
        dst = directory / f"{stem}_{i}{suffix}"
        i += 1
    return dst


def save_bytes(data: bytes, filename: str = "") -> Optional[Path]:
    """把附件字节写入 inbox，返回落盘路径"""
    if not data:
        return None
    dst = _unique_path(inbox_dir(), safe_name(filename))
    dst.write_bytes(data)
    return dst


def save_url(url: str, filename: str = "", default_ext: str = "",
             timeout: float = 30) -> Optional[Path]:
    """从 URL 下载附件到 inbox"""
    if not url or not str(url).startswith("http"):
        return None
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        name = filename or Path(str(url).split("?")[0]).name
        if not Path(name).suffix and default_ext:
            name = f"{name}.{default_ext}"
        return save_bytes(r.content, name)
    except Exception as e:
        print(f"[Inbox] 下载失败: {e}")
        return None
