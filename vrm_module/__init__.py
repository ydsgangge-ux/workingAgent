"""
VRM 虚拟形象模块 — 安全加载入口

遵循零侵入原则：加载失败不影响主程序运行。
"""

VRM_AVAILABLE = False
vrm_widget_class = None

try:
    from .vrm_widget import VRMWidget
    VRM_AVAILABLE = True
    vrm_widget_class = VRMWidget
except Exception as e:
    print(f"[VRM] 模块加载失败，已跳过: {e}")
