"""
qq_bot.py
QQ 机器人 — OneBot 11（NapCat / LLOneBot）WebSocket 客户端

与飞书机器人同一套桥接思路：
    QQ 消息 → 归一化成 [图片:路径] / [文件:路径] + 文本
            → agent.process(..., override_uid=qq_<QQ号>)
            → 回复文本，并把 [image:]/[download:] 标记的产物发回 QQ

部署前提（非官方方案）：
    1. Windows 安装 QQ NT 客户端并登录
    2. 安装 NapCatQQ，开启「WebSocket 服务器」，默认 ws://127.0.0.1:3001
    3. 依赖：pip install websocket-client>=1.6.0（requirements.txt 已包含）
    4. 在设置页填写 WebSocket 地址、Token、QQ 号白名单、群号白名单

安全说明：
    白名单为空时不会响应任何人。agent 具备工具执行能力，务必只把
    可信的 QQ 号 / 群号加入白名单。
"""
import base64
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


# ── 配置读取 ────────────────────────────────────────────────
def _desktop_config() -> dict:
    """读取 desktop / ha 配置（与 lark_bot.py 优先级一致）"""
    # 1) 环境变量优先
    cfg = {
        "qq_enable": os.environ.get("QQ_ENABLE", "").lower() in ("1", "true", "yes"),
        "qq_ws_url": os.environ.get("QQ_WS_URL", ""),
        "qq_access_token": os.environ.get("QQ_ACCESS_TOKEN", ""),
        "qq_allowed_uids": os.environ.get("QQ_ALLOWED_UIDS", ""),
        "qq_group_whitelist": os.environ.get("QQ_GROUP_WHITELIST", ""),
    }
    if cfg["qq_ws_url"]:
        return cfg

    # 2) ha_config.json（服务器部署）
    try:
        cfg_path = Path(__file__).parent / "ha_config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if data.get("qq_ws_url"):
                for k in ("qq_enable", "qq_ws_url", "qq_access_token",
                          "qq_allowed_uids", "qq_group_whitelist"):
                    if k in data:
                        cfg[k] = data[k]
                return cfg
    except Exception:
        pass

    # 3) desktop config.json（GUI 设置）
    try:
        if os.name == "nt":
            root = Path(os.environ.get("APPDATA", str(Path.home()))) / "workingAgent"
        else:
            root = Path.home() / ".workingagent"
        f = root / "config.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            for k in ("qq_enable", "qq_ws_url", "qq_access_token",
                      "qq_allowed_uids", "qq_group_whitelist"):
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def _parse_ids(raw) -> set:
    """'123,456，789 012' → {'123','456','789','012'}"""
    return {s for s in re.split(r"[,，;；\s]+", str(raw or "")) if s}


# ── QQ 表情映射 ────────────────────────────────────────────
# 常用 QQ 表情 id → 名称：收到的表情转成 [表情:名称] 供 AI 理解
FACE_NAMES = {
    "0": "惊讶", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意",
    "5": "流泪", "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭",
    "10": "尴尬", "11": "发怒", "12": "调皮", "13": "呲牙", "14": "微笑",
    "15": "难过", "16": "酷", "17": "冷汗", "18": "抓狂", "19": "吐",
    "20": "偷笑", "21": "可爱", "22": "白眼", "23": "傲慢", "24": "饥饿",
    "25": "困", "26": "惊恐", "27": "流汗", "28": "憨笑", "29": "大兵",
    "30": "奋斗", "31": "咒骂", "32": "疑问", "33": "嘘", "34": "晕",
    "35": "折磨", "36": "衰", "37": "骷髅", "38": "敲打", "39": "再见",
    "40": "擦汗", "41": "抠鼻", "42": "鼓掌", "43": "糗大了", "44": "坏笑",
    "45": "左哼哼", "46": "右哼哼", "47": "哈欠", "48": "鄙视", "49": "委屈",
    "50": "快哭了", "51": "阴险", "52": "亲亲", "53": "吓", "54": "可怜",
    "55": "菜刀", "56": "西瓜", "57": "啤酒", "58": "篮球", "59": "乒乓",
    "60": "咖啡", "61": "饭", "62": "猪头", "63": "玫瑰", "64": "凋谢",
    "66": "爱心", "67": "心碎", "68": "蛋糕", "69": "闪电", "70": "炸弹",
    "71": "刀", "72": "足球", "74": "便便", "75": "月亮", "76": "太阳",
    "77": "礼物", "78": "拥抱", "79": "强", "80": "弱", "81": "握手",
    "82": "胜利", "83": "抱拳", "84": "勾引", "85": "拳头", "86": "差劲",
    "87": "爱你", "88": "NO", "89": "OK", "90": "爱情", "91": "飞吻",
    "92": "跳跳", "93": "发抖", "94": "怄火", "95": "转圈", "96": "磕头",
    "97": "回头", "98": "跳绳", "99": "挥手", "100": "激动",
}

