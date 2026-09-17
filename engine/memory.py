"""
分层记忆系统
SQLite 存储 + 余弦相似度向量检索
可替换为 ChromaDB：pip install chromadb 后修改 MemoryStore 类
"""

import sqlite3
from engine.db_guard import guarded_connect
import json
import math
import hashlib
import uuid
import os
import threading
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path

# numpy 用于向量化检索（BLOB 解码 + 矩阵相似度），缺失时降级到纯 Python
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None

from engine.models import (
    MemoryNode, MemoryModality, MemoryLevel,
    EmotionState, EmotionType
)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """余弦相似度（纯 Python，向量化路径不可用时的回退）"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── embedding BLOB 编解码 ─────────────────────
def _encode_embedding_blob(vec: Optional[List[float]]) -> Optional[bytes]:
    """List[float] → numpy float32 bytes（存储用）"""
    if not vec or not _HAS_NUMPY:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def _decode_embedding_blob(blob: Optional[bytes]) -> Optional[List[float]]:
    """numpy float32 bytes → List[float]（读取用，跳过 JSON 解析开销）"""
    if not blob:
        return None
    if _HAS_NUMPY:
        return np.frombuffer(blob, dtype=np.float32).tolist()
    # 纯 Python 回退（numpy 不可用时）
    import struct
    count = len(blob) // 4
    return list(struct.unpack(f'{count}f', blob))


# ── 向量模型：自动检测可用方案 ─────────────────────
_embedding_model = None
_embedding_mode  = "hash"   # "transformer" | "hash"

def _init_embedding():
    """启动时检测 sentence-transformers，有则使用，没有降级到哈希"""
    global _embedding_model, _embedding_mode
    try:
        # 先测试 torch 是否能正常加载（Python 3.14 下 DLL 可能失败）
        import importlib
        try:
            import torch  # noqa: F401
        except Exception:
            raise ImportError("torch 不可用，跳过 sentence-transformers")

        # 设置国内镜像（避免 huggingface.co 被墙导致下载失败）
        if "HF_ENDPOINT" not in os.environ:
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        from sentence_transformers import SentenceTransformer
        model_name = "paraphrase-multilingual-MiniLM-L12-v2"
        _embedding_model = SentenceTransformer(model_name)
        _embedding_mode  = "transformer"
        print(f"[OK] 语义向量：sentence-transformers ({model_name})")
    except ImportError:
        _embedding_mode = "hash"
        # 静默降级，不打印警告（用户不需要知道）
    except Exception:
        _embedding_mode = "hash"
        # DLL 错误等静默处理，不打印堆栈

# 懒加载：第一次使用 embedding 时才初始化，不在 import 时执行
_embedding_initialized = False

def _ensure_embedding():
    global _embedding_initialized
    if not _embedding_initialized:
        _embedding_initialized = True
        try:
            _init_embedding()
        except Exception as e:
            global _embedding_mode
            _embedding_mode = "hash"
            print(f"[Embedding] 初始化失败，降级到哈希模式: {e}")


def get_embedding(text: str, dim: int = 384) -> List[float]:
    _ensure_embedding()
    """
    获取文本向量
    有 sentence-transformers → 真实语义向量（dim=384）
    无 → 哈希向量（dim=128，仅供关键词匹配用）
    """
    if _embedding_mode == "transformer" and _embedding_model is not None:
        try:
            vec = _embedding_model.encode(text, normalize_embeddings=True)
            return vec.tolist()
        except Exception:
            pass   # 降级

    # 哈希散射向量（dim=128）
    actual_dim = 128
    vec = [0.0] * actual_dim
    for i, char in enumerate(text):
        idx = (ord(char) * 2654435761 + i * 40503) % actual_dim
        vec[idx] += 1.0 / (1 + i * 0.1)
    words = text.lower().split()
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        for j in range(4):
            idx = (h >> (j * 8)) % actual_dim
            vec[idx] += 0.5
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


# 向下兼容旧代码
def simple_embedding(text: str, dim: int = 128) -> List[float]:
    return get_embedding(text, dim)


class MemoryStore:
    """
    记忆存储核心
    SQLite 实现，接口设计与 ChromaDB 兼容
    """

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = db_path
        # 向量检索内存缓存：(key, ids, matrix, meta_arrays)
        # key = (level, user_id) 或 None 表示全部
        # 每次 add() 后失效，下次检索时惰性重建
        self._vec_cache = None
        self._vec_cache_lock = threading.Lock()
        self._init_db()

    def _migrate_embeddings_to_blob(self) -> int:
        """
        把 embedding_json 批量转成 embedding_blob（仅迁移缺失的行）
        幂等：已迁移的行不会被重复处理
        返回迁移的行数
        """
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, embedding_json FROM memories "
                "WHERE embedding_blob IS NULL AND embedding_json IS NOT NULL"
            ).fetchall()
            if not rows:
                return 0
            migrated = 0
            for mid, ej in rows:
                try:
                    vec = json.loads(ej)
                    if not vec:
                        continue
                    blob = _encode_embedding_blob(vec)
                    if blob:
                        conn.execute(
                            "UPDATE memories SET embedding_blob=? WHERE id=?",
                            (blob, mid)
                        )
                        migrated += 1
                except Exception:
                    continue
            conn.commit()
        if migrated:
            print(f"[memory] embedding_blob 迁移完成：{migrated} 条")
        return migrated

    def _init_db(self):
        with guarded_connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    level TEXT NOT NULL,
                    emotion_json TEXT NOT NULL,
                    importance REAL NOT NULL,
                    tags_json TEXT,
                    associations_json TEXT,
                    source TEXT,
                    embedding_json TEXT,
                    created_at TEXT,
                    last_accessed TEXT,
                    access_count INTEGER DEFAULT 0,
                    decay_factor REAL DEFAULT 1.0,
                    user_id TEXT DEFAULT 'default'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id TEXT PRIMARY KEY,
                    user_input TEXT,
                    emotion_json TEXT,
                    memory_ids_json TEXT,
                    reasoning TEXT,
                    response TEXT,
                    storage_decision_json TEXT,
                    timestamp TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_modality ON memories(modality)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_level ON memories(level)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_importance ON memories(importance)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON memories(user_id)")
            conn.commit()

        # 迁移：旧数据库没有 user_id 列时自动添加
        with guarded_connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
            if "user_id" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN user_id TEXT DEFAULT 'default'")
                conn.commit()
            if "user_name" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN user_name TEXT DEFAULT ''")
                conn.commit()

        # 迁移：新增 embedding_blob 列（二进制向量存储，加速检索）
        # 旧列 embedding_json 保留不动，作为回滚保险
        with guarded_connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
            if "embedding_blob" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN embedding_blob BLOB")
                conn.commit()

        # 批量迁移：把已有 embedding_json 转成 embedding_blob（仅迁移缺失的行）
        self._migrate_embeddings_to_blob()

        # 迁移：interactions 表添加 user_id 列 + 索引
        with guarded_connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(interactions)").fetchall()]
            if "user_id" not in cols:
                conn.execute("ALTER TABLE interactions ADD COLUMN user_id TEXT DEFAULT 'default'")
                conn.commit()
            if "user_name" not in cols:
                conn.execute("ALTER TABLE interactions ADD COLUMN user_name TEXT DEFAULT ''")
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_time ON interactions(timestamp)")

    def add(self, node: MemoryNode, user_id: str = "default", user_name: str = "") -> str:
        """添加记忆节点"""
        if not node.id:
            node.id = str(uuid.uuid4())
        if node.embedding is None:
            node.embedding = get_embedding(node.content)

        # 同时写 embedding_json（回滚保险）和 embedding_blob（快速检索）
        embedding_blob = _encode_embedding_blob(node.embedding)

        with guarded_connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO memories
                (id, content, modality, level, emotion_json, importance,
                 tags_json, associations_json, source, embedding_json,
                 created_at, last_accessed, access_count, decay_factor,
                 user_id, user_name, embedding_blob)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                node.id, node.content, node.modality.value, node.level.value,
                json.dumps(node.emotion.to_dict()),
                node.importance,
                json.dumps(node.tags),
                json.dumps(node.associations),
                node.source,
                json.dumps(node.embedding),
                node.created_at, node.last_accessed,
                node.access_count, node.decay_factor,
                user_id, user_name, embedding_blob
            ))
            conn.commit()

        # 失效向量缓存（下次检索时惰性重建）
        self._invalidate_vec_cache()
        return node.id

    def get(self, memory_id: str) -> Optional[MemoryNode]:
        """按ID获取记忆"""
        with guarded_connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
        return self._row_to_node(row) if row else None

    def search(
        self,
        query: str,
        top_k: int = 5,
        modality: Optional[MemoryModality] = None,
        level: Optional[MemoryLevel] = None,
        min_importance: float = 0.0,
        emotion_filter: Optional[EmotionType] = None,
        user_id: Optional[str] = None
    ) -> List[Tuple[MemoryNode, float]]:
        """
        语义检索记忆
        返回 (节点, 相似度分数) 列表，按分数降序
        user_id=None 时检索所有用户（兼容旧行为）

        检索路径：
          1. 优先走向量化快速路径（numpy 矩阵乘法，O(n) 但在 C 层执行）
          2. numpy 不可用 或 BLOB 缺失时，回退到原始线性扫描
        """
        query_vec = get_embedding(query)

        # 优先走向量化快速路径
        if _HAS_NUMPY:
            results = self._search_vectorized(
                query_vec, top_k, modality, level, min_importance,
                emotion_filter, user_id
            )
            if results is not None:
                return results

        # 回退：原始线性扫描
        return self._search_linear(
            query_vec, top_k, modality, level, min_importance,
            emotion_filter, user_id
        )

    def _search_vectorized(
        self,
        query_vec: List[float],
        top_k: int,
        modality: Optional[MemoryModality],
        level: Optional[MemoryLevel],
        min_importance: float,
        emotion_filter: Optional[EmotionType],
        user_id: Optional[str]
    ) -> Optional[List[Tuple[MemoryNode, float]]]:
        """
        向量化检索：numpy 矩阵乘法替代逐条 Python 循环
        返回 None 表示无法走快速路径（调用方应回退到线性扫描）
        """
        conditions = ["decay_factor > 0.1", "embedding_blob IS NOT NULL"]
        params: list = []
        if modality:
            conditions.append("modality=?")
            params.append(modality.value)
        if level:
            conditions.append("level=?")
            params.append(level.value)
        if min_importance > 0:
            conditions.append("importance>=?")
            params.append(min_importance)
        if user_id is not None:
            conditions.append("(user_id=? OR user_id='default' OR user_id='system')")
            params.append(user_id)
        where = " AND ".join(conditions)

        # 显式列名查询（列顺序与 SELECT * 一致，_row_to_node 可直接复用）
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT id, content, modality, level, emotion_json, importance, "
                f"tags_json, associations_json, source, embedding_json, "
                f"created_at, last_accessed, access_count, decay_factor, "
                f"user_id, user_name, embedding_blob "
                f"FROM memories WHERE {where}", params
            ).fetchall()

        if not rows:
            return []

        # 查询向量维度
        q_dim = len(query_vec)

        # 按维度分组（DB 中可能混有 128维 hash 和 384维 transformer 向量）
        # 只处理与查询向量同维度的记忆（不同维度无法计算余弦相似度）
        matching_rows = []
        all_vecs = []
        for r in rows:
            try:
                vec = np.frombuffer(r[16], dtype=np.float32)
                if len(vec) == q_dim:
                    matching_rows.append(r)
                    all_vecs.append(vec.copy())
            except Exception:
                continue

        if not matching_rows:
            return []
        rows = matching_rows

        # 构建 numpy 归一化向量矩阵
        try:
            matrix = np.vstack(all_vecs)
        except Exception:
            return None  # BLOB 数据异常，回退到线性扫描

        # 归一化查询向量
        q = np.asarray(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm > 0:
            q = q / q_norm

        # 归一化记忆向量
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix_norm = matrix / norms

        # 一次矩阵乘法算出全部余弦相似度（替代逐条 Python 循环）
        sims = matrix_norm @ q  # shape: (n,)

        # 向量化评分：score = sim * importance * decay * (1 + intensity * 0.3)
        importances = np.array([r[5] for r in rows], dtype=np.float32)
        decays = np.array([r[13] for r in rows], dtype=np.float32)

        # 解析情绪数据（轻量 JSON，比 embedding_json 小得多）
        intensities = np.zeros(len(rows), dtype=np.float32)
        primaries: List[str] = []
        for i, r in enumerate(rows):
            try:
                emo = json.loads(r[4])
                intensities[i] = emo.get("intensity", 0.0)
                primaries.append(emo.get("primary", "neutral"))
            except Exception:
                primaries.append("neutral")

        emotion_bonus = 1.0 + intensities * 0.3
        scores = sims * importances * decays * emotion_bonus

        # 情绪过滤降权
        if emotion_filter:
            filter_val = emotion_filter.value
            for i, p in enumerate(primaries):
                if p != filter_val:
                    scores[i] *= 0.5

        # 取 top-k（argsort 一次完成排序）
        top_indices = np.argsort(-scores)[:top_k]

        # 仅为 top-k 构造 MemoryNode（避免全量构造的开销）
        scored: List[Tuple[MemoryNode, float]] = []
        for idx in top_indices:
            r = rows[idx]
            node = self._row_to_node(r)
            if node:
                scored.append((node, float(scores[idx])))

        return scored

    def _search_linear(
        self,
        query_vec: List[float],
        top_k: int,
        modality: Optional[MemoryModality],
        level: Optional[MemoryLevel],
        min_importance: float,
        emotion_filter: Optional[EmotionType],
        user_id: Optional[str]
    ) -> List[Tuple[MemoryNode, float]]:
        """原始线性扫描（numpy 不可用 或 BLOB 缺失时的回退路径）"""
        conditions = ["decay_factor > 0.1"]
        params: list = []
        if modality:
            conditions.append("modality=?")
            params.append(modality.value)
        if level:
            conditions.append("level=?")
            params.append(level.value)
        if min_importance > 0:
            conditions.append("importance>=?")
            params.append(min_importance)
        if user_id is not None:
            conditions.append("(user_id=? OR user_id='default' OR user_id='system')")
            params.append(user_id)
        where = " AND ".join(conditions)

        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {where}", params
            ).fetchall()

        scored: List[Tuple[MemoryNode, float]] = []
        for row in rows:
            node = self._row_to_node(row)
            if node and node.embedding:
                sim = cosine_similarity(query_vec, node.embedding)
                # 综合分数 = 语义相似度 * 重要性权重 * 衰减系数 * 情绪加成
                emotion_bonus = 1.0 + node.emotion.intensity * 0.3
                score = sim * node.effective_importance() * emotion_bonus
                if emotion_filter and node.emotion.primary != emotion_filter:
                    score *= 0.5
                scored.append((node, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _invalidate_vec_cache(self):
        """失效向量缓存（add/delete 后调用，预留扩展接口）"""
        self._vec_cache = None

    def get_recent(
        self,
        top_k: int = 6,
        level: Optional[MemoryLevel] = None,
        user_id: Optional[str] = None
    ) -> List[MemoryNode]:
        """按时间倒序获取最近存储的记忆（不依赖语义匹配）"""
        conditions = []
        params: list = []
        if level:
            conditions.append("level=?")
            params.append(level.value)
        if user_id:
            conditions.append("user_id=?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM memories {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*params, top_k)
            ).fetchall()
        return [self._row_to_node(r) for r in rows if r]

    def log_interaction(
        self,
        user_input: str,
        response: str,
        user_id: str = "default",
        user_name: str = "",
    ):
        """记录每轮对话到 interactions 表（启动恢复用）"""
        with guarded_connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO interactions (id, user_input, response, timestamp, user_id, user_name) "
                "VALUES (?,?,?,?,?,?)",
                (
                    str(uuid.uuid4())[:12],
                    user_input[:2000],
                    response[:2000],
                    datetime.now().isoformat(),
                    user_id,
                    user_name,
                )
            )
            conn.commit()

    def get_recent_interactions(
        self, limit: int = 10, user_id: str = "default"
    ) -> List[tuple]:
        """取最近 N 条对话记录（时间倒序），用于启动时恢复上下文"""
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT user_input, response FROM interactions "
                "WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        return rows

    def get_by_date_range(
        self,
        start_date: str,
        end_date: str,
        level: Optional[MemoryLevel] = None,
        user_id: Optional[str] = None,
        top_k: int = 30,
    ) -> List[MemoryNode]:
        """按日期范围查询记忆（按时间倒序）"""
        conditions = ["created_at >= ?", "created_at <= ?"]
        params: list = [start_date, end_date]
        if level:
            conditions.append("level=?")
            params.append(level.value)
        if user_id:
            conditions.append("user_id=?")
            params.append(user_id)
        where = " AND ".join(conditions)
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*params, top_k)
            ).fetchall()
        return [self._row_to_node(r) for r in rows if r]

    def get_interactions_by_date_range(
        self,
        start_date: str,
        end_date: str,
        user_id: Optional[str] = None,
        limit: int = 30,
    ) -> List[tuple]:
        """按日期范围查询 interactions 表原始对话文本

        用于"XX号我们聊了什么"式话题回溯。
        返回 [(user_input, response, timestamp), ...] 按时间倒序。
        """
        conditions = ["timestamp >= ?", "timestamp <= ?"]
        params: list = [start_date, end_date]
        if user_id:
            conditions.append("user_id=?")
            params.append(user_id)
        where = " AND ".join(conditions)
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT user_input, response, timestamp FROM interactions "
                f"WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                (*params, limit)
            ).fetchall()
        return rows

    def search_by_level(
        self, query: str, level: MemoryLevel, top_k: int = 3,
        user_id: Optional[str] = None
    ) -> List[Tuple[MemoryNode, float]]:
        """按层级搜索"""
        return self.search(query, top_k=top_k, level=level, user_id=user_id)

    def get_siblings(self, summary_node_id: str) -> Dict[str, Optional[MemoryNode]]:
        """
        通过大纲节点ID，定向获取同一记忆的细纲和细节
        大纲ID格式：xxxx_summary → 找 xxxx_outline 和 xxxx_detail
        这是「大纲命中→精准展开」的核心方法
        """
        base_id = summary_node_id.replace("_summary", "").replace("_outline", "").replace("_detail", "")
        result = {"outline": None, "detail": None}
        for level_suffix in ("_outline", "_detail"):
            node = self.get(base_id + level_suffix)
            if node:
                key = level_suffix.lstrip("_")
                result[key] = node
        return result

    def get_by_base_ids(self, base_ids: List[str],
                        levels: List[str] = None) -> List[MemoryNode]:
        """
        批量按 base_id 前缀获取记忆节点
        用于大纲命中后批量拉取对应细纲/细节
        """
        if not base_ids:
            return []
        levels = levels or ["outline", "detail"]
        target_ids = []
        for bid in base_ids:
            clean = bid.replace("_summary","").replace("_outline","").replace("_detail","")
            for lv in levels:
                target_ids.append(f"{clean}_{lv}")

        if not target_ids:
            return []

        placeholders = ",".join("?" * len(target_ids))
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders})",
                target_ids
            ).fetchall()
        nodes = [self._row_to_node(r) for r in rows]
        return [n for n in nodes if n is not None]

    def update_access(self, memory_id: str):
        """更新访问记录，同时回升衰减系数（被想起的记忆更难遗忘）"""
        with guarded_connect(self.db_path) as conn:
            conn.execute("""
                UPDATE memories
                SET last_accessed=?,
                    access_count=access_count+1,
                    decay_factor=MIN(1.0, decay_factor * 1.05)
                WHERE id=?
            """, (datetime.now().isoformat(), memory_id))
            conn.commit()

    def apply_decay(self, decay_rate: float = 0.995):
        """
        记忆衰减 - 模拟遗忘曲线
        三重保护：重要性高 + 情绪强烈 + 最近访问过 → 衰减更慢
        被检索时 update_access 已让 decay_factor 回升5%
        """
        from datetime import datetime as _dt
        now = _dt.now()
        with guarded_connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, importance, decay_factor, emotion_json, last_accessed FROM memories"
            ).fetchall()

            for row in rows:
                mid, importance, decay, emotion_json, last_accessed = row
                emotion = EmotionState.from_dict(json.loads(emotion_json))

                # 重要性 + 情绪保护
                protection = importance * 0.5 + emotion.intensity * 0.3

                # 最近访问保护：7天内访问过的记忆额外减缓衰减
                if last_accessed:
                    try:
                        days_ago = (now - _dt.fromisoformat(last_accessed)).days
                        if days_ago <= 7:
                            protection = min(1.0, protection + 0.2 * (1 - days_ago / 7))
                    except Exception:
                        pass

                actual_decay = decay_rate + protection * (1 - decay_rate) * 0.8
                new_decay = max(0.05, decay * actual_decay)
                conn.execute(
                    "UPDATE memories SET decay_factor=? WHERE id=?",
                    (new_decay, mid)
                )
            conn.commit()

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆库统计"""
        with guarded_connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            by_level = conn.execute(
                "SELECT level, COUNT(*) FROM memories GROUP BY level"
            ).fetchall()
            by_modality = conn.execute(
                "SELECT modality, COUNT(*) FROM memories GROUP BY modality"
            ).fetchall()
            avg_importance = conn.execute(
                "SELECT AVG(importance) FROM memories"
            ).fetchone()[0]

        return {
            "total": total,
            "by_level": dict(by_level),
            "by_modality": dict(by_modality),
            "avg_importance": round(avg_importance or 0, 3)
        }

    def _row_to_node(self, row) -> Optional[MemoryNode]:
        if not row:
            return None
        try:
            # 优先用二进制 BLOB（np.frombuffer，跳过 JSON 解析）
            # 回退到 embedding_json（兼容旧数据 / 回滚场景）
            embedding = None
            blob = row[16] if len(row) > 16 else None
            if blob:
                embedding = _decode_embedding_blob(blob)
            elif row[9]:
                embedding = json.loads(row[9])
            return MemoryNode(
                id=row[0], content=row[1],
                modality=MemoryModality(row[2]),
                level=MemoryLevel(row[3]),
                emotion=EmotionState.from_dict(json.loads(row[4])),
                importance=row[5],
                tags=json.loads(row[6] or "[]"),
                associations=json.loads(row[7] or "[]"),
                source=row[8] or "conversation",
                embedding=embedding,
                created_at=row[10], last_accessed=row[11],
                access_count=row[12], decay_factor=row[13]
            )
        except Exception:
            return None


