"""
小智 WebSocket 服务端
======================
监听局域网端口，接收小智设备的语音输入，
经 STT → A 层 → TTS 处理后返回给小智播放。

启动：
    python -m hardware.xiaozhi_server

测试（wscat）：
    npm install -g wscat
    wscat -c ws://localhost:8765
    > {"type":"hello","version":3,"transport":"websocket",
      "audio_params":{"format":"opus","sample_rate":16000,
      "channels":1,"frame_duration":60}}

小智配置页填写：
    ws://电脑局域网IP:8765
"""
import asyncio
import json
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from .audio_codec import OpusDecoder, OpusEncoder
from .xiaozhi_protocol import (
    build_hello,
    build_stt,
    build_tts_start,
    build_tts_stop,
    build_error,
    parse_message,
    is_listen_start,
    is_listen_stop,
)
from .stt_tts_bridge import STTTTSBridge

HOST = "0.0.0.0"
PORT = 8765


class XiaozhiSession:
    """单个小智设备的会话管理"""

    def __init__(self, websocket, bridge, decoder, encoder):
        self.ws = websocket
        self.bridge = bridge
        self.decoder = decoder
        self.encoder = encoder
        self.audio_buffer = bytearray()
        self.is_listening = False

    async def run(self):
        """会话主循环：握手 → 消息循环"""
        await self._handshake()
        print(f"[小智] 握手完成: {self.ws.remote_address}")

        async for message in self.ws:
            if isinstance(message, bytes):
                await self._handle_audio(message)
            else:
                await self._handle_control(message)

    async def _handshake(self):
        await self.ws.send(build_hello())

    async def _handle_control(self, message: str):
        data = parse_message(message)
        msg_type = data.get("type")

        if msg_type == "hello":
            # 小智可能重复发 hello，回复即可
            await self.ws.send(build_hello())

        elif is_listen_start(data):
            self.is_listening = True
            self.audio_buffer.clear()
            print(f"[小智] 开始接收语音 ─────────────────")

        elif is_listen_stop(data):
            self.is_listening = False
            print(f"[小智] 语音结束，音频 {len(self.audio_buffer)} bytes")
            await self._process_audio()

    async def _handle_audio(self, data: bytes):
        if self.is_listening:
            self.audio_buffer.extend(data)

    async def _process_audio(self):
        """完整处理：Opus解 → STT → A层 → TTS → Opus编 → 发送"""
        if not self.audio_buffer:
            return

        # 1. Opus → PCM
        pcm_data = self.decoder.decode(bytes(self.audio_buffer))
        if not pcm_data:
            print("[小智] Opus 解码为空")
            return

        # 2. STT：PCM → 文字
        user_text = await self.bridge.stt(pcm_data)
        if not user_text:
            print("[小智] STT 未识别到文字")
            await self.ws.send(build_tts_start("没听清，请再说一遍"))
            await self.ws.send(build_tts_stop())
            return

        # 3. 发送 STT 结果给小智屏幕显示
        await self.ws.send(build_stt(user_text))

        # 4. A 层：文字 → Levy 回复
        reply_text = await self.bridge.ask_levy(user_text)
        if not reply_text:
            print("[小智] A 层未返回回复")
            return

        # 5. 通知小智开始播放
        await self.ws.send(build_tts_start(reply_text))

        # 6. TTS：文字 → PCM → Opus 帧 → 逐帧发送
        pcm_audio = await self.bridge.tts(reply_text)
        if pcm_audio:
            opus_frames = self.encoder.encode(pcm_audio)
            print(f"[小智] TTS {len(opus_frames)} 帧 → 发送")
            for frame in opus_frames:
                await self.ws.send(frame)
        else:
            print("[小智] TTS 合成为空")

        # 7. 通知播放结束
        await self.ws.send(build_tts_stop())
        print(f"[小智] 回复完成 ─────────────────────")


class XiaozhiServer:
    """WebSocket 服务端，等待小智设备连接"""

    def __init__(self, agent=None):
        self.bridge = STTTTSBridge(agent=agent)
        self.decoder = OpusDecoder()
        self.encoder = OpusEncoder()

    async def handle_connection(self, websocket):
        addr = websocket.remote_address
        print(f"[小智] 设备连接: {addr}")
        session = XiaozhiSession(
            websocket, self.bridge, self.decoder, self.encoder
        )
        try:
            await session.run()
        except websockets.exceptions.ConnectionClosed:
            print(f"[小智] 设备断开: {addr}")
        except Exception as e:
            print(f"[小智] 连接异常: {e}")

    async def start(self):
        print(f"\n{'='*50}")
        print(f"  小智服务端启动")
        print(f"  监听: ws://{HOST}:{PORT}")
        print(f"  小智配置: ws://你的局域网IP:{PORT}")
        print(f"{'='*50}\n")
        async with websockets.serve(self.handle_connection, HOST, PORT):
            await asyncio.Future()  # 永久运行


def main():
    server = XiaozhiServer()
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
