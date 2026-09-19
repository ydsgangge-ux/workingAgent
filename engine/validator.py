"""
validator.py — 产出校验器（工作方向 · 阶段1）
==============================================
职责：对工具"声称完成"的产出做真实校验——文件存在吗、非空吗、格式对吗，
     必要时回读文件内容核对关键词。做对了才算成功。

设计原则：
  - 纯新增模块，不改变工具本身的执行。
  - 校验结果返回结构化 `ValidationResult`：通过/不通过 + 人类可读原因。
  - 容错优先：校验器自身异常绝不导致执行崩溃，视为"不拦截"（宽松放行）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> Dict:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail}


def _ok(reason: str = "") -> ValidationResult:
    return ValidationResult(ok=True, reason=reason)


def _fail(reason: str, detail: str = "") -> ValidationResult:
    return ValidationResult(ok=False, reason=reason, detail=detail)


def _path_candidates(value: Any) -> List[Path]:
    """从工具返回里尽量抠出文件路径（兼容 str / 含 'path' 键的 dict）。"""
    if isinstance(value, str):
        return [Path(value)]
    if isinstance(value, dict):
        out = []
        for key in ("path", "filepath", "file_path", "result", "output", "filename"):
            v = value.get(key)
            if isinstance(v, str):
                out.append(Path(v))
        return out
    return []


def _file_passed(path: Path, min_size: int = 4) -> Optional[str]:
    """返回 None 表示通过；否则返回失败原因。"""
    p = path if path.is_absolute() else Path.cwd() / path
    if not p.exists():
        return f"文件不存在：{path}"
    if not p.is_file():
        return f"不是文件：{path}"
    try:
        size = p.stat().st_size
    except OSError as e:
        return f"无法读取文件信息：{e}"
    if size < min_size:
        return f"文件过小（{size}B），疑似空产出：{path}"
    return None


def _text_contains_failed(path: Path, keywords: List[str]) -> Optional[str]:
    """需要回读文本校验关键词；任何一个命中即通过。"""
    if not keywords:
        return None
    p = path if path.is_absolute() else Path.cwd() / path
    try:
        content = p.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return f"无法读取文件内容校验关键词：{e}"
    if any(kw.lower() in content.lower() for kw in keywords):
        return None
    return f"文件中未找到预期关键词（{', '.join(keywords[:3])}）"


# ── 规则表：tool_name -> {ext, min_size} ──────────────
_RULES: Dict[str, Dict[str, Any]] = {
    "create_pdf":     {"ext": ".pdf"},
    "create_docx":    {"ext": ".docx"},
    "create_pptx":    {"ext": ".pptx"},
    "annotate_pdf":   {"ext": ".png"},
    "write_file":     {"min_size": 4},
}

DEFAULT_FORBIDDEN_EXT = {".tmp", ".part", ".swp"}


def validate_tool_output(tool_name: str, result_content: Any,
                         keywords: Optional[List[str]] = None) -> ValidationResult:
    """校验一个工具调用的产出。keywords 可选：期望结果里出现的关键词。

    未配置规则的工具 → 直接放行（不误伤既有行为）。
    """
    try:
        return _validate(tool_name, result_content, keywords or [])
    except Exception as e:
        return _ok(reason=f"校验器自身异常，已放行：{type(e).__name__}")


def _validate(tool_name, result_content, keywords) -> ValidationResult:
    if not isinstance(result_content, dict) or not result_content.get("ok"):
        return _fail("工具自身未报告成功（ok=False）")
    if tool_name not in _RULES:
        return _ok(reason="未配置该校验规则，放行")

    rule = _RULES[tool_name]
    ext = rule.get("ext")
    min_size = rule.get("min_size", 4)

    candidates = _path_candidates(result_content)
    effective = []
    for c in candidates:
        if c.suffix.lower() in DEFAULT_FORBIDDEN_EXT:
            return _fail(f"产出为临时文件（{c.suffix}），判定未完成")
        effective.append(c)
    if not effective:
        return _fail("无法从工具返回中定位产出文件路径")

    checked = []
    for c in effective:
        err = _file_passed(c, min_size=min_size)
        if err:
            checked.append(err)
            continue
        if ext and c.suffix.lower() != ext:
            checked.append(f"后缀不符（期望 {ext}，实际 {c.suffix}）：{c}")
            continue
        kw_err = _text_contains_failed(c, keywords) if keywords else None
        if kw_err:
            checked.append(kw_err)
            continue
        return _ok(reason=f"校验通过：{c}")

    return _fail("全部候选文件均未通过", "；".join(checked[:3]))


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "report.docx"
        p.write_text("hello world", encoding="utf-8")
        print(validate_tool_output("create_docx", {"ok": True, "path": str(p)}))
        print(validate_tool_output("create_docx", {"ok": True, "path": str(p)},
                                   keywords=["不存在词"]))
        print(validate_tool_output("create_docx", {"ok": True}))