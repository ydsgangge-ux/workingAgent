"""
SQLite 存储层 — 视觉记忆持久化
================================
第一版：LIKE 文本匹配（够用）
第二版 TODO：替换为向量检索（embeddings + cosine similarity）
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from .memory_schema import VisualMemory

DB_PATH = Path(__file__).parent.parent / "data" / "visual_memory.db"


class VisualMemoryStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DB_PATH)
        self._init_db()

    # ── 初始化 ────────────────────────────────────────

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS visual_memory (
                    node_id           TEXT PRIMARY KEY,
                    scene_type        TEXT,
                    memory_type       TEXT,
                    timestamp         TEXT,
                    gps               TEXT,
                    gps_accuracy      REAL,
                    indoor_coords     TEXT,
                    landmark_ref      TEXT,
                    location_confidence REAL,
                    description       TEXT,
                    objects           TEXT,
                    persons           TEXT,
                    event_summary     TEXT,
                    subjective_note   TEXT,
                    image_path        TEXT,
                    image_category    TEXT,
                    pinned            INTEGER DEFAULT 0,
                    importance        REAL,
                    vision_confidence REAL,
                    last_accessed     TEXT,
                    access_count      INTEGER DEFAULT 0
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_desc "
                "ON visual_memory(description)"
            )
            # 兼容旧表：自动添加新字段
            self._migrate_schema(conn)
            conn.commit()

    def _migrate_schema(self, conn):
        """自动添加新字段（兼容旧数据库）"""
        try:
            cur = conn.execute("PRAGMA table_info(visual_memory)")
            existing = {row[1] for row in cur.fetchall()}
            new_cols = {
                "image_path": "TEXT",
                "image_category": "TEXT",
                "pinned": "INTEGER DEFAULT 0",
                "user_id": "TEXT DEFAULT 'default'",
                "user_name": "TEXT DEFAULT ''",
            }
            for col, col_type in new_cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE visual_memory ADD COLUMN {col} {col_type}")
                    print(f"[VisualMemoryStore] 已添加字段: {col}")
        except Exception as e:
            print(f"[VisualMemoryStore] 迁移失败: {e}")

    # ── 写入 ──────────────────────────────────────────

    def insert(self, mem: VisualMemory, user_id: str = "default", user_name: str = ""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO visual_memory
                (node_id, scene_type, memory_type, timestamp, gps, gps_accuracy,
                 indoor_coords, landmark_ref, location_confidence, description,
                 objects, persons, event_summary, subjective_note,
                 image_path, image_category, pinned, importance,
                 vision_confidence, last_accessed, access_count,
                 user_id, user_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mem.node_id,
                    mem.scene_type,
                    mem.memory_type,
                    mem.timestamp,
                    json.dumps(mem.gps, ensure_ascii=False) if mem.gps else None,
                    mem.gps_accuracy,
                    json.dumps(mem.indoor_coords, ensure_ascii=False)
                    if mem.indoor_coords else None,
                    mem.landmark_ref,
                    mem.location_confidence,
                    mem.description,
                    json.dumps(mem.objects, ensure_ascii=False),
                    json.dumps(mem.persons, ensure_ascii=False),
                    mem.event_summary,
                    mem.subjective_note,
                    mem.image_path,
                    mem.image_category,
                    1 if mem.pinned else 0,
                    mem.importance,
                    mem.vision_confidence,
                    mem.last_accessed,
                    mem.access_count,
                    user_id,
                    user_name,
                ),
            )
            conn.commit()

    # ── A层检索（第一版：LIKE 文本匹配） ─────────────
    # TODO v2: 替换为向量检索（embedding + cosine）

    def search(self, query: str, top_k: int = 5) -> list:
        """
        语义检索：仅走 description 字段。
        命中后自动 +0.05 importance（模拟记忆强化）。
        """
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM visual_memory WHERE description LIKE ?"
                " ORDER BY importance DESC LIMIT ?",
                (f"%{query}%", top_k),
            )
            rows = self._fetch_rows(cur)

        for row in rows:
            self._bump_importance(row["node_id"])

        return rows

    # ── 重要性衰减（每30天未命中 -0.1） ─────────────

    def decay_importance(self, threshold_days: int = 30):
        """
        模拟人类遗忘：超过 threshold_days 未被检索则 -0.1。
        建议通过 QTimer 每 2 小时调用一次。
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE visual_memory
                SET importance = MAX(importance - 0.1, 0.0)
                WHERE last_accessed IS NULL
                   OR CAST(julianday('now') - julianday(last_accessed) AS INTEGER) > ?
                """,
                (threshold_days,),
            )
            conn.commit()

    # ── 按类型清理 ──────────────────────────────────

    def cleanup(self):
        """
        按记忆类型执行生命周期清理：

        space:   长期保留，同一描述只留最新1条
        person:  长期保留，同一人只留最近3条
        event:   短期记忆，24h内 importance<0.3 删除，7天 importance<0.5 删除
        interest: 中期保留，30天 importance<0.4 删除
        """
        with sqlite3.connect(self.db_path) as conn:
            # event: 24小时内低重要性
            conn.execute(
                "DELETE FROM visual_memory WHERE memory_type = 'event' "
                "AND importance < 0.3 "
                "AND CAST(julianday('now') - julianday(timestamp) AS FLOAT) * 24 > 24"
            )
            # event: 7天低重要性
            conn.execute(
                "DELETE FROM visual_memory WHERE memory_type = 'event' "
                "AND importance < 0.5 "
                "AND CAST(julianday('now') - julianday(timestamp) AS INTEGER) > 7"
            )
            # interest: 30天低重要性
            conn.execute(
                "DELETE FROM visual_memory WHERE memory_type = 'interest' "
                "AND importance < 0.4 "
                "AND CAST(julianday('now') - julianday(timestamp) AS INTEGER) > 30"
            )
            # space: 同一描述只保留最新1条（保留高importance的）
            conn.execute(
                "DELETE FROM visual_memory WHERE memory_type = 'space' "
                "AND node_id NOT IN ("
                "  SELECT node_id FROM visual_memory s "
                "  WHERE s.memory_type = 'space' "
                "  GROUP BY description ORDER BY timestamp DESC LIMIT 1"
                ")"
            )
            # person: 同一人只保留最近3条
            person_rows = conn.execute(
                "SELECT persons, node_id, timestamp FROM visual_memory "
                "WHERE memory_type = 'person' ORDER BY timestamp DESC"
            ).fetchall()
            seen_persons = {}
            for persons_json, node_id, ts in person_rows:
                try:
                    persons = json.loads(persons_json) if isinstance(persons_json, str) else (persons_json or [])
                except Exception:
                    continue
                for p in persons:
                    pid = p.get("id", p.get("name", ""))
                    if not pid:
                        continue
                    seen_persons.setdefault(pid, []).append(node_id)
            for pid, node_ids in seen_persons.items():
                if len(node_ids) > 3:
                    for nid in node_ids[3:]:
                        conn.execute(
                            "DELETE FROM visual_memory WHERE node_id = ?", (nid,)
                        )
            # 重要性归零的统统删除
            conn.execute(
                "DELETE FROM visual_memory WHERE importance <= 0.0"
            )
            conn.commit()
            deleted = conn.total_changes
        return deleted

    def count_by_type(self) -> dict:
        """按 memory_type 统计数量"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT memory_type, COUNT(*) FROM visual_memory "
                "GROUP BY memory_type"
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    # ── 内部工具 ─────────────────────────────────────

    def _bump_importance(self, node_id: str):
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE visual_memory
                SET importance = MIN(importance + 0.05, 1.0),
                    last_accessed = ?,
                    access_count = access_count + 1
                WHERE node_id = ?
                """,
                (now, node_id),
            )
            conn.commit()

    @staticmethod
    def _fetch_rows(cur) -> list:
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── 查询辅助 ────────────────────────────────────

    def get_by_node_id(self, node_id: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM visual_memory WHERE node_id = ?", (node_id,)
            )
            rows = self._fetch_rows(cur)
            return rows[0] if rows else None

    def get_recent(self, limit: int = 10) -> list:
        """按时间倒序取最近 N 条视觉记忆。"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM visual_memory ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return self._fetch_rows(cur)

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM visual_memory").fetchone()[0]

    # ── 室外按坐标查询 ──────────────────────────────

    def query_nearby(
        self,
        lat: float,
        lng: float,
        radius_meters: float = 20.0,
        limit: int = 5,
    ) -> list:
        """
        查询 GPS 坐标半径内的视觉记忆。
        先用经纬度粗筛（约 1度≈111km），再精确计算距离。
        """
        # 粗筛：1度≈111km，radius_meters 对应的经纬度范围
        delta = radius_meters / 111000.0
        lat_min, lat_max = lat - delta, lat + delta
        lng_min, lng_max = lng - delta, lng + delta

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                SELECT * FROM visual_memory
                WHERE gps IS NOT NULL
                AND memory_type IN ('space', 'event', 'outdoor')
                ORDER BY timestamp DESC
                """
            )
            all_rows = self._fetch_rows(cur)

        # 精确过滤
        import math
        results = []
        for row in all_rows:
            try:
                gps = json.loads(row["gps"]) if isinstance(row["gps"], str) else row["gps"]
                if not gps:
                    continue
                rlat, rlng = gps.get("lat", 0), gps.get("lng", 0)
                if not (lat_min <= rlat <= lat_max and lng_min <= rlng <= lng_max):
                    continue
                # 精确距离
                R = 6371000
                phi1, phi2 = math.radians(lat), math.radians(rlat)
                dphi = math.radians(rlat - lat)
                dlam = math.radians(rlng - lng)
                a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
                dist = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                if dist <= radius_meters:
                    row["_distance_m"] = round(dist, 1)
                    results.append(row)
            except Exception:
                continue

        results.sort(key=lambda r: r.get("_distance_m", 9999))
        return results[:limit]

    # ── 人像 pinned 管理 ────────────────────────────

    def pin_person(self, person_name: str, pinned: bool = True):
        """标记/取消标记某人的图片为永久保存"""
        with sqlite3.connect(self.db_path) as conn:
            # 查找包含该人的记录
            rows = conn.execute(
                "SELECT node_id, persons FROM visual_memory WHERE memory_type = 'person'"
            ).fetchall()
            node_ids = []
            for node_id, persons_json in rows:
                try:
                    persons = json.loads(persons_json) if isinstance(persons_json, str) else (persons_json or [])
                    for p in persons:
                        if p.get("name") == person_name or p.get("id") == person_name:
                            node_ids.append(node_id)
                            break
                except Exception:
                    continue

            for nid in node_ids:
                conn.execute(
                    "UPDATE visual_memory SET pinned = ? WHERE node_id = ?",
                    (1 if pinned else 0, nid),
                )
            conn.commit()
            return len(node_ids)

    def count_space_images(self, description_keyword: str = "") -> int:
        """统计同一空间描述的图片数量"""
        with sqlite3.connect(self.db_path) as conn:
            if description_keyword:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM visual_memory WHERE memory_type = 'space' AND image_path IS NOT NULL AND description LIKE ?",
                    (f"%{description_keyword}%",),
                )
            else:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM visual_memory WHERE memory_type = 'space' AND image_path IS NOT NULL"
                )
            return cur.fetchone()[0]
