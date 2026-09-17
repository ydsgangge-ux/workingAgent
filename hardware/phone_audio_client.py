"""
手机音频客户端 — IP Webcam 音频输入/输出
==========================================
输入：从手机麦克风拉取音频流（/audio.wav）
输出：推送 TTS 音频到手机扬声器播放（POST /audio）

IP Webcam 音频接口：
  /audio.wav   → 实时 WAV 音频流（16kHz, mono, 16bit）
  /audio.opus  → Opus 编码音频流（带宽更低）
  POST /audio  → 推送音频到手机扬声器播放
"""

import io
import time
import wave
import threading
import requests
from typing import Optional

import numpy as np


def _log(msg: str):
    print(f"[PhoneAudio] {msg}")


class PhoneAudioClient:
    """手机音频输入（麦克风）+ 输出（扬声器 TTS）"""

    def __init__(self, phone_url: str):
        self.url = phone_url.rstrip("/")
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── 音频输入：手机麦克风 → PCM bytes ──────────────

    def capture_audio(self, duration_seconds: float = 3.0) -> bytes:
        """
        从手机麦克风录制音频，返回 PCM 16kHz mono 16bit bytes。
        可直接送 STT 引擎。

        IP Webcam 的 /audio.wav 是实时流，需要按时间截断。
        """
        try:
            resp = requests.get(
                f"{self.url}/audio.wav",
                stream=True,
                timeout=duration_seconds + 5,
            )
            if resp.status_code != 200:
                _log(f"音频流请求失败: HTTP {resp.status_code}")
                return b""

            self._connected = True
            chunks = []
            start = time.time()

            for chunk in resp.iter_content(chunk_size=4096):
                chunks.append(chunk)
                if time.time() - start > duration_seconds:
                    break

            raw = b"".join(chunks)

            # IP Webcam 返回的 WAV 可能包含头部，需要提取纯 PCM
            pcm = self._extract_pcm_from_wav(raw)
            return pcm

        except requests.exceptions.ConnectionError:
            self._connected = False
            _log("手机连接断开")
        except Exception as e:
            _log(f"录音失败: {e}")
        return b""

    @staticmethod
    def _extract_pcm_from_wav(wav_bytes: bytes) -> bytes:
        """
        从 WAV bytes 中提取纯 PCM 数据。
        如果不是 WAV 格式（无头部），直接返回原始数据。
        """
        if len(wav_bytes) < 44:
            return wav_bytes

        # 检查 WAV 头部
        if wav_bytes[:4] == b'RIFF' and wav_bytes[8:12] == b'WAVE':
            # 找到 data chunk
            offset = 12
            while offset < len(wav_bytes) - 8:
                chunk_id = wav_bytes[offset:offset + 4]
                chunk_size = int.from_bytes(wav_bytes[offset + 4:offset + 8], 'little')
                if chunk_id == b'data':
                    return wav_bytes[offset + 8:offset + 8 + chunk_size]
                offset += 8 + chunk_size
            # 没找到 data chunk，返回 44 字节之后的数据
            return wav_bytes[44:]

        return wav_bytes

    # ── 音频输出：TTS → 手机扬声器 ────────────────────

    def play_tts(self, audio_bytes: bytes, content_type: str = "audio/wav"):
        """
        把 TTS 生成的音频推送到手机扬声器播放。

        audio_bytes: 音频数据（WAV 格式）
        content_type: 音频 MIME 类型
        """
        try:
            resp = requests.post(
                f"{self.url}/audio",
                data=audio_bytes,
                headers={"Content-Type": content_type},
                timeout=10,
            )
            if resp.status_code == 200:
                _log("TTS 音频已推送到手机扬声器")
            else:
                _log(f"推送失败: HTTP {resp.status_code}")
        except requests.exceptions.ConnectionError:
            self._connected = False
            _log("手机连接断开，无法播放 TTS")
        except Exception as e:
            _log(f"播放失败: {e}")

    def play_tts_mp3(self, mp3_bytes: bytes):
        """
        推送 MP3 格式的 TTS 音频到手机扬声器。
        IP Webcam 支持多种音频格式，MP3 也可以直接推送。
        """
        self.play_tts(mp3_bytes, content_type="audio/mpeg")

    # ── 连接检测 ──────────────────────────────────────

    def check_connection(self) -> bool:
        """检测手机音频接口是否可用"""
        try:
            resp = requests.head(f"{self.url}/audio.wav", timeout=3)
            self._connected = resp.status_code in (200, 206)
        except Exception:
            self._connected = False
        return self._connected


