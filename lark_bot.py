"""
lark_bot.py
飞书智能机器人 — 长连接（WebSocket）客户端
基于 lark-oapi 官方 SDK，无需公网 IP / 域名 / 回调地址

相比企业微信机器人，额外支持：文件消息、图片消息（下载后交给 AI 处理）。

使用方式：
    1. pip install lark-oapi>=1.6.5
    2. 飞书开放平台建「企业自建应用」：添加机器人能力 + 订阅事件
       im.message.receive_v1 ，事件订阅方式选「使用长连接接收事件」
    3. 权限申请：im:message:readonly、im:message:send_as_bot、
       im:resource、im:message.p2p_msg:readonly、(群聊还需 im:message.group_at_msg:readonly)
    4. 配置 lark_app_id / lark_app_secret（环境变量或 ha_config.json / desktop config.json）
    5. server_start.py 中启动
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests


def _load_lark_config() -> dict:
    """读取飞书配置

    优先级：环境变量 > ha_config.json（服务器部署） > desktop config.json（GUI设置）
    """
    # 1) 环境变量优先
    app_id = os.environ.get("LARK_APP_ID", "")
    secret = os.environ.get("LARK_APP_SECRET", "")
    if app_id and secret:
        return {"app_id": app_id, "app_secret": secret}

    # 2) ha_config.json
    try:
        cfg_path = Path(__file__).parent / "ha_config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            app_id = cfg.get("lark_app_id", "")
            secret = cfg.get("lark_app_secret", "")
            if app_id and secret:
                return {"app_id": app_id, "app_secret": secret}
    except Exception:
        pass

    # 3) desktop config.json（GUI 设置方式）
    try:
        if os.name == "nt" or sys.platform == "win32":
            data_root = Path(os.environ.get("APPDATA", str(Path.home()))) / "workingAgent"
        else:
            data_root = Path.home() / ".workingagent"
        cfg_file = data_root / "config.json"
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            app_id = cfg.get("lark_app_id", "")
            secret = cfg.get("lark_app_secret", "")
            if app_id and secret:
                return {"app_id": app_id, "app_secret": secret}
    except Exception:
        pass

    return {}


class FeishuHttp:
    """基于 requests 的飞书 OpenAPI 轻封装（token / 下载 / 回复 / 发送）"""

    def __init__(self, app_id: str, app_secret: str,
                 base_url: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._token_expire = 0

    # ── token ───────────────────────────────────
    def tenant_token(self) -> str:
        """获取(并缓存) tenant_access_token"""
        if self._token and time.time() < self._token_expire:
            return self._token
        r = requests.post(
            f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        data = r.json()
        self._token = data.get("tenant_access_token", "")
        self._token_expire = time.time() + int(data.get("expire", 7200)) - 60
        return self._token

    def _headers(self) -> dict:
        # 新版本签名校验：feishu 要求请求带 x-lark-signature，这里关闭校验即可
        return {
            "Authorization": f"Bearer {self.tenant_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _code(self, resp):
        """安全取应答 code，解析失败返回 -1（不抛异常）"""
        try:
            return resp.json().get("code", -1)
        except Exception:
            return -1

    # ── 发送 / 回复 ──────────────────────────────
    def send_text(self, chat_id: str, text: str) -> bool:
        try:
            r = requests.post(
                f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers=self._headers(),
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                timeout=15,
            )
            return self._code(r) == 0
        except Exception as e:
            print(f"[LarkBot] send_text 失败: {e}")
            return False

    # ── 文件上传 / 发送 ──────────────────────────
    def upload_file(self, file_path: str, file_name: str) -> str:
        """上传本地文件到飞书，返回 file_key；失败返回空串"""
        try:
            token = self.tenant_token()
            with open(file_path, "rb") as f:
                r = requests.post(
                    f"{self.base_url}/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "stream", "file_name": file_name},
                    files={"file": (file_name, f)},
                    timeout=60,
                )
            code = self._code(r)
            if code != 0:
                print(f"[LarkBot] upload_file 失败 code={code}: {r.text[:200]}")
                return ""
            return (r.json().get("data", {}) or {}).get("file_key", "")
        except Exception as e:
            print(f"[LarkBot] upload_file 异常: {e}")
            return ""

    def send_file(self, chat_id: str, file_key: str) -> bool:
        try:
            r = requests.post(
                f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers=self._headers(),
                json={
                    "receive_id": chat_id,
                    "msg_type": "file",
                    "content": json.dumps({"file_key": file_key}),
                },
                timeout=20,
            )
            code = self._code(r)
            if code != 0:
                print(f"[LarkBot] send_file 失败 code={code}: {r.text[:200]}")
            return code == 0
        except Exception as e:
            print(f"[LarkBot] send_file 异常: {e}")
            return False

    # ── 图片上传 / 发送（图片消息，非文件附件）────────────────
    def upload_image(self, file_path: str) -> str:
        """上传本地图片到飞书，返回 image_key；失败返回空串"""
        try:
            token = self.tenant_token()
            fname = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                r = requests.post(
                    f"{self.base_url}/open-apis/im/v1/images?image_type=message",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"image": (fname, f, "image/png")},
                    timeout=60,
                )
            code = self._code(r)
            if code != 0:
                print(f"[LarkBot] upload_image 失败 code={code}: {r.text[:200]}")
                return ""
            return (r.json().get("data", {}) or {}).get("image_key", "")
        except Exception as e:
            print(f"[LarkBot] upload_image 异常: {e}")
            return ""

    def send_image(self, chat_id: str, image_key: str) -> bool:
        try:
            r = requests.post(
                f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers=self._headers(),
                json={
                    "receive_id": chat_id,
                    "msg_type": "image",
                    "content": json.dumps({"image_key": image_key}),
                },
                timeout=20,
            )
            code = self._code(r)
            if code != 0:
                print(f"[LarkBot] send_image 失败 code={code}: {r.text[:200]}")
            return code == 0
        except Exception as e:
            print(f"[LarkBot] send_image 异常: {e}")
            return False

    def reply_text(self, message_id: str, text: str) -> bool:
        try:
            r = requests.post(
                f"{self.base_url}/open-apis/im/v1/messages/{message_id}/reply",
                headers=self._headers(),
                json={
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                timeout=15,
            )
            code = self._code(r)
            if code != 0:
                print(f"[LarkBot] reply_text 失败 code={code}: {r.text[:200]}")
            return code == 0
        except Exception as e:
            print(f"[LarkBot] reply_text 异常: {e}")
            return False

    # ── 下载文件/图片 ────────────────────────────
    def download(self, message_id: str, file_key: str, type_: str) -> Optional[bytes]:
        """下载消息资源。type_: 'file' 文件 / 'image' 图片"""
        try:
            r = requests.get(
                f"{self.base_url}/open-apis/im/v1/messages/{message_id}"
                f"/resources/{file_key}",
                params={"type": type_},
                headers={"Authorization": f"Bearer {self.tenant_token()}"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.content
            print(f"[LarkBot] download 失败: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[LarkBot] download 异常: {e}")
        return None


class LarkBot:
    """飞书智能机器人：把飞书聊天消息桥接到 agent.process()，支持文本/文件/图片"""

    def __init__(self, agent, app_id: str = "", app_secret: str = "",
                 base_url: str = ""):
        self._agent = agent
        config = _load_lark_config()
        self._app_id = app_id or config.get("app_id", "")
        self._secret = app_secret or config.get("app_secret", "")
        self._base_url = base_url or os.environ.get("LARK_BASE_URL", "https://open.feishu.cn")
        self._http = None
        self._tmpdir = Path(os.environ.get("TEMP", "/tmp")) / "workingagent_lark"
        self._tmpdir.mkdir(parents=True, exist_ok=True)

    # ── 入口 ─────────────────────────────────────
    def start(self):
        """启动飞书长连接。阻塞运行，autoreconnect 由 SDK 内部处理。"""
        if not self._app_id or not self._secret:
            print("[LarkBot] 未配置 lark_app_id / lark_app_secret，跳过启动")
            return

        try:
            import lark_oapi as lark
        except ImportError:
            print("[LarkBot] lark-oapi 未安装，飞书不可用")
            print("[LarkBot] 安装: pip install lark-oapi>=1.6.5")
            return

        self._http = FeishuHttp(self._app_id, self._secret, self._base_url)

        def on_message(data: lark.im.v1.P2ImMessageReceiveV1):
            """收到消息 → 另起线程处理（长连接需 3 秒内返回，否则会重推）"""
            try:
                self._dispatch(data)
            except Exception as e:
                print(f"[LarkBot] dispatch 异常: {e}")

        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(on_message) \
            .build()

        cli = lark.ws.Client(
            self._app_id, self._secret,
            event_handler=event_handler,
            log_level=getattr(lark.LogLevel, "WARNING", None),
        )
        print("[LarkBot] 飞书长连接启动…")

        # 建连阻塞；断线 SDK 会自动重连，这里做最外层兜底
        while True:
            try:
                cli.start()
            except Exception as e:
                print(f"[LarkBot] 连接异常: {e}")
            print("[LarkBot] 连接断开，5秒后重连...")
            time.sleep(5)

    # ── 事件分发 ─────────────────────────────────
    def _dispatch(self, data):
        # P2ImMessageReceiveV1 结构：data.event.message.* / data.event.sender.*
        evt = getattr(data, "event", None)
        msg = getattr(evt, "message", None) if evt else None
        if msg is None:
            msg = getattr(data, "message", None)
        if not msg:
            return
        message_id = getattr(msg, "message_id", "") or ""
        chat_id = getattr(msg, "chat_id", "") or ""
        message_type = getattr(msg, "message_type", "text") or "text"
        content_raw = getattr(msg, "content", "") or "{}"
        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else {}
        except Exception:
            content = {}

        sender_id = ""
        sender = getattr(evt, "sender", None) if evt else None
        if sender and sender.sender_id:
            sender_id = (sender.sender_id.open_id or "") if hasattr(sender.sender_id, "open_id") else ""
        print(f"[LarkBot] 收到 {message_type} 消息（{chat_id} / {sender_id}）")

        # 异步线程处理，避免阻塞长连接
        threading.Thread(
            target=self._handle,
            args=(message_id, chat_id, message_type, content, sender_id),
            daemon=True,
        ).start()

    def _handle(self, message_id, chat_id, message_type, content, sender_id):
        # 立刻回占位，避免等待 AI 期间用户以为没应答
        self._http.reply_text(message_id, "正在思考…")
        try:
            reply = self._run_agent(message_id, message_type, content, sender_id)
        except Exception as e:
            print(f"[LarkBot] 处理异常: {e}")
            reply = f"抱歉，我遇到了一点问题：{e}"
        # 若回应带 [download:...]/[image:...] 标记，把文件/图片上传发送到飞书并从文本移除
        reply = self._deliver(chat_id, reply)
        self._http.reply_text(message_id, reply)

    # ── 交付：解析 [download:文件名:显示名] 与 [image:文件名:显示名] 标记并发送 ──
    def _deliver(self, chat_id: str, text: str) -> str:
        file_pat = re.compile(r"\[download:([^:\]]+):([^:\]]+)\]")
        image_pat = re.compile(r"\[image:([^:\]]+):([^:\]]+)\]")
        cleaned = file_pat.sub("", text)
        cleaned = image_pat.sub("", cleaned).strip()

        download_dir = Path(__file__).parent / "downloads"
        # 文件附件（原逻辑）
        for m in file_pat.finditer(text):
            file_name = m.group(1)
            src = download_dir / file_name
            if not src.exists():
                print(f"[LarkBot] 待发送文件不存在: {src}")
                cleaned += f"\n（文件丢失：{file_name}）"
                continue
            file_key = self._http.upload_file(str(src), file_name)
            if file_key and self._http.send_file(chat_id, file_key):
                print(f"[LarkBot] 文件已发送到飞书: {file_name}")
            else:
                cleaned += f"\n（文件无法在飞书发送，本地路径：{src}）"
        # 图片消息（多图连发）
        for m in image_pat.finditer(text):
            file_name = m.group(1)
            src = download_dir / file_name
            if not src.exists():
                print(f"[LarkBot] 待发送图片不存在: {src}")
                cleaned += f"\n（图片丢失：{file_name}）"
                continue
            image_key = self._http.upload_image(str(src))
            if image_key and self._http.send_image(chat_id, image_key):
                print(f"[LarkBot] 图片已发送到飞书: {file_name}")
            else:
                cleaned += f"\n（图片无法在飞书发送，本地路径：{src}）"
        return cleaned

    # ── 构造输入并调 agent ───────────────────────
    def _run_agent(self, message_id, message_type, content, sender_id):
        attachment = ""
        user_text = ""

        if message_type == "text":
            user_text = (content.get("text") or "").strip()

        elif message_type == "file":
            file_key = content.get("file_key", "")
            bytes_ = self._http.download(message_id, file_key, "file") if file_key else None
            if bytes_ is None:
                return "文件下载失败，请稍后再试。"
            # 用消息里的文件名(带后缀)保存，方便 office_tools 识别类型
            fname = (content.get("file_name") or "file").strip()
            path = self._tmpdir / f"{int(time.time())}_{os.path.basename(fname or 'file')}"
            path.write_bytes(bytes_)
            attachment = f"[文件:{path}]"

        elif message_type == "image":
            image_key = content.get("image_key", "")
            bytes_ = self._http.download(message_id, image_key, "image") if image_key else None
            if bytes_ is None:
                return "图片下载失败，请稍后再试。"
            path = self._tmpdir / f"img_{int(time.time())}_{hash(image_key) & 0xffff}.png"
            path.write_bytes(bytes_)
            attachment = f"[图片:{path}]"

        else:
            return "暂时只支持文本、文件、图片消息哦。"

        # 拼成 agent.process 认识的输入：附件标记 + 用户文字
        parts = ([attachment] if attachment else []) + ([user_text] if user_text else [])
        user_input = " ".join(parts).strip() or "请分析以上内容"

        lark_uid = f"lark_{sender_id}" if sender_id else ""
        result = self._agent.process(
            user_input,
            override_uid=lark_uid,
            override_uname=f"飞书_{sender_id[:6]}" if sender_id else "",
        )
        return (
            result.get("response")
            or result.get("reply")
            or result.get("text")
            or "好的，我知道了。"
        )