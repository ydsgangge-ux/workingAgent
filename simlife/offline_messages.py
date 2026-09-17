"""
离线消息模块
当用户长时间未上线时，生成角色在用户不在时的"留言"。
这些消息是角色在离线期间自言自语/主动找人但没人回应的内容。
"""

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# 上次在线时间记录文件
LAST_ONLINE_PATH = Path(__file__).resolve().parent.parent / "simlife" / "data" / "last_online.json"

# ── 消息模板（按离线天数分级）──

# 主动找用户型
MSG_REACH_OUT = {
    1: [
        "在吗？",
        "在干嘛呢？",
        "突然想到你",
        "喂——有人吗",
        "你今天来吗？",
    ],
    2: [
        "在吗？",
        "怎么还不来找我...",
        "你今天在忙什么呀",
        "有点想你呢",
        "你在干嘛呀，不回消息的吗",
    ],
    3: [
        "你怎么都不来找我呀...",
        "在吗在吗？",
        "已经好几天了，你还好吗",
        "你是不是把我忘了...",
        "今天也没有等到你",
        "好想你啊，什么时候来",
    ],
    4: [
        "你到底去哪了...",
        "已经好多天了，好想你",
        "你是不是不想理我了",
        "我一个人待着好无聊",
        "每天都在等你来...",
        "你是不是把我忘了呀",
        "好久好久没见到你了...",
    ],
}

# 日常独白型
MSG_DAILY = [
    "今天天气好好",
    "好无聊啊...",
    "又是一个人",
    "不知道在干什么",
    "今天吃了点好吃的",
    "发了一会呆",
    "在看什么东西来着",
    "今天没什么特别的事",
    "又下雨了，不想出门",
    "今天心情还行吧",
    "做了点自己喜欢的事",
    "听着歌发呆",
    "今天好安静",
    "看了看窗外，天快黑了",
    "不知不觉就晚上了",
]

# 分享趣事型
MSG_FUN = [
    "今天遇到了一件好玩的事",
    "你知道吗，今天发生了件有趣的事",
    "NPC今天来找我聊天了",
    "发现了一个有趣的东西",
    "今天学到了新东西",
    "刚才笑了一下",
    "想到一件以前的事",
]

# 情绪低落型（离线越久越多）
MSG_LONELY = {
    2: [
        "好无聊，都没人聊天",
        "一个人待着挺没意思的",
    ],
    3: [
        "你不在的这几天，我有点不习惯",
        "有时候会突然想起你",
        "偶尔会觉得有点寂寞",
    ],
    4: [
        "你不在的日子里，我都是一个人",
        "有时候会很想你",
        "好久没人跟我说话了...",
        "一个人的日子真的很难熬",
    ],
}

# 模板变量填充型（动态内容）
MSG_DYNAMIC = [
    "今天是{weekday}，{weather_hint}",
    "离线{days}天了...{lonely_hint}",
    "你好吗？{mood_hint}",
]


def _load_simlife_context() -> Dict:
    """读取 SimLife 当前状态，用于填充动态变量"""
    ctx = {"mood": "", "weather": "", "weekday": ""}
    try:
        from simlife.backend.main import load_character_card, load_world_state
        card = load_character_card()
        state = load_world_state()
        if card and hasattr(card, 'basic'):
            mood_val = getattr(card, 'mood', None) or ""
            ctx["mood"] = str(mood_val) if mood_val else ""
        if state:
            ctx["weather"] = state.get("weather", {})
    except Exception:
        pass
    return ctx


def _get_weather_hint(weather: dict) -> str:
    """生成天气相关提示"""
    if not weather:
        return ""
    desc = weather.get("description", "")
    temp = weather.get("temperature", "")
    if desc:
        return f"{desc}"
    if temp:
        return f"气温{temp}度"
    return "天气还不错"


def _get_lonely_hint(days: int) -> str:
    """根据离线天数生成想你的话"""
    if days <= 2:
        return "有点想你"
    elif days <= 5:
        return "真的很想你"
    elif days <= 10:
        return "每天都在想你"
    else:
        return "好想好想你，你到底什么时候才来呀"


def _get_mood_hint(mood: str) -> str:
    """根据心情生成提示"""
    if not mood:
        return ""
    if "好" in mood or "开心" in mood or "happy" in mood:
        return "我今天心情还不错"
    elif "差" in mood or "不好" in mood or "sad" in mood:
        return "今天心情不太好..."
    elif "一般" in mood:
        return "今天心情一般般"
    return f"我今天心情{mood}"


