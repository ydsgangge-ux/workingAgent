"""
多模态模型分析图片时使用的系统提示词。
根据不同场景使用不同 prompt，均在代码中降级兜底：
- 优先尝试 JSON 提取（GPT-4o/Gemini）
- 失败则降级为纯文本描述（GLM-4V 等）
"""

# ── 标准场景理解（event / 通用） ─────────────────────

VISION_PROMPT_STANDARD = """分析这张图片，严格按以下JSON格式输出，不要输出任何其他内容：
{
  "description": "客观描述场景内容，30字以内，用于检索",
  "objects": [{"label": "物体名称", "position": "位置描述", "state": "当前状态"}],
  "persons": [{"name": "未知或已知姓名", "action": "正在做什么"}],
  "event_summary": "本图发生了什么，一句话动态描述",
  "vision_confidence": 0到1之间的浮点数
}
如果图像模糊、光线不足、严重遮挡，vision_confidence 填 0.6 以下。"""

# ── 空间记录（space 类型） ──────────────────────────

VISION_PROMPT_SPACE = """这是一张空间记录照片，分析环境全貌，按以下JSON格式输出：
{
  "description": "空间客观描述，包含主要物体和空间关系，50字以内",
  "objects": [{"label": "物体名称", "position": "在空间中的位置", "state": "状态"}],
  "spatial_relations": "主要物体之间的空间关系描述",
  "vision_confidence": 0到1之间的浮点数
}"""

# ── 人物识别（person 类型） ─────────────────────────

VISION_PROMPT_PERSON = """这是一张人物识别照片，按以下JSON格式输出：
{
  "description": "人物客观描述，包含外貌特征和当前动作，30字以内",
  "appearance": {"gender": "男/女/不确定", "age_range": "大致年龄段", "clothing": "服装描述"},
  "action": "当前在做什么",
  "vision_confidence": 0到1之间的浮点数
}"""

# ── 简易降级（模型不支持 JSON 时用） ───────────────

FALLBACK_PROMPT = "用一句话描述这张图片的内容，只说事实。"
