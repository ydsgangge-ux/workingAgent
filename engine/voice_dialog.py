"""
语音对话集成模块（Voice Dialog）
将 STT + A层处理 + TTS 串联成完整的语音对话链路。

A 层对话入口完全不动：
  - 输入从键盘变成 STT 输出的文字
  - 输出送进 TTS 而不是直接显示

使用方式：
  dialog = VoiceDialog(agent=agent, config=config)
  dialog.start_listening()   # 开始监听麦克风
  dialog.stop()              # 停止

安全约束：
  - 挂掉不影响核心功能（STT/TTS 任何环节失败都静默降级）
  - 作为独立模块，不修改 agent.py 和 learner.py 主逻辑
"""

import os
import sys
import time
import threading
import tempfile
from typing import Optional, Callable, Dict
from enum import Enum


class DialogState(Enum):
    """语音对话状态"""
    IDLE = "idle"               # 空闲，等待唤醒
    LISTENING = "listening"     # 正在录音
    PROCESSING = "processing"   # A层处理中
    SPEAKING = "speaking"       # TTS 播放中
    ERROR = "error"             # 出错（静默恢复到 IDLE）


class VoiceDialog:
    """
    语音对话管理器
    STT → A层 process() → TTS 完整链路
    """

    def __init__(self, agent, config: dict = None):
        """
        Args:
            agent: ConsciousnessAgent 实例
            config: 配置字典
        """
        self.agent = agent
        self._config = config or {}

        self.state = DialogState.IDLE
        self._lock = threading.Lock()
        self._stop_flag = False

        # 回调
        self.on_text_recognized: Optional[Callable] = None    # STT 识别到文字时
        self.on_ai_response: Optional[Callable] = None       # A层生成回复时
        self.on_state_changed: Optional[Callable] = None     # 状态变化时

        # 配置
        self.enabled = self._config.get("voice_dialog_enabled", False)
        self.wake_word = self._config.get("voice_wake_word", "")  # 留空则使用按钮触发
        self.auto_speak = self._config.get("voice_auto_speak", True)
        self.listen_duration = self._config.get("voice_listen_duration", 5)
        self.stt_language = self._config.get("stt_language", "zh")

        # STT / TTS 引擎（懒初始化）
        self._stt = None
        self._tts = None

    def _get_stt(self):
        """懒初始化 STT 引擎"""
        if self._stt is None:
            try:
                from engine.stt_engine import STTEngine
                self._stt = STTEngine(self._config)
            except Exception as e:
                print(f"[VoiceDialog] STT 初始化失败: {e}")
        return self._stt

    def _get_tts(self):
        """懒初始化 TTS 引擎"""
        if self._tts is None:
            try:
                from engine.tts_engine import get_tts
                self._tts = get_tts()
            except Exception as e:
                print(f"[VoiceDialog] TTS 初始化失败: {e}")
        return self._tts

    def _set_state(self, new_state: DialogState):
        old = self.state
        self.state = new_state
        if old != new_state and self.on_state_changed:
            try:
                self.on_state_changed(old, new_state)
            except Exception:
                pass

    def is_available(self) -> bool:
        """检查语音对话是否可用"""
        stt = self._get_stt()
        tts = self._get_tts()
        return (stt and stt.is_available()) or (tts and tts.is_available())

    def speak_text(self, text: str, wait: bool = False):
        """
        直接朗读一段文字（TTS）
        不经过 A 层处理，用于主动消息朗读等场景
        """
        tts = self._get_tts()
        if not tts or not tts.is_available():
            print("[VoiceDialog] TTS 不可用，跳过朗读")
            return

        self._set_state(DialogState.SPEAKING)
        done_event = threading.Event()

        def _on_done():
            self._set_state(DialogState.IDLE)
            done_event.set()

        def _on_error(err):
            print(f"[VoiceDialog] TTS 播放失败: {err}")
            self._set_state(DialogState.IDLE)
            done_event.set()

        tts.speak(text, on_done=_on_done, on_error=_on_error)

        if wait:
            done_event.wait()

    def process_audio(self, audio_path: str) -> Dict:
        """
        处理一段音频：STT → A层 process() → TTS
        返回完整结果字典
        """
        if not self.agent:
            return {"ok": False, "error": "Agent 未初始化"}

        # ① STT：音频 → 文字
        self._set_state(DialogState.LISTENING)
        stt = self._get_stt()
        if not stt or not stt.is_available():
            self._set_state(DialogState.IDLE)
            return {"ok": False, "error": "STT 不可用"}

        stt_result = stt.recognize_file(audio_path)
        if not stt_result.get("ok"):
            self._set_state(DialogState.IDLE)
            return {"ok": False, "error": f"语音识别失败: {stt_result.get('error')}"}

        recognized_text = stt_result["text"]
        print(f"[VoiceDialog] 识别到: {recognized_text[:60]}...")

        if self.on_text_recognized:
            try:
                self.on_text_recognized(recognized_text)
            except Exception:
                pass

        # ② A层处理：文字 → 回复
        self._set_state(DialogState.PROCESSING)
        try:
            ai_result = self.agent.process(recognized_text)
            response_text = ai_result.get("response", "")
        except Exception as e:
            self._set_state(DialogState.IDLE)
            return {"ok": False, "error": f"A层处理失败: {e}"}

        if self.on_ai_response:
            try:
                self.on_ai_response(response_text)
            except Exception:
                pass

        # ③ TTS：回复 → 语音播放
        if self.auto_speak and response_text:
            self.speak_text(response_text, wait=False)

        return {
            "ok": True,
            "recognized_text": recognized_text,
            "response": response_text,
            "emotion": ai_result.get("emotion", {}),
            "backend": stt_result.get("backend", "unknown")
        }

    def listen_and_respond(self, duration: float = None) -> Dict:
        """
        录音 + 识别 + A层处理 + TTS 播放（一步到位）
        """
        if not self.agent:
            return {"ok": False, "error": "Agent 未初始化"}

        duration = duration or self.listen_duration

        # ① 录音
        self._set_state(DialogState.LISTENING)
        try:
            from engine.stt_engine import record_audio
            audio_path = record_audio(duration=duration)
            if not audio_path:
                self._set_state(DialogState.IDLE)
                return {"ok": False, "error": "录音失败，请检查麦克风"}
        except Exception as e:
            self._set_state(DialogState.IDLE)
            return {"ok": False, "error": f"录音异常: {e}"}

        # ② 处理音频（STT + A层 + TTS）
        try:
            result = self.process_audio(audio_path)
            return result
        finally:
            # 清理临时文件
            try:
                os.unlink(audio_path)
            except Exception:
                pass

    # ── 持续对话循环 ────────────────────────────────────

    def start_listening_loop(self):
        """
        启动持续监听循环（后台线程）
        每次录音 → 识别 → 处理 → 播放 → 等待 → 下一次
        """
        self._stop_flag = False

        def _loop():
            print("[VoiceDialog] 持续监听已启动")
            while not self._stop_flag:
                try:
                    result = self.listen_and_respond()
                    if not result.get("ok"):
                        print(f"[VoiceDialog] 本次对话失败: {result.get('error')}")
                except Exception as e:
                    print(f"[VoiceDialog] 循环异常: {e}")

                # 等待 TTS 播放完成 + 1秒间隔
                if self.state == DialogState.SPEAKING:
                    time.sleep(1)
                else:
                    time.sleep(0.5)

            print("[VoiceDialog] 持续监听已停止")

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        return thread

    def stop(self):
        """停止监听"""
        self._stop_flag = True
        self._set_state(DialogState.IDLE)
        tts = self._get_tts()
        if tts:
            tts.stop()

    def get_status(self) -> Dict:
        """获取当前状态"""
        stt = self._get_stt()
        tts = self._get_tts()
        return {
            "state": self.state.value,
            "stt_available": stt.is_available() if stt else False,
            "stt_backend": stt.get_backend_name() if stt else "未初始化",
            "tts_available": tts.is_available() if tts else False,
            "tts_backend": tts.get_backend_name() if tts else "未初始化",
            "auto_speak": self.auto_speak,
            "listen_duration": self.listen_duration,
        }

    @staticmethod
    def install_guide() -> str:
        return (
            "语音对话完整安装指南：\n\n"
            "1. 语音识别（STT）：\n"
            "   pip install websocket-client sounddevice\n"
            "   或 pip install openai-whisper\n\n"
            "2. 语音合成（TTS）：\n"
            "   pip install edge-tts\n\n"
            "3. 在 config.json 中配置：\n"
            "   voice_dialog_enabled: true\n"
            "   stt_provider: \"deepseek\" (或 \"xunfei\" / \"whisper_local\")\n"
            "   voice_auto_speak: true\n"
        )


# ── 全局单例 ────────────────────────────────────────────

_voice_dialog_instance: Optional[VoiceDialog] = None


def get_voice_dialog(agent=None, config: dict = None) -> Optional[VoiceDialog]:
    global _voice_dialog_instance
    if _voice_dialog_instance is None and agent:
        _voice_dialog_instance = VoiceDialog(agent=agent, config=config or {})
    return _voice_dialog_instance
