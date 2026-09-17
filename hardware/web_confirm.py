"""
web_confirm.py
网页端高危工具确认机制
====================
当 B 层执行器遇到高风险工具时，不再阻塞命令行，
而是通过 SSE 推送确认请求到前端，前端弹窗让用户确认。

流程：
  1. executor 调用 confirm(tool_name, params)
  2. 请求存入 _pending，通过 SSE 推送给前端
  3. 前端弹窗，用户点击 同意/拒绝
  4. 前端调用 /api/confirm 回传结果
  5. confirm() 返回 True/False
"""

import time
import uuid
import threading
from typing import Dict, Optional

# 全局待确认请求表
_pending: Dict[str, dict] = {}      # confirm_id → {event, result, lock}
_sse_listeners = []                  # SSE 监听者队列


def request_confirm(tool_name: str, params: dict, timeout: float = 120) -> bool:
    """
    请求网页端确认（阻塞调用，等待前端回复）。
    超时返回 False。
    """
    confirm_id = str(uuid.uuid4())[:8]
    event = {
        "id": confirm_id,
        "type": "confirm_request",
        "tool_name": tool_name,
        "params": params,
        "timestamp": time.time(),
    }

    lock = threading.Event()
    _pending[confirm_id] = {
        "event": event,
        "result": None,
        "lock": lock,
    }

    # 推送给所有 SSE 监听者
    _notify_sse(event)

    print(f"[WebConfirm] 等待确认: {tool_name} (id={confirm_id})")

    # 阻塞等待结果
    ok = lock.wait(timeout=timeout)

    entry = _pending.pop(confirm_id, None)
    if entry and entry["result"] is not None:
        result = entry["result"]
        print(f"[WebConfirm] 结果: {tool_name} → {'允许' if result else '拒绝'}")
        return result

    # 超时
    print(f"[WebConfirm] 超时: {tool_name} → 拒绝")
    return False


def resolve_confirm(confirm_id: str, approved: bool) -> bool:
    """
    前端回复确认结果。
    返回 True 表示成功处理，False 表示找不到请求。
    """
    entry = _pending.get(confirm_id)
    if not entry:
        return False
    entry["result"] = approved
    entry["lock"].set()
    return True


def get_pending_confirms() -> list:
    """获取所有待确认请求"""
    return [v["event"] for v in _pending.values() if v["result"] is None]


# ── SSE 支持 ──────────────────────────────────────────────

def _notify_sse(event: dict):
    """通知所有 SSE 监听者"""
    import json
    data = json.dumps(event, ensure_ascii=False)
    stale = []
    for i, q in enumerate(_sse_listeners):
        try:
            q.put_nowait(data)
        except Exception:
            stale.append(i)
    for i in reversed(stale):
        _sse_listeners.pop(i)


def create_sse_queue():
    """创建 SSE 队列（供 /api/confirm_stream 使用）"""
    import queue
    q = queue.Queue(maxsize=50)
    _sse_listeners.append(q)
    return q


def remove_sse_queue(q):
    """移除 SSE 队列"""
    try:
        _sse_listeners.remove(q)
    except ValueError:
        pass
