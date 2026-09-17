"""
emotion_bridge.py — 情绪状态映射层

将 AGI-DPA 内部情绪状态（字符串/数值）映射为 VRM BlendShape 参数。
主程序只需调用 translate(emotion_key) 即可获取 (表情名, 强度)。
"""

EMOTION_MAP: dict[str, tuple[str, float]] = {
    # AGI-DPA 内部状态  ->  (VRM 表情名,  强度 0~1)
    "happy":        ("happy",     1.0),
    "excited":      ("happy",     0.8),
    "curious":      ("surprised", 0.5),
    "thinking":     ("neutral",   0.3),
    "sad":          ("sad",       0.7),
    "angry":        ("angry",     0.6),
    "surprised":    ("surprised", 1.0),
    "surprise":     ("surprised", 1.0),
    "neutral":      ("neutral",   1.0),
    "calm":         ("neutral",   0.8),
    "anticipation": ("happy",     0.4),
    "love":         ("happy",     0.9),
    "gratitude":    ("happy",     0.7),
    "pride":        ("happy",     0.6),
    "confused":     ("surprised", 0.4),
    "anxious":      ("sad",       0.5),
    "bored":        ("neutral",   0.5),
    "nostalgic":    ("sad",       0.3),
    "trust":        ("neutral",   0.7),
    "shame":        ("sad",       0.6),
}


def translate(emotion_key: str, intensity: float = 1.0) -> tuple[str, float]:
    """
    将 AGI-DPA 情绪 key 映射为 VRM 表情参数。

    Args:
        emotion_key: 情绪字符串，如 "happy", "sad" 等
        intensity:   原始情绪强度 0~1

    Returns:
        (vrm_expression_name, vrm_intensity) 元组
    """
    name, base = EMOTION_MAP.get(emotion_key.lower(), ("neutral", 1.0))
    return name, base * intensity
