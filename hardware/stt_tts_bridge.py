"""
stt_tts_bridge.py
对接现有 AGI-DPA 系统的三个接口
这是小智服务和现有系统之间的唯一对接文件

使用方式：
    1. 从 main.py 传入已初始化的 agent 实例
    2. bridge = STTTTSBridge(agent=agent)
    3. bridge.stt(pcm_data)  → 识别文字
    4. bridge.ask_levy(text) → Levy 回复
    5. bridge.tts(text)      → PCM 音频 bytes
"""

import asyncio
import json
import os
import tempfile

from engine.stt_engine import STTEngine


class STTTTSBridge:
    """
    小智 ↔ 现有 AGI-DPA 系统的桥接层。

    接收外部传入的 agent 引用（因为 ConsciousnessAgent 需要 8 个依赖，
    不能在此处 new 实例）。
    """

    def __init__(self, agent=None):
        self._agent = agent
        try:
            from desktop.config import load_config
            cfg = load_config()
            self._stt = STTEngine(cfg)
        except Exception:
            self._stt = STTEngine()

    # ──────────────────────────────────────────────────────
    # 接口1：STT  PCM → 文字
    # ──────────────────────────────────────────────────────

    async def stt(self, pcm_data: bytes) -> str:
        """PCM（16bit, 16000Hz, 单声道）→ 识别文字"""
        if not pcm_data:
            return ""

        try:
            result = await asyncio.to_thread(
                self._stt.recognize_bytes, pcm_data, "wav"
            )
            text = result.get("text", "").strip()
            if text:
                print(f"[Bridge/STT] 识别: {text}")
            return text
        except Exception as e:
            print(f"[Bridge/STT] 识别失败: {e}")
            return ""

    # ──────────────────────────────────────────────────────
    # 接口2：A 层  文字 → Levy 回复文字
    # ──────────────────────────────────────────────────────

    async def ask_levy(self, text: str) -> str:
        """用户文字 → Levy 回复"""
        if not self._agent:
            print("[Bridge/Agent] agent 未初始化")
            return "我还没准备好，请稍后再试。"

        if not text.strip():
            return ""

        try:
            result = await asyncio.to_thread(self._agent.process, text)
            reply = (
                result.get("response")
                or result.get("reply")
                or result.get("text")
                or ""
            )
            if reply:
                print(f"[Bridge/Agent] Levy: {reply[:60]}...")
            return reply
        except Exception as e:
            print(f"[Bridge/Agent] 处理失败: {e}")
            return "抱歉，我现在无法回答。"

    # ──────────────────────────────────────────────────────
    # 接口3：TTS  文字 → PCM bytes
    # ──────────────────────────────────────────────────────

    async def tts(self, text: str) -> bytes:
        """文字 → PCM（16bit, 16000Hz, 单声道）"""
        if not text.strip():
            return b""

        try:
            return await self._edge_tts_to_pcm(text)
        except Exception as e:
            print(f"[Bridge/TTS] 合成失败: {e}")
            return b""

    async def _edge_tts_to_pcm(self, text: str) -> bytes:
        """Edge-TTS 合成文字 → MP3 → ffmpeg 转 PCM"""
        import edge_tts

        voice = "zh-CN-XiaoxiaoNeural"
        communicate = edge_tts.Communicate(text, voice)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            mp3_path = f.name
        pcm_path = mp3_path.replace(".mp3", ".pcm")

        try:
            await communicate.save(mp3_path)

            import subprocess
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", mp3_path,
                    "-f", "s16le",
                    "-ar", "16000",
                    "-ac", "1",
                    pcm_path,
                ],
                capture_output=True,
                check=True,
            )

            with open(pcm_path, "rb") as f:
                return f.read()

        finally:
            for path in [mp3_path, pcm_path]:
                if os.path.exists(path):
                    os.unlink(path)