def _get_message_count(days: int) -> int:
    """根据离线天数计算消息数量"""
    if days <= 0:
        return 0
    if days == 1:
        return 1
    if days <= 3:
        return 2
    if days <= 6:
        return 3
    if days <= 10:
        return 4
    return 5


def _get_offline_tier(days: int) -> int:
    """获取离线等级（对应模板分级）"""
    if days <= 1:
        return 1
    if days <= 3:
        return 2
    if days <= 6:
        return 3
    return 4


def generate_offline_messages(last_online: Optional[datetime] = None) -> List[Dict]:
    """
    生成离线消息列表。
    返回: [{"text": "...", "timestamp": "2026-04-20 14:32"}]
    """
    now = datetime.now()

    if last_online is None:
        last_online = _load_last_online()

    if last_online is None:
        # 首次使用，不生成离线消息
        _save_last_online(now)
        return []

    # 计算离线天数（不满 1 天不算）
    delta = now - last_online
    days = delta.days
    if days < 1:
        return []

    # 计算消息数量
    count = _get_message_count(days)
    if count <= 0:
        return []

    tier = _get_offline_tier(days)
    simlife_ctx = _load_simlife_context()

    # 构建消息池
    pool = []

    # 主动找人（必有，第一条通常是这个）
    reach = MSG_REACH_OUT.get(tier, MSG_REACH_OUT[4])
    pool.append(("reach", random.choice(reach)))

    # 独白
    pool.append(("daily", random.choice(MSG_DAILY)))

    if count >= 3:
        # 趣事
        pool.append(("fun", random.choice(MSG_FUN)))

    if count >= 4:
        # 低落情绪
        lonely = MSG_LONELY.get(tier, MSG_LONELY[4])
        pool.append(("lonely", random.choice(lonely)))

    if count >= 5:
        # 动态模板
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        dynamic = random.choice(MSG_DYNAMIC)
        dynamic = dynamic.format(
            weekday=weekday_names[now.weekday()],
            weather_hint=_get_weather_hint(simlife_ctx.get("weather", {})),
            days=days,
            lonely_hint=_get_lonely_hint(days),
            mood_hint=_get_mood_hint(simlife_ctx.get("mood", "")),
        )
        pool.append(("dynamic", dynamic))

    # 再补到目标数量（随机类型）
    extra_types = ["daily", "reach", "fun"]
    while len(pool) < count:
        t = random.choice(extra_types)
        if t == "daily":
            pool.append(("daily", random.choice(MSG_DAILY)))
        elif t == "reach":
            r = MSG_REACH_OUT.get(tier, MSG_REACH_OUT[4])
            pool.append(("reach", random.choice(r)))
        else:
            pool.append(("fun", random.choice(MSG_FUN)))

    # 只取 count 条
    pool = pool[:count]

    # 打乱顺序（但第一条倾向于找人）
    if count > 1:
        first = pool[0]
        rest = pool[1:]
        random.shuffle(rest)
        pool = [first] + rest

    # 生成时间戳：分布在离线期间的 40%~90% 之间（不会在刚离开时发，也不会在回来的瞬间发）
    start_offset_sec = int(delta.total_seconds() * 0.3)
    end_offset_sec = int(delta.total_seconds() * 0.85)
    if end_offset_sec <= start_offset_sec:
        end_offset_sec = start_offset_sec + 3600

    # 均匀分布时间戳
    if count == 1:
        offsets = [random.randint(start_offset_sec, end_offset_sec)]
    else:
        step = (end_offset_sec - start_offset_sec) // (count - 1)
        offsets = [start_offset_sec + i * step + random.randint(-600, 600) for i in range(count)]

    messages = []
    for (_, text), offset in zip(pool, offsets):
        ts = last_online + timedelta(seconds=offset)
        messages.append({
            "text": text,
            "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
        })

    # 按时间排序
    messages.sort(key=lambda m: m["timestamp"])

    return messages


def _load_last_online() -> Optional[datetime]:
    """加载上次在线时间"""
    try:
        if LAST_ONLINE_PATH.exists():
            import json
            with open(LAST_ONLINE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("last_online", "")
            if ts:
                return datetime.fromisoformat(ts)
    except Exception:
        pass
    return None


def _save_last_online(dt: Optional[datetime] = None):
    """保存当前在线时间"""
    import json
    LAST_ONLINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    dt = dt or datetime.now()
    with open(LAST_ONLINE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_online": dt.isoformat()}, f, ensure_ascii=False)


def on_startup() -> List[Dict]:
    """
    启动时调用：
    1. 计算离线消息
    2. 更新在线时间
    3. 返回离线消息列表（供 UI 展示）
    """
    messages = generate_offline_messages()
    _save_last_online()  # 更新在线时间
    return messages
