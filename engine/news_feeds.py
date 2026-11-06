"""
正规新闻源抓取（RSS，零 API Key）

用途：主动学习页面取"当天真实新闻"，替代不可用的 web_search。

实测可用（feed 带发布时间且当日更新）：
  中新网·滚动 / 中新网·国际 / 界面新闻 / IT之家 / 少数派 / Solidot

已停用（实测失效，勿再启用）：
  新华网·时政/国际   —— feed 无发布时间，内容是旧稿
  人民网·时政/国际/财经 —— pubDate 固定为 2025-06-05，不再更新
  36氪 / 澎湃 / 财新 / 环球网 —— 返回网页不是 feed
  第一财经 —— 404；BBC / Reuters —— 当前网络超时
"""

import calendar
import html
import re
import urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) workingAgent/1.0"

# 只保留超过这么多天的条目（防止把陈年旧闻当"今天"学）
FRESH_DAYS = 2


# 源定义：key → (显示名, RSS 地址)
FEEDS: Dict[str, Tuple[str, str]] = {
    "chinanews_scroll": ("中新网·滚动", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    "chinanews_world":  ("中新网·国际", "https://www.chinanews.com.cn/rss/world.xml"),
    "jiemian":          ("界面新闻", "https://a.jiemian.com/index.php?m=article&a=rss"),
    "ithome":           ("IT之家", "https://www.ithome.com/rss/"),
    "sspai":            ("少数派", "https://sspai.com/feed"),
    "solidot":          ("Solidot", "https://www.solidot.org/index.rss"),
}

# 主题关键词 → 源列表（按顺序命中第一个）
TOPIC_MAP: List[Tuple[Tuple[str, ...], List[str]]] = [
    (("人工智能", "ai", "科技", "技术", "科技新闻"),
     ["ithome", "sspai", "solidot"]),
    (("世界", "国际", "全球", "外交"),
     ["chinanews_world", "jiemian"]),
    (("财经", "经济", "金融", "股市", "商业"),
     ["jiemian", "chinanews_scroll"]),
    (("国内", "时政", "政治", "社会", "中国"),
     ["chinanews_scroll", "jiemian"]),
]

DEFAULT_FEEDS = ["chinanews_scroll", "jiemian", "ithome"]


def pick_feeds(topic: str) -> List[str]:
    """按主题挑源；没命中就用默认三源"""
    low = (topic or "").lower()
    for keys, feeds in TOPIC_MAP:
        if any(k in low for k in keys):
            return feeds
    return DEFAULT_FEEDS


def _clean(text: str, limit: int = 120) -> str:
    """去 HTML 标签、实体、多余空白"""
    s = re.sub(r"<[^>]+>", "", text or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _norm_title(title: str) -> str:
    """标题归一化，用于跨源去重"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", title or "").lower()


def _utc_to_local(ts) -> Optional[datetime]:
    """feedparser 的 published_parsed 是 UTC，转成本机时间"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(ts))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_feed(body: bytes) -> List[dict]:
    """优先 feedparser；没装则用标准库解析"""
    try:
        import feedparser
        d = feedparser.parse(body)
        out = []
        for e in d.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            dt = _utc_to_local(ts)
            out.append({
                "title": _clean(e.get("title", ""), 80),
                "summary": _clean(e.get("summary") or e.get("description", ""), 120),
                "link": e.get("link", ""),
                "time": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "dt": dt,
            })
        return out
    except Exception:
        pass

    # 兜底：xml.etree（拿不到发布时间，dt 为空，后续会被新鲜度过滤掉）
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(body)
    except Exception:
        return out
    for item in root.iter():
        tag = item.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = summary = link = ""
        for child in item:
            name = child.tag.split("}")[-1].lower()
            if name == "title":
                title = child.text or ""
            elif name in ("description", "summary", "content"):
                summary = child.text or ""
            elif name == "link":
                link = child.text or child.attrib.get("href", "")
        out.append({
            "title": _clean(title, 80),
            "summary": _clean(summary, 120),
            "link": link,
            "time": "",
            "dt": None,
        })
    return out


def fetch_feed(key: str, limit: int = 8) -> List[dict]:
    """抓单个源，失败返回空列表"""
    name, url = FEEDS[key]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        items = _parse_feed(body)[:limit]
        for it in items:
            it["source"] = name
        return [it for it in items if it["title"]]
    except Exception:
        return []


def fetch_for_topic(topic: str, per_feed: int = 6, total: int = 12,
                    timeout: float = 15) -> Tuple[List[dict], List[str]]:
    """
    按主题抓取正规新闻
    返回 (条目列表, 实际命中的源名列表)
    条目字段：title / summary / time / source / link
    只保留最近 FRESH_DAYS 天内、且有发布时间的条目
    """
    keys = pick_feeds(topic)
    items: List[dict] = []

    with ThreadPoolExecutor(max_workers=min(4, len(keys))) as pool:
        futs = {pool.submit(fetch_feed, k, per_feed): k for k in keys}
        for f in as_completed(futs, timeout=timeout):
            try:
                items.extend(f.result())
            except Exception:
                continue

    # 新鲜度过滤：无发布时间（无法确认是当天）或超过 FRESH_DAYS 一律丢弃
    now = datetime.now()
    fresh = []
    for it in items:
        dt = it.get("dt")
        if not dt:
            continue
        if (now - dt).total_seconds() > FRESH_DAYS * 86400:
            continue
        fresh.append(it)

    # 跨源去重
    seen, uniq = set(), []
    for it in fresh:
        n = _norm_title(it["title"])
        if not n or n in seen:
            continue
        seen.add(n)
        uniq.append(it)

    # 各源内部按时间倒序，再轮转合并，避免单一源占满结果
    by_src: Dict[str, List[dict]] = {}
    for it in uniq:
        by_src.setdefault(it["source"], []).append(it)
    queues = {name: sorted(lst, key=lambda x: x["dt"], reverse=True)
              for name, lst in by_src.items()}
    merged: List[dict] = []
    while len(merged) < len(uniq):
        for q in queues.values():
            if q:
                merged.append(q.pop(0))

    return merged[:total], list(queues.keys())


def build_context(items: List[dict], max_chars: int = 1200) -> str:
    """把新闻条目拼成给 LLM 的参考资料"""
    parts, size = [], 0
    for it in items:
        line = f"【{it.get('source', '')}{(' ' + it['time']) if it.get('time') else ''}】{it['title']}"
        if it.get("summary"):
            line += f"\n{it['summary']}"
        if size + len(line) > max_chars:
            break
        parts.append(line)
        size += len(line)
    return "\n\n".join(parts)