# AI 回复里的 emoji → QQ 表情 id；未收录的 emoji 原样保留为文字
EMOJI_FACE = {
    "😊": "14", "🙂": "14", "😄": "14", "😁": "13", "😆": "13",
    "😂": "13", "🤣": "13", "😭": "5", "😢": "5", "🥲": "5",
    "😡": "11", "😠": "11", "😳": "6", "😴": "8", "😪": "8",
    "😏": "4", "😎": "16", "🤔": "32", "😅": "27", "😓": "27",
    "😱": "26", "😨": "26", "🤢": "19", "🤮": "19", "😍": "21",
    "😘": "52", "👍": "79", "👎": "80", "👏": "42", "🙏": "83",
    "🤝": "81", "✌": "82", "❤": "66", "💔": "67", "🌹": "63",
    "🍰": "68", "🍺": "57", "☕": "60", "⚽": "72", "🐷": "62",
}

# 回复里显式的表情标记：[face:14]
FACE_TOKEN_RE = re.compile(r"\[face:(\d{1,4})\]")
FACE_MAX = 5          # 单条回复最多带 5 个表情，避免刷屏


# ── OneBot 11 客户端 ────────────────────────────────────────
class OneBotClient:
    """极简 OneBot 11 WS 客户端：收事件 + 调用 API（靠 echo 关联应答）"""

    def __init__(self, ws_url: str, token: str = ""):
        self.ws_url = (ws_url or "").strip()
        self.token = (token or "").strip()
        self.self_id = ""
        self._ws = None
        self._connected = threading.Event()
        self._pending = {}
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._on_event = None
        self._app = None

    # ── 连接 ───────────────────────────────────────────
    def run(self, on_event):
        """阻塞运行；断线由 websocket-client 自动重连"""
        if websocket is None:
            print("[QQBot] websocket-client 未安装，QQ 不可用")
            print("[QQBot] 安装: pip install websocket-client>=1.6.0")
            return
        if not self.ws_url:
            print("[QQBot] 未配置 QQ WebSocket 地址，跳过启动")
            return

        self._on_event = on_event
        header = [f"Authorization: Bearer {self.token}"] if self.token else []

        def on_open(ws):
            self._connected.set()
            print(f"[QQBot] 已连接 OneBot: {self.ws_url}")

        def on_message(ws, raw):
            try:
                data = json.loads(raw)
            except Exception:
                return
            echo = data.get("echo")
            if echo:
                item = self._pending.get(echo)
                if item:
                    item[1] = data
                    item[0].set()
                return
            # 记录 bot 自身 QQ 号（事件里自带）
            sid = data.get("self_id")
            if sid:
                self.self_id = str(sid)
            if data.get("post_type") in ("message", "message_sent"):
                try:
                    self._on_event(data)
                except Exception as e:
                    print(f"[QQBot] 事件处理异常: {e}")

        def on_close(ws, code, msg):
            self._connected.clear()
            print(f"[QQBot] 连接断开 ({code})，5 秒后重连…")

        def on_error(ws, err):
            print(f"[QQBot] 连接错误: {err}")

        self._app = websocket.WebSocketApp(
            self.ws_url, header=header,
            on_open=on_open, on_message=on_message,
            on_close=on_close, on_error=on_error,
        )
        while not self._stop.is_set():
            try:
                self._app.run_forever(reconnect=5, ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"[QQBot] 运行异常: {e}")
            if self._stop.is_set():
                break
            time.sleep(3)

    def stop(self):
        self._stop.set()
        self._connected.clear()
        try:
            if self._app:
                self._app.close()
        except Exception:
            pass

    # ── 调用 API ───────────────────────────────────────
    def call(self, action: str, params: dict = None, timeout: float = 20) -> dict:
        if not self._connected.is_set() or self._app is None:
            return {}
        echo = uuid.uuid4().hex
        ev = threading.Event()
        self._pending[echo] = [ev, None]
        payload = {"action": action, "params": params or {}, "echo": echo}
        try:
            with self._send_lock:
                self._app.send(json.dumps(payload, ensure_ascii=False))
            ev.wait(timeout)
        except Exception as e:
            print(f"[QQBot] 调用 {action} 失败: {e}")
        finally:
            item = self._pending.pop(echo, None)
        if not item or item[1] is None:
            return {}
        return item[1]

    # ── 发送 ───────────────────────────────────────────
    def send_private(self, user_id, message) -> dict:
        return self.call("send_private_msg", {"user_id": int(user_id), "message": message})

    def send_group(self, group_id, message) -> dict:
        return self.call("send_group_msg", {"group_id": int(group_id), "message": message})


