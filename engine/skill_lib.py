"""
skill_lib.py — 可复用技能沉淀库（工作方向 · 阶段3）
=====================================================
职责：把"连续多工具且成功的任务"抽象为可复用技能模板，供下次同类任务参考。

设计原则：
  - 与 learner.py 同库（guarded_connect），独立表 `skills`。
  - 只对"≥2 个工具且成功"的任务沉淀，单工具/失败不产生噪音。
  - 按任务标题特征匹配 + 工具名序列去重，相似技能合并强化 usage_count。
  - **技能只作为"参考提示"返回**，由调用方决定是否采用，绝不硬性约束模型。

对外提供：
  SkillStore.record_success(title, steps)  → 成功任务后沉淀，返回技能 id / None
  SkillStore.match(title)                  → 返回参考提示 str / 空串
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional

from engine.db_guard import guarded_connect

# 沉淀为技能的最少工具数
MIN_TOOLS_TO_SKILL = 2
# 关键词重叠率达到该值视为同一技能（合并强化）
SIMILARITY_THRESHOLD = 0.35


class SkillStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with guarded_connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id             TEXT PRIMARY KEY,
                    title          TEXT NOT NULL,
                    tool_sequence  TEXT NOT NULL,   -- json list[str]
                    step_titles    TEXT NOT NULL,   -- json list[str] 计划步骤
                    usage_count    INTEGER DEFAULT 1,
                    created_at     TEXT NOT NULL,
                    last_used_at   TEXT NOT NULL
                )
            """)
            try:
                conn.execute("SELECT step_titles FROM skills LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE skills ADD COLUMN step_titles TEXT NOT NULL DEFAULT '[]'"
                )
            conn.commit()

    # ── 分词（中文整串 + 2字符 n-gram，供相似度计算）────────
    @staticmethod
    def _keywords(text: str):
        raw = text or ""
        # 保留整词（英文/数字）
        out = set(re.findall(r'[a-zA-Z]+|\d+', raw))
        # 中文：2 字符 n-gram（大串也能共享"周报""一份"等局部特征）
        han = re.sub(r'[^\u4e00-\u9fff]', '', raw)
        out.update(han[i:i + 2] for i in range(max(0, len(han) - 1)))
        # 常见单字词（写/做/给/帮等）作为弱特征
        for ch in han:
            if ch in "写做产生创文报告会整分建生成给帮读查列搜":
                out.add(ch)
        return out

    @classmethod
    def _overlap(cls, a: str, b: str) -> float:
        ka, kb = cls._keywords(a), cls._keywords(b)
        if not ka or not kb:
            return 0.0
        return len(ka & kb) / max(len(ka), len(kb))

    # ── 写入 ──────────────────────────────────────────────
    def record_success(self, title: str, steps: List[dict]) -> Optional[str]:
        """成功任务后沉淀。返回技能 id；不满足条件或失败则返回 None。

        steps 需是 executor.execute_task 返回的 steps（每步含 tool / params / result）。
        仅当至少出现 MIN_TOOLS_TO_SKILL 个工具调用时视为技能。
        """
        if not title or not steps:
            return None
        tools = [str(s.get("tool", "")).strip() for s in steps
                 if isinstance(s, dict) and s.get("tool")]
        tools = [t for t in tools if t]
        if len(tools) < MIN_TOOLS_TO_SKILL:
            return None  # 单工具或失败不沉淀

        step_titles = [str(s.get("tool", "") or "步骤") for s in steps
                       if isinstance(s, dict)]
        tool_seq = json.dumps(tools, ensure_ascii=False)
        step_ser = json.dumps(step_titles, ensure_ascii=False)
        now = datetime.now().isoformat()

        try:
            with guarded_connect(self.db_path) as conn:
                existing = conn.execute(
                    "SELECT id, tool_sequence FROM skills WHERE title LIKE ?",
                    (title[:20] + "%",)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE skills SET usage_count=usage_count+1,"
                        " last_used_at=? WHERE id=?",
                        (now, existing[0])
                    )
                    return existing[0]

                rows = conn.execute(
                    "SELECT id, title, tool_sequence, usage_count "
                    "FROM skills"
                ).fetchall()
                for rid, rtitle, rseq, rcnt in rows:
                    if self._overlap(title, rtitle) >= SIMILARITY_THRESHOLD:
                        conn.execute(
                            "UPDATE skills SET usage_count=?, last_used_at=? "
                            "WHERE id=?",
                            (rcnt + 1, now, rid)
                        )
                        return rid
                sid = uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT INTO skills (id, title, tool_sequence, step_titles,"
                    " usage_count, created_at, last_used_at) "
                    "VALUES (?,?,?,?,1,?,?)",
                    (sid, title, tool_seq, step_ser, now, now)
                )
                return sid
        except Exception:
            return None

    # ── 读取（返回参考提示，无命中返回空串）────────────────
    def match(self, title: str) -> str:
        """按任务标题查找历史技能，返回"参考提示"文本（供注入 system prompt）。

        不命中一律返回空串，绝不抛异常、绝不阻塞调用方。
        """
        if not title:
            return ""
        try:
            with guarded_connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, title, tool_sequence, step_titles, usage_count "
                    "FROM skills"
                ).fetchall()
        except Exception:
            return ""

        best = None
        best_score = 0.0
        for rid, rtitle, rseq, rsteps, rcnt in rows:
            score = self._overlap(title, rtitle)
            if score > best_score:
                best_score = score
                best = (rid, rtitle, rseq, rsteps, rcnt)
        if best is None or best_score < SIMILARITY_THRESHOLD:
            return ""

        rid, rtitle, rseq, rsteps, rcnt = best
        try:
            tool_seq = json.loads(rseq) if rseq else []
            step_titles = json.loads(rsteps) if rsteps else []
        except (json.JSONDecodeError, ValueError):
            tool_seq, step_titles = [], []

        lines = [f"你以前完成过类似任务（{rtitle}，使用过 {rcnt} 次）"]
        if step_titles:
            lines.append("之前的步骤：" + " → ".join(step_titles))
        if tool_seq:
            lines.append("之前用过且有效的工具序列：" + " → ".join(tool_seq))
        lines.append("这些只作参考，你可根据当前情况灵活调整。")
        self.touch(rid)
        return "\n".join(lines)

    def touch(self, skill_id: str) -> None:
        try:
            with guarded_connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE skills SET last_used_at=? WHERE id=?",
                    (datetime.now().isoformat(), skill_id)
                )
        except Exception:
            pass

    def count(self) -> int:
        try:
            with guarded_connect(self.db_path) as conn:
                return int(conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0])
        except Exception:
            return 0


if __name__ == "__main__":
    import tempfile
    db = tempfile.mktemp(suffix=".db")
    ss = SkillStore(db)
    steps = [
        {"tool": "search_web", "result": {"ok": True}},
        {"tool": "create_docx", "result": {"ok": True}},
    ]
    sid = ss.record_success("给用户写一份周报文档", steps)
    print("recorded:", sid, "count:", ss.count())
    hint = ss.match("帮我写一份周报")
    print("match hint:\n", hint)
    print("no-match:", repr(ss.match("完全无关的需求")))