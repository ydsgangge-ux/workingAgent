"""多段用户输入合并处理（应用层缓冲器）

解决：A 层单线程一次一轮，当用户在上一条(Q1)处理期间又发送新消息(Q2)时，
不打断、不丢，按规则处理：
  - 纯算型 Q1：结果攒住不发，Q2 算完后两次结果轻量合并成一段输出（不重跑全流程）
  - 动作型 Q1（含 [image:]/[download:] 交付标记）：标记随文本保留，合并不切坏

两种用法：
  1. route()           —— 同步串行 + 合并（适合 web_server 的同步 handler）
  2. claim/pend/release —— 纯串行化（适合桌面，代理在子线程跑，主线程只排队）
"""
import threading
from typing import Optional, List, Dict, Any, Callable


class MultiSeg:
    def __init__(self):
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}

    # ── 纯串行化接口（桌面）────────────────────────────
    def claim(self, uid: str) -> bool:
        """尝试进入处理中。空闲→置忙返回 True；正在处理→返回 False（走 pend）。"""
        with self._lock:
            st = self._states.setdefault(uid, {"busy": False, "pending": []})
            if st["busy"]:
                return False
            st["busy"] = True
            return True

    def pend(self, uid: str, text: str) -> None:
        """正在处理时挂起后续输入。"""
        with self._lock:
            st = self._states.setdefault(uid, {"busy": False, "pending": []})
            st["pending"].append(text)

    def release(self, uid: str) -> List[str]:
        """结束处理，取出积压待办并清空。返回待处理列表，调用方串行继续。"""
        with self._lock:
            st = self._states.setdefault(uid, {"busy": False, "pending": []})
            st["busy"] = False
            pend = st["pending"]
            st["pending"] = []
            return pend

    # ── 同步路由 + 合并（web）─────────────────────────
    def route(
        self,
        agent,
        uid: str,
        text: str,
        process_fn: Callable[[str], Dict[str, Any]],
        emit_fn: Callable[[Dict[str, Any]], None],
        on_each: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """同步串行处理并合并。同一时刻仅一个执行，其余挂起并随后合并进一次输出。"""
        with self._lock:
            st = self._states.setdefault(uid, {"busy": False, "pending": []})
            if st["busy"]:
                st["pending"].append(text)
                return
            st["busy"] = True

        combined: Optional[Dict[str, Any]] = None
        try:
            combined = process_fn(text)
            if on_each:
                on_each(combined)
            # 处理积压：Q1 算完不急着发，等后续输入算完一起合并
            while True:
                with self._lock:
                    st = self._states[uid]
                    if not st["pending"]:
                        break
                    q = st["pending"].pop(0)
                r2 = process_fn(q)
                if on_each:
                    on_each(r2)
                combined = self._merge(agent, combined, r2)
            emit_fn(combined)
        except Exception:
            # 已算出至少一问 → 尽量交付，不吞掉用户输入
            if combined is not None:
                try:
                    emit_fn(combined)
                except Exception:
                    pass
            else:
                raise
        finally:
            with self._lock:
                st = self._states[uid]
                st["busy"] = False
                st["pending"] = []

    def _merge(self, agent, r1: Dict[str, Any], r2: Dict[str, Any]) -> Dict[str, Any]:
        """两次结果轻量合并：1 次 LLM 文本合并，不重跑 A 层感知/记忆/工具。"""
        out = dict(r1)
        t1 = r1.get("response", "") if isinstance(r1, dict) else str(r1)
        t2 = r2.get("response", "") if isinstance(r2, dict) else str(r2)
        merged = self._llm_merge(agent, t1, t2)
        out["response"] = merged
        # 工具/情绪以最近的（后问句）为主
        if isinstance(r2, dict):
            for k in ("tool_steps", "tools_used", "emotion"):
                if r2.get(k):
                    out[k] = r2[k]
        return out

    @staticmethod
    def _llm_merge(agent, t1: str, t2: str) -> str:
        if not t1 or not t2:
            return (t1 or "") + (("\n\n" + t2) if t2 else "")
        prompt = (
            "这是我针对用户先后两个问题分别给出的回答 A 与回答 B。\n"
            "请把它们自然地合并成一次完整、连贯的回应输出，覆盖这两个问题，"
            "不重复、不编造、不重新分析。保留其中所有 [图片:...]/[image:]/[download:] 等标记原样不动。\n"
            "只用合并后的最终文本作答，不要额外说明。\n\nA：\n" + t1 + "\n\nB：\n" + t2
        )
        try:
            b = getattr(agent, "b", None)
            if b is not None and hasattr(b, "generate"):
                m = b.generate(prompt, max_tokens=2048)
                if m and str(m).strip():
                    return str(m).strip()
        except Exception:
            pass
        # 兜底：直接拼接，不切坏交付标记
        return t1 + "\n\n" + t2


# 模块级单例：桌面多窗口、web、飞书等各入口共用同一队列，
# 避免同一用户从不同入口连发时并发交叉 A 层历史。
multiseg = MultiSeg()