# ── QQ 机器人 ───────────────────────────────────────────────
class QQBot:
    """把 QQ（OneBot 11）消息桥接到 agent.process()"""

    TEXT_LIMIT = 900          # 单条文本上限（QQ 长消息容易发送失败）

    def __init__(self, agent, cfg: Optional[dict] = None):
        self._agent = agent
        self._cfg = cfg or _desktop_config()
        self._client = OneBotClient(self._cfg.get("qq_ws_url", ""),
                                    self._cfg.get("qq_access_token", ""))
        self._allowed = _parse_ids(self._cfg.get("qq_allowed_uids", ""))
        self._groups = _parse_ids(self._cfg.get("qq_group_whitelist", ""))
        self._tmpdir = Path(os.environ.get("TEMP", "/tmp")) / "workingagent_qq"
        self._tmpdir.mkdir(parents=True, exist_ok=True)
        self._user_locks = {}
        self._lock_guard = threading.Lock()
        self._thread = None

    def set_agent(self, agent):
        """引擎重载时更新 agent 引用，无需重连 QQ"""
        self._agent = agent

    def start(self):
        if not self._cfg.get("qq_enable"):
            print("[QQBot] 未启用（设置里勾选「启用 QQ 机器人」后重启生效）")
            return
        if not self._allowed:
            print("[QQBot] 警告：QQ 号白名单为空，不会响应任何消息")
        self._thread = threading.Thread(target=self._client.run,
                                        args=(self._on_event,), daemon=True)
        self._thread.start()
        print("[QQBot] QQ 机器人已启动（OneBot 11 / NapCat）")

    def stop(self):
        self._client.stop()

    # ── 事件分发 ───────────────────────────────────────
    def _on_event(self, ev: dict):
        if ev.get("post_type") == "message_sent":
            return
        if str(ev.get("user_id")) == str(ev.get("self_id")):
            return
        threading.Thread(target=self._handle, args=(ev,), daemon=True).start()

    def _handle(self, ev: dict):
        try:
            self._handle_inner(ev)
        except Exception as e:
            print(f"[QQBot] 处理异常: {e}")

    def _handle_inner(self, ev: dict):
        kind = ev.get("message_type")            # private / group
        user_id = str(ev.get("user_id", ""))
        group_id = str(ev.get("group_id") or "")
        if not user_id:
            return

        # 白名单
        if user_id not in self._allowed:
            print(f"[QQBot] 忽略非白名单用户 {user_id}")
            return

        segs = self._segments(ev.get("message"))
        if kind == "group":
            if group_id not in self._groups:
                print(f"[QQBot] 忽略非白名单群 {group_id}")
                return
            if not self._is_at_me(segs, ev.get("self_id")):
                return    # 群里只在 @ 时响应
        elif kind != "private":
            return

        target = group_id if kind == "group" else user_id
        sender = ev.get("sender") or {}
        uname = (sender.get("card") or sender.get("nickname") or user_id).strip()

        # 占位回复，避免用户以为没响应
        self._reply(kind, target, "正在思考…", at_user=user_id if kind == "group" else "")

        attachment, text = self._collect(segs)
        user_input = " ".join([p for p in (attachment, text) if p]).strip() or "请分析以上内容"

        uid = f"qq_group{group_id}_{user_id}" if kind == "group" else f"qq_{user_id}"
        try:
            result = self._agent.process(
                user_input,
                override_uid=uid,
                override_uname=f"QQ_{uname}"[:40],
            )
            reply = (result.get("response") or result.get("reply")
                     or result.get("text") or "好的，我知道了。")
        except Exception as e:
            print(f"[QQBot] agent 处理失败: {e}")
            reply = f"抱歉，我遇到了一点问题：{e}"

        reply = self._deliver(kind, target, reply)
        if not reply.strip():
            reply = "（已完成）"
        self._reply(kind, target, reply,
                    at_user=user_id if kind == "group" else "")

    # ── 消息解析 ───────────────────────────────────────
    @staticmethod
    def _segments(message) -> list:
        if isinstance(message, list):
            return message
        if isinstance(message, str):
            return [{"type": "text", "data": {"text": message}}]
        return []

    @staticmethod
    def _is_at_me(segs: list, self_id) -> bool:
        if not self_id:
            return True     # 拿不到自己的 QQ 号时放行，避免完全不响应
        return any(s.get("type") == "at"
                   and str((s.get("data") or {}).get("qq")) == str(self_id)
                   for s in segs)

    def _collect(self, segs: list):
        """返回 (附件标记, 纯文本)"""
        texts, attachments = [], []
        for seg in segs:
            t = seg.get("type")
            data = seg.get("data") or {}
            if t == "text":
                texts.append(str(data.get("text", "")))
            elif t == "image":
                path = self._save_image(data)
                if path:
                    attachments.append(f"[图片:{path}]")
            elif t == "file":
                path = self._save_file(data)
                if path:
                    attachments.append(f"[文件:{path}]")
            elif t == "face":
                fid = str(data.get("id", ""))
                texts.append(f"[表情:{FACE_NAMES.get(fid, fid)}]")
            elif t == "mface":
                summary = data.get("summary") or data.get("name") or "动态表情"
                texts.append(f"[表情包:{summary}]")
            elif t in ("at", "reply", "json", "xml"):
                continue
            elif t == "record":
                attachments.append("[语音]")
        return " ".join(attachments), "".join(texts).strip()

    def _save_image(self, data: dict) -> Optional[Path]:
        url = data.get("url") or ""
        local = data.get("file") or ""
        if not url and not local:
            # 只有 file_id 的情况，向 OneBot 查询
            q = self._client.call("get_image", {"file": data.get("file_id") or local})
            info = (q.get("data") or {})
            url = info.get("url") or ""
            local = info.get("file") or info.get("path") or ""
        return self._save_media(url, local, "img", "png")

    def _save_file(self, data: dict) -> Optional[Path]:
        url = data.get("url") or ""
        local = data.get("file") or ""
        name = data.get("file_name") or data.get("name") or data.get("file") or "file"
        if not url and (data.get("file_id") or local):
            q = self._client.call("get_file", {
                "file_id": data.get("file_id") or "",
                "file": data.get("file") or "",
            })
            info = (q.get("data") or {})
            url = info.get("url") or ""
            local = info.get("file") or info.get("path") or ""
            name = info.get("file_name") or info.get("name") or name
        return self._save_media(url, local, "file", "", name)

    def _save_media(self, url: str, local: str, prefix: str,
                    default_ext: str, name: str = "") -> Optional[Path]:
        try:
            base = Path(local) if local else None
            if base and base.is_file():
                data = base.read_bytes()
                ext = base.suffix.lstrip(".") or default_ext
                fname = base.name
            elif url.startswith("http"):
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                data = r.content
                ext = (Path(url.split("?")[0]).suffix.lstrip(".") or default_ext)
                fname = Path(url.split("?")[0]).name or f"{prefix}.{ext}"
            else:
                return None
            safe = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", fname) or f"{prefix}.{ext}"
            dst = self._tmpdir / f"{int(time.time() * 1000)}_{safe}"
            dst.write_bytes(data)
            return dst
        except Exception as e:
            print(f"[QQBot] 媒体保存失败: {e}")
            return None

    # ── 回复与交付 ─────────────────────────────────────
    def _reply(self, kind: str, target: str, text: str, at_user: str = ""):
        """发送文本（自动分段），并把 [face:N] / emoji 转成 QQ 表情段"""
        text = text or ""
        tokens = self._tokenize(text)
        text_len = sum(len(v) for k, v in tokens if k == "text")

        # 常规长度：一次发出，文字与表情按原顺序排列
        if tokens and text_len <= self.TEXT_LIMIT:
            segs = []
            if at_user:
                segs.append({"type": "at", "data": {"qq": str(at_user)}})
                segs.append({"type": "text", "data": {"text": " "}})
            faces = 0
            for k, v in tokens:
                if k == "face":
                    if faces >= FACE_MAX:
                        continue
                    faces += 1
                    segs.append({"type": "face", "data": {"id": v}})
                else:
                    segs.append({"type": "text", "data": {"text": v}})
            self._send_group_or_private(kind, target, segs)
            return

        # 超长回复：退化为纯文本分段（表情标记去掉，避免切段后错位）
        plain = FACE_TOKEN_RE.sub("", text)
        for chunk in self._split_text(plain):
            segs = []
            if at_user:
                segs.append({"type": "at", "data": {"qq": str(at_user)}})
                segs.append({"type": "text", "data": {"text": " "}})
            segs.append({"type": "text", "data": {"text": chunk}})
            self._send_group_or_private(kind, target, segs)

    @staticmethod
    def _tokenize(text: str) -> list:
        """切成 [('text', 文本) | ('face', 表情id)]，保持原始顺序"""
        toks, pos = [], 0
        for m in FACE_TOKEN_RE.finditer(text):
            if m.start() > pos:
                toks.extend(QQBot._emoji_tokens(text[pos:m.start()]))
            toks.append(("face", m.group(1)))
            pos = m.end()
        if pos < len(text):
            toks.extend(QQBot._emoji_tokens(text[pos:]))
        return toks

    @staticmethod
    def _emoji_tokens(s: str) -> list:
        """emoji → ('face', id)；不认识的字符归入文本"""
        toks, buf = [], ""
        for ch in s:
            if ch in ("\ufe0f", "\u200d"):   # 变体选择符 / 零宽连接符，丢弃
                continue
            fid = EMOJI_FACE.get(ch)
            if fid:
                if buf:
                    toks.append(("text", buf))
                    buf = ""
                toks.append(("face", fid))
            else:
                buf += ch
        if buf:
            toks.append(("text", buf))
        return toks

    @staticmethod
    def _split_text(text: str, limit: int = TEXT_LIMIT) -> list:
        text = text or ""
        if len(text) <= limit:
            return [text] if text.strip() else []
        chunks, buf = [], ""
        for line in text.splitlines(keepends=True):
            while len(line) > limit:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.append(line[:limit])
                line = line[limit:]
            if len(buf) + len(line) > limit:
                chunks.append(buf)
                buf = ""
            buf += line
        if buf:
            chunks.append(buf)
        return chunks

    def _deliver(self, kind: str, target: str, text: str) -> str:
        """把 [download:文件名:显示名] / [image:文件名:显示名] 发送为真实文件/图片"""
        file_pat = re.compile(r"\[download:([^:\]]+):([^:\]]+)\]")
        image_pat = re.compile(r"\[image:([^:\]]+):([^:\]]+)\]")
        cleaned = file_pat.sub("", text)
        cleaned = image_pat.sub("", cleaned).strip()

        download_dir = Path(__file__).parent / "downloads"
        for m in file_pat.finditer(text):
            src = download_dir / m.group(1)
            if not src.exists():
                cleaned += f"\n（文件丢失：{m.group(1)}）"
                continue
            seg = {"type": "file",
                   "data": {"file": src.resolve().as_uri(), "name": m.group(2)}}
            self._send_group_or_private(kind, target, [seg])
        for m in image_pat.finditer(text):
            src = download_dir / m.group(1)
            if not src.exists():
                cleaned += f"\n（图片丢失：{m.group(1)}）"
                continue
            try:
                b64 = base64.b64encode(src.read_bytes()).decode()
            except Exception as e:
                cleaned += f"\n（图片读取失败：{e}）"
                continue
            seg = {"type": "image", "data": {"file": f"base64://{b64}"}}
            self._send_group_or_private(kind, target, [seg])
        return cleaned

    def _send_group_or_private(self, kind: str, target: str, segs: list):
        if kind == "group":
            self._client.send_group(target, segs)
        else:
            self._client.send_private(target, segs)


# ── 便捷入口（main.py / server_start.py 共用）───────────────
def start_qq_bot(agent, cfg: Optional[dict] = None) -> Optional[QQBot]:
    """按配置启动 QQ 机器人；未启用返回 None"""
    try:
        merged = _desktop_config()
        if cfg:
            for k in ("qq_enable", "qq_ws_url", "qq_access_token",
                      "qq_allowed_uids", "qq_group_whitelist"):
                if k in cfg:
                    merged[k] = cfg[k]
        if not merged.get("qq_enable"):
            print("[QQBot] 未启用 QQ 机器人，跳过")
            return None
        bot = QQBot(agent, merged)
        bot.start()
        return bot
    except Exception as e:
        print(f"[QQBot] 启动失败: {e}")
        return None