class PhoneAudioCapture:
    """
    手机音频采集器 — 兼容 AudioCaptureBase 接口。
    持续从 IP Webcam 拉取音频流，推入缓冲区，
    供 audio_pipeline.py 的 AudioPipeline 使用。

    使用方式：和 RTSPAudioCapture / LocalMicCapture 一样，
    通过 create_capture() 工厂函数创建。
    """

    def __init__(self, phone_url: str, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.phone_url = phone_url.rstrip("/")
        self._running = False
        self._connected = False
        self._buffer = bytearray()
        self._max_buffer = sample_rate * 2 * 30  # 30秒
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        _log(f"手机音频采集已启动 ({self.phone_url})")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._connected = False
        _log("手机音频采集已停止")

    @property
    def connected(self) -> bool:
        return self._connected

    def read(self, duration: float = 5.0) -> Optional[bytes]:
        """从缓冲区读取指定时长的 PCM 数据"""
        needed = int(self.sample_rate * duration * 2)
        with self._lock:
            if len(self._buffer) >= needed:
                data = bytes(self._buffer[:needed])
                self._buffer = self._buffer[needed:]
                return data
        return None

    def read_available(self, max_duration: float = 10.0) -> bytes:
        max_bytes = int(self.sample_rate * max_duration * 2)
        with self._lock:
            data = bytes(self._buffer[:max_bytes])
            self._buffer = self._buffer[len(data):]
        return data

    def buffer_duration(self) -> float:
        with self._lock:
            return len(self._buffer) / (self.sample_rate * 2)

    def _push_pcm(self, pcm: bytes):
        with self._lock:
            self._buffer.extend(pcm)
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[-self._max_buffer:]

    def _capture_loop(self):
        """
        持续从 IP Webcam 拉取音频流。

        策略：循环请求 /audio.wav，每次录制一小段（2秒），
        提取 PCM 数据推入缓冲区。

        IP Webcam 的 /audio.wav 是实时流，会持续发送数据。
        我们按时间截断，每次读取约2秒的音频，然后关闭连接，
        再发起下一次请求。
        """
        CHUNK_DURATION = 2.0  # 每次拉取2秒
        _reconnect_count = 0

        while self._running:
            try:
                resp = requests.get(
                    f"{self.phone_url}/audio.wav",
                    stream=True,
                    timeout=CHUNK_DURATION + 5,
                )
                if resp.status_code != 200:
                    self._connected = False
                    _reconnect_count += 1
                    if _reconnect_count <= 3:
                        _log(f"音频流请求失败: HTTP {resp.status_code}，3秒后重试...")
                    time.sleep(3)
                    continue

                self._connected = True
                _reconnect_count = 0
                chunks = []
                start = time.time()

                for chunk in resp.iter_content(chunk_size=4096):
                    if not self._running:
                        break
                    chunks.append(chunk)
                    if time.time() - start > CHUNK_DURATION:
                        break

                # 主动关闭连接，避免资源泄漏
                try:
                    resp.close()
                except Exception:
                    pass

                raw = b"".join(chunks)
                pcm = PhoneAudioClient._extract_pcm_from_wav(raw)

                if pcm and len(pcm) > 320:
                    # 如果采样率不是 16kHz，需要重采样
                    pcm = self._resample_if_needed(pcm)
                    self._push_pcm(pcm)

                # 短暂间隔，避免过于频繁请求
                time.sleep(0.1)

            except requests.exceptions.ConnectionError:
                self._connected = False
                _reconnect_count += 1
                if _reconnect_count <= 3:
                    _log("手机连接断开，3秒后重连...")
                time.sleep(3)
            except requests.exceptions.Timeout:
                # 超时不一定是断开，可能只是暂时无数据
                if not self._connected:
                    _reconnect_count += 1
                    if _reconnect_count <= 3:
                        _log("音频请求超时，3秒后重试...")
                time.sleep(3)
            except Exception as e:
                self._connected = False
                _reconnect_count += 1
                if _reconnect_count <= 3:
                    _log(f"音频采集异常: {e}，3秒后重试...")
                time.sleep(3)

    def _resample_if_needed(self, pcm: bytes) -> bytes:
        """
        如果音频不是 16kHz mono 16bit，进行重采样。
        IP Webcam 默认输出 16kHz mono 16bit，通常不需要重采样。
        """
        # 简单处理：直接返回，IP Webcam 默认就是 16kHz
        return pcm
