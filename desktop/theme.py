"""
运行时主题重映射模块

主窗口里有大量写死的深色内联样式（setStyleSheet("#0d1117;...")），
逐个改不现实。这里通过拦截 QWidget.setStyleSheet，在运行时把深色
配色按主题统一替换为浅色（mac 风格白灰）配比：

- 深色主题：原样透传，不影响现有行为
- 浅色主题：#0d1117 → 浅灰背景、#e6edf3 → 深灰文字 …… 一键转白灰

切换主题时，对已创建的控件用登记过的原始样式重新 apply，即实现即时生效。
"""
import weakref

# ── 深色 → 浅色（mac 风白灰）映射 ─────────────────
# 依据 ui/main_window.py 里实际用到的深色板逐一映射
LIGHT_MAP = {
    "#0d1117": "#eef1f5",   # 主背景（略灰，防刺眼）
    "#161b22": "#f4f6f8",   # 次级背景 / 输入框
    "#1c2128": "#dfe4ea",   # 更深背景
    "#21262d": "#e2e6ec",   # 边框 / 按钮 / 悬停
    "#30363d": "#d6dbe1",   # 边框
    "#484f58": "#d0d5dc",   # 次要边框
    "#1f3a5c": "#dce9f7",   # 深蓝底
    "#e6edf3": "#24292f",   # 主文字
    "#c9d1d9": "#363b41",   # 文字
    "#8b949e": "#69717b",   # 次要文字
    "#6e7681": "#8a919b",   # 弱化文字
    "#1c1c1c": "#24292f",   # 近黑文字
    "#58a6ff": "#0969da",   # 亮蓝（文字/滚动条 hover）
    "#79c0ff": "#0b6bcb",   # 浅蓝
    "#1f6feb": "#0969da",   # 高亮蓝
    "#388bfd": "#0969da",   # 浅蓝
    "#7c3aed": "#7c3aed",   # 渐变紫（保留，白字按钮）
    "#bc8cff": "#8250df",   # 紫
    "#d2a8ff": "#9f66ff",   # 浅紫
    "#3fb950": "#1a7f37",   # 绿（成功）
    "#56d364": "#2da44e",   # 亮绿
    "#238636": "#1a7f37",   # 绿
    "#d29922": "#9a6700",   # 黄（警告）
    "#ffa657": "#cc6016",   # 橙
    "#f0883e": "#d95d1f",   # 橙黄
    "#f85149": "#cf222e",   # 红（错误/危险）
    "#ff7b72": "#d1242f",   # 浅红
    "#ffffff": "#ffffff",   # 白色（按钮白字，保持）
}

# 当前激活主题：'dark' | 'light'
_ACTIVE = "dark"

_orig_setStyleSheet = None   # 原始 QWidget.setStyleSheet
_inline = {}                 # weakref(QWidget) -> 原始样式字符串


def active_theme() -> str:
    return _ACTIVE


def remap(qss: str) -> str:
    """深色透传；浅色时把深色板替换为白灰"""
    if not qss:
        return qss
    if _ACTIVE != "light":
        return qss
    out = qss
    for dark, light in LIGHT_MAP.items():
        if dark in out:
            out = out.replace(dark, light)
    return out


def install_shim(theme: str = "dark"):
    """安装 setStyleSheet 拦截器（全局）。须在创建任何控件前调用。"""
    global _ACTIVE, _orig_setStyleSheet
    _ACTIVE = theme

    if _orig_setStyleSheet is not None:
        # 已安装：只更新主题，避免重复包装
        return

    from PyQt6 import QtWidgets

    def _drop(weak):
        _inline.pop(weak, None)

    def _patched_setstyle(self, qss):
        _inline[weakref.ref(self, _drop)] = qss
        _orig_setStyleSheet(self, remap(qss))

    _orig_setStyleSheet = QtWidgets.QWidget.setStyleSheet
    QtWidgets.QWidget.setStyleSheet = _patched_setstyle


def apply_theme(theme: str) -> str:
    """
    切换主题：更新全局状态 + 对已建控件用登记样式重新应用，
    返回应设置到应用/主窗口的全局 QSS。
    """
    global _ACTIVE
    _ACTIVE = theme
    # 重新应用所有登记过的内联样式
    for weak, raw in list(_inline.items()):
        w = weak()
        if w is not None and _orig_setStyleSheet is not None:
            _orig_setStyleSheet(w, remap(raw))
    from desktop.config import get_qss
    return get_qss(theme)