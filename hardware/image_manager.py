"""
图片管理器 — 视觉记忆图片的保存、路径生成、清理
================================================
存储策略：
  space/   室内固定空间图库，每空间最多4张（四视角），覆盖更新
  event/   语义事件流，有事件就存，FIFO 淘汰
  person/  人像图库，每人最多5张，1个月清理（pinned 除外）
  outdoor/ 室外按坐标存储，按距离分级（10/20/50米）

每张图都有对应的 visual_memory 记录（文字描述+图片路径），
不会出现"不知道图是什么"的情况。
"""
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 图片根目录
IMAGE_ROOT = Path(__file__).parent.parent / "data" / "visual_images"

# 每类图片上限
LIMITS = {
    "space": 200,
    "event": 300,
    "person": 500,
    "outdoor": 500,
}

# JPEG 压缩质量（0-100）
JPEG_QUALITY = 85

# 室外距离分级（米）
OUTDOOR_DENSITY_RADIUS = {
    "dense": 10,    # 走廊/车间/室内
    "normal": 20,   # 院子/停车场/小区
    "open": 50,     # 街道/广场/空地
}

# 人像清理天数（未 pinned 的）
PERSON_EXPIRE_DAYS = 30


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """计算两个 GPS 坐标之间的距离（米）"""
    R = 6371000  # 地球半径（米）
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class ImageManager:
    """统一管理视觉记忆图片的保存、路径生成、清理"""

    def __init__(self, root: Optional[str] = None):
        self.root = Path(root) if root else IMAGE_ROOT
        self._ensure_dirs()

    def _ensure_dirs(self):
        for cat in ("space", "event", "person", "outdoor"):
            (self.root / cat).mkdir(parents=True, exist_ok=True)

    # ── 保存图片 ──────────────────────────────────────

    def save_image(
        self,
        img_bytes: bytes,
        category: str,
        label: str = "",
        gps: Optional[dict] = None,
    ) -> Optional[str]:
        """
        保存图片并返回相对路径（相对于 IMAGE_ROOT）。

        category: space / event / person / outdoor
        label: 空间名/人名/事件描述（用于文件名）
        gps: {"lat": ..., "lng": ...}，outdoor 必填
        """
        if not img_bytes or len(img_bytes) < 100:
            return None

        category = category.lower()
        if category not in ("space", "event", "person", "outdoor"):
            category = "event"

        filename = self._generate_filename(category, label, gps)
        rel_path = f"{category}/{filename}"
        abs_path = self.root / rel_path

        # 压缩保存
        try:
            self._save_compressed(img_bytes, abs_path)
        except Exception as e:
            print(f"[ImageManager] 保存失败: {e}")
            return None

        return rel_path

    def _generate_filename(
        self,
        category: str,
        label: str,
        gps: Optional[dict],
    ) -> str:
        """根据类别生成文件名"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(c for c in label[:20] if c.isalnum() or c in "_-") or "img"

        if category == "outdoor" and gps:
            lat_s = f"{gps['lat']:.4f}"
            lng_s = f"{gps['lng']:.4f}"
            return f"{lat_s}_{lng_s}_{ts}.jpg"
        elif category == "space":
            return f"{safe_label}_{ts}.jpg"
        elif category == "person":
            return f"{safe_label}_{ts}.jpg"
        else:
            return f"{ts}.jpg"

    def _save_compressed(self, img_bytes: bytes, path: Path):
        """读取图片字节，压缩后保存为 JPEG"""
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                # 限制最大分辨率 1920x1080
                h, w = img.shape[:2]
                if w > 1920 or h > 1080:
                    scale = min(1920 / w, 1080 / h)
                    img = cv2.resize(img, None, fx=scale, fy=scale)
                cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                return
        except ImportError:
            pass

        # OpenCV 不可用时直接保存原始字节
        path.write_bytes(img_bytes)

    # ── 存图判断 ──────────────────────────────────────

    def should_save(
        self,
        memory_type: str,
        scene_type: str,
        importance: float,
        persons: list,
        gps: Optional[dict],
        description: str = "",
        existing_count: int = 0,
    ) -> tuple[bool, str]:
        """
        判断是否应该保存图片，返回 (should_save, category)。

        存图规则：
        - space: 室内空间，每空间最多4张
        - person: 检测到人脸就存
        - event: importance >= 0.8 时存
        - outdoor: 按距离分级，坐标半径内无图就存
        """
        # 检测到人脸 → person
        if persons:
            return True, "person"

        # 室内空间
        if memory_type == "space" and scene_type == "indoor":
            if existing_count < 4:
                return True, "space"
            return False, ""

        # 室外
        if scene_type == "outdoor" and gps:
            density = self._judge_density(description)
            return True, "outdoor"

        # 高重要性事件
        if importance >= 0.8:
            return True, "event"

        return False, ""

    def _judge_density(self, description: str) -> str:
        """根据视觉描述判断场景密度"""
        desc = description.lower()

        dense_keywords = ["走廊", "车间", "办公室", "货架", "工位", "室内", "房间", "大厅"]
        open_keywords = ["街道", "广场", "空地", "田野", "马路", "公路", "开阔"]

        for kw in dense_keywords:
            if kw in desc:
                return "dense"
        for kw in open_keywords:
            if kw in desc:
                return "open"
        return "normal"

    def get_outdoor_radius(self, description: str) -> float:
        """获取室外存图距离半径（米）"""
        density = self._judge_density(description)
        return OUTDOOR_DENSITY_RADIUS.get(density, 20)

    # ── 清理 ──────────────────────────────────────────

    def cleanup(
        self,
        store=None,
        max_total: int = 1000,
    ) -> dict:
        """
        执行图片清理：
        1. person: 1个月清理（pinned 除外）
        2. event: FIFO 超过上限
        3. space: 每空间最多4张
        4. outdoor: 超过上限按时间淘汰
        5. 总量超限按时间淘汰最旧的
        """
        stats = {"deleted": 0, "kept": 0}

        if store is None:
            return stats

        now = datetime.now(timezone.utc)

        # person: 1个月清理，pinned 除外
        try:
            with sqlite3_connect(store.db_path) as conn:
                conn.execute(
                    """
                    DELETE FROM visual_memory
                    WHERE memory_type = 'person'
                    AND image_path IS NOT NULL
                    AND pinned = 0
                    AND CAST(julianday('now') - julianday(timestamp) AS INTEGER) > ?
                    """,
                    (PERSON_EXPIRE_DAYS,),
                )
                stats["deleted"] += conn.total_changes
                conn.commit()
        except Exception:
            pass

        # 检查各类别数量并淘汰
        try:
            with sqlite3_connect(store.db_path) as conn:
                for cat, limit in LIMITS.items():
                    count = conn.execute(
                        "SELECT COUNT(*) FROM visual_memory WHERE image_category = ? AND image_path IS NOT NULL",
                        (cat,),
                    ).fetchone()[0]
                    if count > limit:
                        excess = count - limit
                        old_ids = conn.execute(
                            "SELECT node_id, image_path FROM visual_memory "
                            "WHERE image_category = ? AND image_path IS NOT NULL "
                            "ORDER BY timestamp ASC LIMIT ?",
                            (cat, excess),
                        ).fetchall()
                        for node_id, img_path in old_ids:
                            self._delete_image_file(img_path)
                            conn.execute(
                                "UPDATE visual_memory SET image_path = NULL, image_category = NULL WHERE node_id = ?",
                                (node_id,),
                            )
                        stats["deleted"] += len(old_ids)
                conn.commit()
        except Exception:
            pass

        # 总量超限
        try:
            total = sum(
                len(list((self.root / cat).glob("*.jpg")))
                for cat in ("space", "event", "person", "outdoor")
            )
            if total > max_total:
                # 按修改时间排序，删除最旧的
                all_files = []
                for cat in ("space", "event", "person", "outdoor"):
                    for f in (self.root / cat).glob("*.jpg"):
                        all_files.append((f.stat().st_mtime, f))
                all_files.sort()
                to_delete = total - max_total
                for _, f in all_files[:to_delete]:
                    f.unlink(missing_ok=True)
                    stats["deleted"] += 1
        except Exception:
            pass

        return stats

    def _delete_image_file(self, rel_path: Optional[str]):
        """删除图片文件"""
        if not rel_path:
            return
        try:
            abs_path = self.root / rel_path
            if abs_path.exists():
                abs_path.unlink()
        except Exception:
            pass

    def get_image_path(self, rel_path: str) -> Optional[Path]:
        """获取图片的绝对路径"""
        if not rel_path:
            return None
        p = self.root / rel_path
        return p if p.exists() else None

    def count_images(self) -> dict:
        """统计各类别图片数量"""
        counts = {}
        for cat in ("space", "event", "person", "outdoor"):
            cat_dir = self.root / cat
            if cat_dir.exists():
                counts[cat] = len(list(cat_dir.glob("*.jpg")))
            else:
                counts[cat] = 0
        counts["total"] = sum(counts.values())
        return counts


def sqlite3_connect(db_path: str):
    """辅助：获取 sqlite3 连接"""
    import sqlite3
    return sqlite3.connect(db_path)
