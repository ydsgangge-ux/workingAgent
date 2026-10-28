"""附件待指令缓冲

QQ / 飞书发文件时，附件往往单独成为一条消息，紧接着才是"帮我标注一下"
这类指令。这里做一个小窗口缓冲：附件先到就暂存，等同一用户的下一条文字
一起提交；超时则用附件本身提交一次（保持原有"请分析以上内容"的行为）。
"""
import threading
from typing import Callable, Dict, Optional


class PendingAttachments:
    """按用户维度暂存"已收到、还未下指令"的附件"""

    def __init__(self, delay: float = 8.0):
        self._delay = delay
        self._lock = threading.Lock()
        self._items: Dict[str, dict] = {}

    def hold(self, key: str, attachment: str,
             submit: Callable[[str], None]) -> None:
        """暂存附件；若该用户已有暂存，则合并为一个附件串"""
        if not attachment:
            return
        with self._lock:
            item = self._items.get(key)
            if item:
                item["timer"].cancel()
                attachment = (item["attachment"] + " " + attachment).strip()
            timer = threading.Timer(self._delay, self._expire, args=(key,))
            timer.daemon = True
            self._items[key] = {
                "attachment": attachment,
                "timer": timer,
                "submit": submit,
            }
            timer.start()

    def take(self, key: str) -> Optional[str]:
        """取走暂存附件（下一条文字到达时调用），并取消超时提交"""
        with self._lock:
            item = self._items.pop(key, None)
        if not item:
            return None
        item["timer"].cancel()
        return item["attachment"]

    def _expire(self, key: str) -> None:
        with self._lock:
            item = self._items.pop(key, None)
        if not item:
            return
        try:
            item["submit"](item["attachment"])
        except Exception as e:
            print(f"[PendingAttachments] 超时提交失败: {e}")
