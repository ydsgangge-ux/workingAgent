"""
phone_ws_server.py
手机摄像头 WebSocket 服务端
============================
云端 AGI-DPA 通过此模块主动控制外出手机的摄像头。

架构：
  手机 Termux → phone_client.py → WebSocket → 本模块 → HACamera

修复设计文档问题：
  1. 使用 asyncio.get_running_loop() 替代废弃的 get_event_loop()
  2. 连接时校验 token（与 AGI_INVITE_CODE 共用）
  3. 提供 sync_capture() 同步接口供 HACamera 调用
  4. 图片用二进制帧传输，避免 base64 膨胀
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

try:
    import websockets
except ImportError:
    websockets = None
    print("[PhoneWS] websockets 未安装，手机外出功能不可用。pip install websockets")


# 连接 token（与邀请码共用，环境变量可覆盖）
WS_TOKEN = os.environ.get("AGI_WS_TOKEN", "agi2025202620272028")


class PhoneWSServer:
    """
    WebSocket 服务端
    管理手机连接，提供主动截图接口给系统调用
    """

    def __init__(self, host="0.0.0.0", port=18766):
        self.host = host
        self.port = port
        self._connection = None          # 当前连接的 WebSocket
        self._pending = {}                # request_id → asyncio.Future (截图)
        self._pending_sensor_requests = {}  # request_id → asyncio.Future (传感器)
        self._server = None               # websockets.Server
        self._loop = None                 # 事件循环引用
        self._connected_since = None      # 连接时间
        self._capture_count = 0           # 截图计数

    # ── 连接处理 ─────────────────────────────────────

    async def handle_phone(self, websocket):
        """处理手机连接"""
        # 认证：第一条消息必须是 auth 或 token
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            auth_msg = json.loads(raw)
            auth_type = auth_msg.get("type", "")
            if auth_type not in ("auth", "token") or auth_msg.get("token") != WS_TOKEN:
                print(f"[PhoneWS] 认证失败，断开连接")
                await websocket.close(4001, "认证失败")
                return
        except asyncio.TimeoutError:
            print("[PhoneWS] 认证超时，断开连接")
            await websocket.close(4002, "认证超时")
            return
        except Exception as e:
            print(f"[PhoneWS] 认证异常: {e}")
            await websocket.close(4003, "认证异常")
            return

        self._connection = websocket
        self._connected_since = datetime.now()
        print(f"[PhoneWS] 手机已连接: {websocket.remote_address}")

        # 通知认证成功
        try:
            await websocket.send(json.dumps({"type": "auth_ok"}))
        except websockets.exceptions.ConnectionClosed:
            print("[PhoneWS] 手机认证后立即断开")
            self._connection = None
            self._connected_since = None
            return

        try:
            async for message in websocket:
                await self._handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            print("[PhoneWS] 手机连接断开")
        finally:
            self._connection = None
            self._connected_since = None

    async def _handle_message(self, message):
        """处理手机返回的消息"""
        try:
            # 二进制帧 = 图片数据
            if isinstance(message, bytes):
                # 格式：前8字节 = request_id(ASCII)，后面 = JPEG bytes
                if len(message) > 8:
                    request_id = message[:8].decode("ascii", errors="ignore").strip()
                    img_bytes = message[8:]
                    if request_id in self._pending:
                        future = self._pending.pop(request_id)
                        if not future.done():
                            future.set_result(img_bytes)
                return

            # 文本帧 = JSON 控制消息
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "frame_response":
                # 兼容 base64 模式（旧客户端）
                request_id = data.get("request_id", "")
                image_b64 = data.get("image", "")
                if request_id in self._pending and image_b64:
                    import base64
                    future = self._pending.pop(request_id)
                    if not future.done():
                        img_bytes = base64.b64decode(image_b64)
                        future.set_result(img_bytes)

            elif msg_type == "ping":
                if self._connection:
                    await self._connection.send(json.dumps({"type": "pong"}))

            elif msg_type == "sensor_data":
                # 手机传感器数据（GPS、电量等），后续扩展
                print(f"[PhoneWS] 收到传感器数据: {list(data.keys())}")

            elif msg_type == "sensor_response":
                # 传感器请求的响应
                request_id = data.get("request_id", "")
                sensor_data = data.get("data", {})
                gps = sensor_data.get("gps", {})
                print(f"[PhoneWS] sensor_response GPS: lat={gps.get('lat')} lng={gps.get('lng')}")
                if request_id in self._pending_sensor_requests:
                    future = self._pending_sensor_requests.pop(request_id)
                    if not future.done():
                        future.set_result(sensor_data)

        except Exception as e:
            print(f"[PhoneWS] 消息处理错误: {e}")

    # ── 截图接口 ─────────────────────────────────────

    async def capture(self, timeout: float = 10.0) -> Optional[bytes]:
        """
        主动向手机请求截图（异步）
        返回 JPEG bytes，超时或手机未连接返回 None
        """
        if self._connection is None:
            return None

        request_id = datetime.now().strftime("%H%M%S%f")[:8]

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future

        try:
            await self._connection.send(json.dumps({
                "type": "capture_request",
                "request_id": request_id,
            }))
        except Exception as e:
            print(f"[PhoneWS] 发送指令失败: {e}")
            self._pending.pop(request_id, None)
            return None

        try:
            img_bytes = await asyncio.wait_for(future, timeout=timeout)
            self._capture_count += 1
            print(f"[PhoneWS] 截图成功: {len(img_bytes)} bytes (第{self._capture_count}次)")
            return img_bytes
        except asyncio.TimeoutError:
            print(f"[PhoneWS] 截图超时（{timeout}s）")
            self._pending.pop(request_id, None)
            return None

    def sync_capture(self, timeout: float = 10.0) -> Optional[bytes]:
        """
        同步截图接口（供 HACamera 等同步代码调用）

        如果在事件循环线程中调用，会用 run_coroutine_threadsafe 桥接。
        如果事件循环未运行，返回 None。
        """
        if self._loop is None or not self._loop.is_running():
            return None

        future = asyncio.run_coroutine_threadsafe(self.capture(timeout), self._loop)
        try:
            return future.result(timeout=timeout + 2)
        except Exception as e:
            print(f"[PhoneWS] sync_capture 失败: {e}")
            return None

    # ── 传感器接口 ─────────────────────────────────────

    async def get_sensor_data(self, timeout: float = 8.0) -> dict:
        """
        主动向手机请求传感器数据
        返回 dict，超时或失败返回空 dict
        """
        if self._connection is None:
            print("[PhoneWS] 手机未连接，无法获取传感器数据")
            return {}

        request_id = datetime.now().strftime("%H%M%S%f")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_sensor_requests[request_id] = future

        try:
            await self._connection.send(json.dumps({
                "type": "sensor_request",
                "request_id": request_id
            }))
        except Exception as e:
            print(f"[PhoneWS] 发送传感器请求失败: {e}")
            self._pending_sensor_requests.pop(request_id, None)
            return {}

        try:
            sensor_data = await asyncio.wait_for(future, timeout=timeout)
            print(f"[PhoneWS] 传感器数据获取成功: {list(sensor_data.keys())}")
            return sensor_data
        except asyncio.TimeoutError:
            print(f"[PhoneWS] 传感器数据请求超时（{timeout}s）")
            self._pending_sensor_requests.pop(request_id, None)
            return {}

    # ── 状态查询 ─────────────────────────────────────

    def is_connected(self) -> bool:
        """手机是否已连接"""
        return self._connection is not None

    def get_status(self) -> dict:
        """获取连接状态"""
        return {
            "connected": self.is_connected(),
            "connected_since": str(self._connected_since) if self._connected_since else None,
            "capture_count": self._capture_count,
            "port": self.port,
        }

    # ── 启动 ─────────────────────────────────────────

    async def start(self):
        """启动 WebSocket 服务"""
        if websockets is None:
            print("[PhoneWS] websockets 未安装，跳过启动")
            return

        self._loop = asyncio.get_running_loop()
        print(f"[PhoneWS] 服务启动，监听 ws://{self.host}:{self.port}")
        try:
            async with websockets.serve(self.handle_phone, self.host, self.port):
                await asyncio.Future()  # 永久运行
        except asyncio.CancelledError:
            print("[PhoneWS] 服务正在关闭...")
        except Exception as e:
            print(f"[PhoneWS] 服务异常: {e}")


# ── 全局单例 ────────────────────────────────────────────────

_phone_server: Optional[PhoneWSServer] = None


def get_phone_server() -> Optional[PhoneWSServer]:
    """获取全局 PhoneWSServer 实例"""
    return _phone_server


def create_phone_server(host="0.0.0.0", port=18766) -> PhoneWSServer:
    """创建全局 PhoneWSServer 实例（只应调用一次）"""
    global _phone_server
    _phone_server = PhoneWSServer(host=host, port=port)
    return _phone_server
