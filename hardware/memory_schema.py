"""
统一数据模板 — 室内/室外/过渡区共用
对应 visual_memory_schema.svg
"""
from dataclasses import dataclass, field
from typing import Optional, Literal
from datetime import datetime, timezone
import uuid


@dataclass
class VisualMemory:
    """一条视觉记忆记录，覆盖室内/室外/过渡区三种场景"""

    # ── 基础字段（必填） ──
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    scene_type: Literal["indoor", "outdoor", "transitional"] = "indoor"
    memory_type: Literal["space", "person", "interest", "event"] = "event"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ── 位置字段（条件填充） ──
    gps: Optional[dict] = None          # {"lat": 31.2, "lng": 121.4}  室外必填
    gps_accuracy: Optional[float] = None  # 米
    indoor_coords: Optional[dict] = None  # {"x": 3.2, "y": 1.1, "z": 0} 室内必填
    landmark_ref: Optional[str] = None    # 就近地标 node_id，室外必填
    location_confidence: float = 0.0      # 0.0~1.0

    # ── 内容字段（语义核心） ──
    description: str = ""                 # 客观描述，Vision模型生成，用于检索
    objects: list = field(default_factory=list)   # [{"label","position","state"}]
    persons: list = field(default_factory=list)   # [{"id","name","action"}]
    event_summary: str = ""               # 本帧发生了什么
    subjective_note: str = ""             # Levy主观感受，仅interest类填写，不参与检索

    # ── 图片字段 ──
    image_path: Optional[str] = None          # 对应图片文件路径（相对 data/visual_images/）
    image_category: Optional[str] = None      # space / event / person / outdoor
    pinned: bool = False                      # 用户指定记住，永不清除

    # ── 记忆管理字段 ──
    importance: float = 0.5               # 0.0~1.0，动态更新
    vision_confidence: float = 0.0        # 本帧视觉理解置信度
    last_accessed: Optional[str] = None   # 最近一次被检索的时间
    access_count: int = 0                 # 累计检索次数
