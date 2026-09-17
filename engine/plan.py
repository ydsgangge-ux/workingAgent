"""
plan.py — 步骤计划栈（工作方向 · 阶段1）
==========================================
职责：把长任务拆成可执行、可回溯、可校验的分步计划。

设计原则：
  - 纯新增模块，不改动 B 层原有的 ReAct 循环与失败重试逻辑。
  - 计划是"可预测、可计算"的：每步都有 id / 期望产出，执行时逐步推进、
    可回溯、可判定完成与否。
  - 不依赖 LLM 就能工作：本模块只负责"结构化 + 解析 + 状态追踪"，
    LLM 的调用方（executor）负责喂给它计划文本。

对外提供：
  PlanStep        — 单步计划数据结构
  TaskPlan        — 完整计划（目标 + 步骤列表）
  parse_plan_text — 从 LLM 返回的计划文本解析出 TaskPlan
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    """单步计划。

    title / expected（期望产出）由解析填充；status / result / note 由执行层回填。
    """
    id: str
    title: str
    expected: str = ""
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    note: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "title": self.title,
            "expected": self.expected,
            "status": self.status.value,
            "result": self.result,
            "note": self.note,
        }


@dataclass
class TaskPlan:
    goal: str = ""
    steps: List[PlanStep] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> Dict:
        return {"goal": self.goal, "steps": [s.to_dict() for s in self.steps]}

    def is_empty(self) -> bool:
        return not self.steps

    def summary(self) -> str:
        parts = [f"目标：{self.goal or '(未指定)'}"]
        for s in self.steps:
            parts.append(f"  {s.id} {s.title}")
        return "\n".join(parts)


# ── 计划文本解析 ──────────────────────────────────────
# 容错优先：解析失败时返回空计划，调用方回退到无计划执行，绝不中断任务。


def _try_parse_json(text: str) -> Optional[TaskPlan]:
    """解析 `{"goal":..., "steps":[{"title":...}]}` 形式。"""
    match = re.search(r"(\{.*\})", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    steps = []
    for i, raw in enumerate(data.get("steps", []) or []):
        if isinstance(raw, dict):
            steps.append(PlanStep(
                id=f"step_{i + 1}",
                title=str(raw.get("title", "") or "").strip() or f"步骤 {i + 1}",
                expected=str(raw.get("expected", "") or "").strip(),
            ))
        elif isinstance(raw, str):
            steps.append(PlanStep(id=f"step_{i + 1}", title=raw.strip()))
    if not steps:
        return None
    return TaskPlan(goal=str(data.get("goal", "") or "").strip(),
                    steps=steps, raw_text=text)


def _try_parse_numbered(text: str) -> Optional[TaskPlan]:
    """解析 `1. 步骤一\n2. 步骤二` 形式。"""
    lines = [ln.strip().lstrip("-*") for ln in text.splitlines() if ln.strip()]
    titles = []
    for ln in lines:
        m = re.match(r"^(\d+)[.、．\)]\s*(.+)$", ln)
        if m:
            titles.append(m.group(2).strip())
    if not titles:
        return None
    steps = [PlanStep(id=f"step_{i + 1}", title=t) for i, t in enumerate(titles)]
    return TaskPlan(steps=steps, raw_text=text)


def parse_plan_text(text: str) -> TaskPlan:
    text = (text or "").strip()
    if not text:
        return TaskPlan()
    plan = _try_parse_json(text)
    if plan is not None and not plan.is_empty():
        return plan
    plan = _try_parse_numbered(text)
    if plan is not None:
        return plan
    return TaskPlan(raw_text=text)


if __name__ == "__main__":
    tp = parse_plan_text("1. 读取需求\n2. 生成大纲\n3. 生成PPT\n4. 校验交付")
    print("steps:", [s.title for s in tp.steps])
    print(tp.summary())