class HierarchicalMemoryManager:
    """
    分层记忆管理器
    实现 大纲→细纲→细节 的渐进式检索
    """

    def __init__(self, store: MemoryStore):
        self.store = store

    def hierarchical_search(
        self, query: str, max_detail: int = 2
    ) -> Dict[str, List[Tuple[MemoryNode, float]]]:
        """
        分层检索：先查大纲，再按需展开
        返回各层级的检索结果
        """
        results = {}

        # 第一步：查大纲（最快，最少内容）
        summaries = self.store.search_by_level(query, MemoryLevel.SUMMARY, top_k=5)
        results["summary"] = summaries

        # 第二步：如果大纲相关，查细纲
        if summaries and summaries[0][1] > 0.3:
            outlines = self.store.search_by_level(query, MemoryLevel.OUTLINE, top_k=4)
            results["outline"] = outlines

            # 第三步：如果细纲高度相关，才展开细节
            if outlines and outlines[0][1] > 0.5:
                details = self.store.search_by_level(query, MemoryLevel.DETAIL, top_k=max_detail)
                results["detail"] = details

        return results

    def store_with_hierarchy(
        self,
        content: str,
        modality: MemoryModality,
        emotion: EmotionState,
        importance: float,
        tags: List[str] = None,
        source: str = "conversation"
    ) -> Dict[str, str]:
        """
        按重要性和情绪强度决定存储层级
        返回各层存储的节点ID
        """
        stored_ids = {}
        base_id = str(uuid.uuid4())[:8]

        # 规则：情绪强烈或重要性高 → 细节层
        if emotion.is_strong() or importance >= 0.8:
            node = MemoryNode(
                id=f"{base_id}_detail",
                content=content,
                modality=modality,
                level=MemoryLevel.DETAIL,
                emotion=emotion,
                importance=importance,
                tags=tags or [],
                source=source
            )
            stored_ids["detail"] = self.store.add(node)

        # 中等重要性 → 细纲层（摘要版本）
        if importance >= 0.4 or emotion.is_moderate():
            summary_content = content[:200] + "..." if len(content) > 200 else content
            node = MemoryNode(
                id=f"{base_id}_outline",
                content=summary_content,
                modality=modality,
                level=MemoryLevel.OUTLINE,
                emotion=emotion,
                importance=importance,
                tags=tags or [],
                source=source
            )
            stored_ids["outline"] = self.store.add(node)

        # 所有内容都存大纲（只存关键词/主题）
        keywords = " ".join(tags) if tags else content[:80]
        node = MemoryNode(
            id=f"{base_id}_summary",
            content=keywords,
            modality=modality,
            level=MemoryLevel.SUMMARY,
            emotion=emotion,
            importance=importance,
            tags=tags or [],
            source=source
        )
        stored_ids["summary"] = self.store.add(node)

        return stored_ids

    @staticmethod
    def _user_tag(node) -> str:
        if hasattr(node, 'user_name') and node.user_name:
            return f" [user: {node.user_name}]"
        if hasattr(node, 'user_id') and node.user_id:
            return f" [userID: {node.user_id}]"
        return ""

    def format_for_prompt(
        self, results: Dict[str, List[Tuple[MemoryNode, float]]]
    ) -> str:
        """将检索结果格式化为 A 层可用的 prompt 片段"""
        if not results:
            return "（无相关记忆）"

        lines = ["【相关记忆】"]

        if "summary" in results and results["summary"]:
            lines.append("\n[大纲级]")
            for node, score in results["summary"][:3]:
                ut = self._user_tag(node)
                lines.append(f"  · {node.content} (重要性:{node.importance:.1f}){ut}")

        if "outline" in results and results["outline"]:
            lines.append("\n[细纲级]")
            for node, score in results["outline"][:2]:
                emotion_desc = f"{node.emotion.primary.value}({node.emotion.intensity:.1f})"
                ut = self._user_tag(node)
                lines.append(f"  · {node.content[:100]} [情绪:{emotion_desc}]{ut}")

        if "detail" in results and results["detail"]:
            lines.append("\n[细节级]")
            for node, score in results["detail"][:1]:
                ut = self._user_tag(node)
                lines.append(f"  · {node.content[:300]}{ut}")

        return "\n".join(lines)
