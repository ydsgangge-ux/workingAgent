"""
音频管线 v4 — 多源 + 状态机 + 唤醒词 + VAD + STT
==================================================
音频源：
  rtsp     → 摄像头 RTSP 音频流（PyAV 解码）
  mic      → 本地麦克风（sounddevice）
  wyoming  → M5Stack Atom Echo / Wyoming 卫星（TCP）

三种模式：
  待机 (standby)  → 只做唤醒词检测（VAD + 短片段 STT）
  对话 (dialog)   → 检测到唤醒词后，完整 STT 处理
  任务 (task)     → 执行任务时，持续监听指令
"""

import io
import time
import wave
import socket
import struct
import threading
import numpy as np
from pathlib import Path
from typing import Optional, Callable
from enum import Enum


def _log(msg: str):
    print(f"[AudioPipeline] {msg}")


class PipelineMode(Enum):
    STANDBY = "standby"
    DIALOG = "dialog"
    TASK = "task"


# ── 音频采集基类 ──────────────────────────────────────

class AudioCaptureBase:
    """音频采集基类，统一 read/buffer_duration/connected 接口"""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._running = False
        self._buffer = bytearray()
        self._max_buffer = sample_rate * 2 * 30
        self._lock = threading.Lock()
        self._connected = False

    def start(self):
        raise NotImplementedError

    def stop(self):
        self._running = False
        _log("音频采集已停止")

    def read(self, duration: float = 5.0) -> Optional[bytes]:
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

    @property
    def connected(self) -> bool:
        return self._connected

    def _push_pcm(self, pcm: bytes):
        with self._lock:
            self._buffer.extend(pcm)
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[-self._max_buffer:]


# ── RTSP 音频采集 ─────────────────────────────────────

class RTSPAudioCapture(AudioCaptureBase):
    """从 RTSP 流中持续读取音频帧"""

    def __init__(self, rtsp_url: str, sample_rate: int = 16000):
        super().__init__(sample_rate)
        self.rtsp_url = rtsp_url
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        _log(f"RTSP 音频采集已启动 ({self.rtsp_url[:40]}...)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        _log("RTSP 音频采集已停止")

    def _capture_loop(self):
        while self._running:
            try:
                import av
                container = av.open(
                    self.rtsp_url,
                    options={"rtsp_transport": "tcp", "stimeout": "5000000"},
                    timeout=10.0,
                )
                audio_stream = None
                for s in container.streams:
                    if s.type == "audio":
                        audio_stream = s
                        break
                if audio_stream is None:
                    _log("RTSP 流中无音频通道")
                    container.close()
                    self._connected = False
                    time.sleep(10)
                    continue

                _log(f"RTSP 音频流已连接: {audio_stream.codec_context.name}, "
                     f"{audio_stream.codec_context.sample_rate}Hz")
                self._connected = True

                resampler = av.AudioResampler(
                    format="s16", layout="mono", rate=self.sample_rate,
                )
                for packet in container.demux(audio_stream):
                    if not self._running:
                        break
                    try:
                        for frame in packet.decode():
                            for rf in resampler.resample(frame):
                                self._push_pcm(bytes(rf.planes[0]))
                    except Exception:
                        continue

                container.close()
                self._connected = False
            except Exception as e:
                self._connected = False
                _log(f"RTSP 音频采集异常: {e}，3秒后重连...")
                time.sleep(3)


# ── 本地麦克风采集 ────────────────────────────────────

class LocalMicCapture(AudioCaptureBase):
    """使用 sounddevice 从本地麦克风采集音频"""

    def __init__(self, device_index: int = None, sample_rate: int = 16000):
        super().__init__(sample_rate)
        self._device_index = device_index
        self._stream = None

    def start(self):
        if self._running:
            return
        try:
            import sounddevice as sd
            self._running = True
            self._stream = sd.InputStream(
                device=self._device_index,
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=int(self.sample_rate * 0.1),
                callback=self._audio_callback,
            )
            self._stream.start()
            self._connected = True
            dev_name = sd.query_devices(self._device_index)["name"] if self._device_index is not None else "默认"
            _log(f"本地麦克风已启动: {dev_name} @ {self.sample_rate}Hz")
        except Exception as e:
            self._running = False
            self._connected = False
            _log(f"本地麦克风启动失败: {e}")

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._connected = False
        _log("本地麦克风已停止")

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            pass
        pcm = indata.flatten().tobytes()
        self._push_pcm(pcm)

    @staticmethod
    def list_devices():
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            inputs = []
            for i, d in enumerate(devices):
                if d["max_input_channels"] > 0:
                    inputs.append({"index": i, "name": d["name"],
                                   "channels": d["max_input_channels"]})
            return inputs
        except Exception:
            return []


# ── Wyoming 卫星采集（M5Stack Atom Echo）───────────────

class WyomingCapture(AudioCaptureBase):
    """
    Wyoming 协议音频采集
    用于接收 M5Stack Atom Echo（ESPHome Wyoming 卫星）推送的音频流

    Wyoming 协议：
      1. TCP 连接建立后，卫星发送 Hello 事件
      2. 服务端回复 Hello
      3. 卫星发送 AudioStart 事件
      4. 卫星持续发送 AudioChunk 事件（PCM 16kHz mono 16bit）
      5. 卫星发送 AudioStop 事件
    """

    WYOMING_EVENT = {
        "HELLO": 1, "AUDIO_START": 2, "AUDIO_CHUNK": 3,
        "AUDIO_STOP": 4, "PING": 5, "PONG": 6,
    }

    def __init__(self, host: str = "0.0.0.0", port: int = 10600,
                 sample_rate: int = 16000):
        super().__init__(sample_rate)
        self._host = host
        self._port = port
        self._server_thread: Optional[threading.Thread] = None
        self._server_socket = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self._server_thread.start()
        _log(f"Wyoming 服务已启动，监听 {self._host}:{self._port}")

    def stop(self):
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
        _log("Wyoming 服务已停止")

    def _server_loop(self):
        while self._running:
            try:
                self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._server_socket.settimeout(1.0)
                self._server_socket.bind((self._host, self._port))
                self._server_socket.listen(1)
                _log(f"Wyoming 等待卫星连接 ({self._host}:{self._port})...")

                while self._running:
                    try:
                        client, addr = self._server_socket.accept()
                        _log(f"Wyoming 卫星已连接: {addr}")
                        self._connected = True
                        self._handle_client(client)
                    except socket.timeout:
                        continue
                    except Exception:
                        break

            except Exception as e:
                self._connected = False
                _log(f"Wyoming 服务异常: {e}，3秒后重试...")
                time.sleep(3)
            finally:
                try:
                    if self._server_socket:
                        self._server_socket.close()
                except Exception:
                    pass

    def _handle_client(self, client: socket.socket):
        client.settimeout(0.5)
        buffer = b""
        try:
            while self._running:
                try:
                    data = client.recv(4096)
                    if not data:
                        break
                    buffer += data

                    while len(buffer) >= 4:
                        event_type = struct.unpack("B", buffer[0:1])[0]
                        payload_len = struct.unpack(">I", buffer[1:5])[0] if len(buffer) >= 5 else 0

                        if event_type == self.WYOMING_EVENT["HELLO"]:
                            if len(buffer) >= 5 + payload_len:
                                buffer = buffer[5 + payload_len:]
                                self._send_wyoming_event(client, "HELLO", b'{"wyoming_version":1}')
                                _log("Wyoming: Hello 握手完成")
                            else:
                                break

                        elif event_type == self.WYOMING_EVENT["AUDIO_START"]:
                            if len(buffer) >= 5 + payload_len:
                                buffer = buffer[5 + payload_len:]
                                _log("Wyoming: 音频流开始")
                            else:
                                break

                        elif event_type == self.WYOMING_EVENT["AUDIO_CHUNK"]:
                            if len(buffer) >= 5 + payload_len:
                                pcm = buffer[5:5 + payload_len]
                                buffer = buffer[5 + payload_len:]
                                self._push_pcm(pcm)
                            else:
                                break

                        elif event_type == self.WYOMING_EVENT["AUDIO_STOP"]:
                            if len(buffer) >= 5 + payload_len:
                                buffer = buffer[5 + payload_len:]
                                _log("Wyoming: 音频流结束")
                            else:
                                break

                        elif event_type == self.WYOMING_EVENT["PING"]:
                            if len(buffer) >= 5 + payload_len:
                                buffer = buffer[5 + payload_len:]
                                self._send_wyoming_event(client, "PONG", b"")
                            else:
                                break

                        else:
                            if len(buffer) >= 5 + payload_len:
                                buffer = buffer[5 + payload_len:]
                            else:
                                break

                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            self._connected = False
            try:
                client.close()
            except Exception:
                pass
            _log("Wyoming: 卫星断开连接")

    def _send_wyoming_event(self, sock: socket.socket, event_name: str,
                            payload: bytes):
        event_type = self.WYOMING_EVENT.get(event_name, 0)
        header = struct.pack("B", event_type) + struct.pack(">I", len(payload))
        try:
            sock.sendall(header + payload)
        except Exception:
            pass


# ── 音频源工厂 ────────────────────────────────────────

def create_capture(config: dict) -> AudioCaptureBase:
    """根据配置创建音频采集实例"""
    source = config.get("audio_source", "rtsp")
    sample_rate = config.get("audio_sample_rate", 16000)

    if source == "mic":
        device_index = config.get("mic_device_index", None)
        return LocalMicCapture(device_index=device_index, sample_rate=sample_rate)

    elif source == "wyoming":
        host = config.get("wyoming_host", "0.0.0.0")
        port = config.get("wyoming_port", 10600)
        return WyomingCapture(host=host, port=port, sample_rate=sample_rate)

    elif source == "phone":
        phone_url = config.get("phone_url", "")
        if not phone_url:
            _log("手机 URL 为空，回退到本地麦克风")
            return LocalMicCapture(sample_rate=sample_rate)
        from .phone_audio_client import PhoneAudioCapture
        return PhoneAudioCapture(phone_url=phone_url, sample_rate=sample_rate)

    else:
        rtsp_url = config.get("rtsp_url", "")
        if not rtsp_url:
            _log("RTSP URL 为空，回退到本地麦克风")
            return LocalMicCapture(sample_rate=sample_rate)
        return RTSPAudioCapture(rtsp_url, sample_rate)


# ── VAD ───────────────────────────────────────────────

class SimpleVAD:
    """能量 + webrtcvad 双模式 VAD"""

    def __init__(self, energy_threshold: float = 300.0,
                 silence_duration: float = 1.0,
                 min_speech_duration: float = 0.5,
                 sample_rate: int = 16000):
        self.energy_threshold = energy_threshold
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration
        self.sample_rate = sample_rate
        self._webrtcvad = None
        try:
            import webrtcvad
            self._webrtcvad = webrtcvad.Vad(2)
        except ImportError:
            pass

    def is_speech(self, pcm_data: bytes) -> bool:
        if self._webrtcvad:
            return self._webrtcvad_check(pcm_data)
        return self._energy_check(pcm_data)

    def _energy_check(self, pcm_data: bytes) -> bool:
        if len(pcm_data) < 2:
            return False
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(samples ** 2))
        return rms > self.energy_threshold

    def get_energy(self, pcm_data: bytes) -> float:
        if len(pcm_data) < 2:
            return 0.0
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(samples ** 2)))

    def _webrtcvad_check(self, pcm_data: bytes) -> bool:
        try:
            frame_duration = 30
            frame_size = int(self.sample_rate * frame_duration / 1000) * 2
            speech_frames = 0
            total_frames = 0
            for i in range(0, len(pcm_data) - frame_size + 1, frame_size):
                frame = pcm_data[i:i + frame_size]
                if self._webrtcvad.is_speech(frame, self.sample_rate):
                    speech_frames += 1
                total_frames += 1
            if total_frames == 0:
                return False
            return speech_frames / total_frames > 0.3
        except Exception:
            return self._energy_check(pcm_data)


# ── 唤醒词检测 ────────────────────────────────────────

class WakeWordDetector:
    DEFAULT_KEYWORDS = ["levy", "小乐", "雷维", "你好"]

    def __init__(self, keywords: list = None, sample_rate: int = 16000):
        self.keywords = [k.lower() for k in (keywords or self.DEFAULT_KEYWORDS)]
        self.sample_rate = sample_rate
        self._sherpa_kws = None
        self._stt_engine = None
        self._stt_init_attempted = False
        self._stt_broken = False
        self._init_sherpa()

    def _init_sherpa(self):
        try:
            import sherpa_onnx
            model_dir = Path(__file__).parent.parent / "models" / "sherpa-onnx-kws-zh"
            tokens = model_dir / "tokens.txt"
            encoder = model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
            decoder = model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
            joiner = model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"

            if not all(p.exists() for p in [tokens, encoder, decoder, joiner]):
                _log("sherpa-onnx KWS 模型未找到，使用 STT 回退方案")
                return

            keywords_file = model_dir / "keywords.txt"
            self._write_keywords_file(keywords_file)

            self._sherpa_kws = sherpa_onnx.KeywordSpotter(
                tokens=str(tokens), encoder=str(encoder),
                decoder=str(decoder), joiner=str(joiner),
                keywords_file=str(keywords_file),
                num_threads=1, keywords_threshold=0.25, provider="cpu",
            )
            _log(f"sherpa-onnx 唤醒词检测已初始化: {self.keywords}")
        except Exception as e:
            _log(f"sherpa-onnx 初始化失败: {e}，使用 STT 回退方案")

    def _write_keywords_file(self, path: Path):
        try:
            import sherpa_onnx
            input_file = path.parent / "keywords_raw.txt"
            with open(input_file, "w", encoding="utf-8") as f:
                for kw in self.keywords:
                    f.write(f"{kw} :2.0 #0.5 @{kw}\n")
            sherpa_onnx.text2token(
                str(input_file), str(path),
                tokens=str(path.parent / "tokens.txt"), tokens_type="ppinyin",
            )
        except Exception:
            with open(path, "w", encoding="utf-8") as f:
                for kw in self.keywords:
                    f.write(f"{kw} :2.0 #0.5\n")

    def detect_sherpa(self, pcm_data: bytes) -> Optional[str]:
        if not self._sherpa_kws:
            return None
        try:
            stream = self._sherpa_kws.create_stream()
            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            stream.accept_waveform(self.sample_rate, samples)
            result = self._sherpa_kws.decode(stream)
            if result:
                _log(f"sherpa 唤醒词: {result}")
                return result
        except Exception as e:
            _log(f"sherpa 检测异常: {e}")
        return None

    def _get_stt(self):
        if self._stt_engine is None and not self._stt_init_attempted:
            self._stt_init_attempted = True
            try:
                from engine.stt_engine import STTEngine
                from desktop.config import load_config
                cfg = load_config()
                cfg["stt_provider"] = "faster_whisper"
                cfg["whisper_model"] = cfg.get("whisper_model", "base")
                self._stt_engine = STTEngine(cfg)
                _log("唤醒词检测: STT 引擎已初始化 (faster_whisper)")
            except Exception as e:
                _log(f"唤醒词检测: STT 引擎初始化失败: {e}")
                self._stt_broken = True
        return self._stt_engine

    def detect_stt(self, pcm_data: bytes) -> Optional[str]:
        if getattr(self, '_stt_broken', False):
            return None
        stt = self._get_stt()
        if not stt:
            self._stt_broken = True
            return None
        wav_bytes = AudioPipeline._pcm_to_wav(pcm_data)
        try:
            result = stt.recognize_bytes(wav_bytes, "wav")
            text = result.get("text", "").strip().lower()
            if text:
                _log(f"唤醒词检测: STT 识别到 '{text[:60]}'")
                for kw in self.keywords:
                    if kw.lower() in text:
                        _log(f"唤醒词匹配: '{text}' → 关键词 '{kw}'")
                        return kw
        except Exception as e:
            _log(f"唤醒词检测: STT 异常 {e}")
            self._stt_broken = True
        return None

    def detect(self, pcm_data: bytes) -> Optional[str]:
        result = self.detect_sherpa(pcm_data)
        if result:
            return result
        return self.detect_stt(pcm_data)


# ── 音频管线 ──────────────────────────────────────────

class AudioPipeline:
    """
    状态机音频管线 v4

    支持三种音频源：RTSP / 本地麦克风 / Wyoming 卫星
    模式切换：待机 → (唤醒词) → 对话 → (超时) → 待机
    """

    CHUNK_DURATION = 0.5
    MAX_SEGMENT_DURATION = 15.0
    DIALOG_TIMEOUT = 30.0
    STANDBY_WINDOW_SECONDS = 5.0
    STANDBY_POLL_INTERVAL = 0.3
    STANDBY_SPEECH_FRAMES = 3

    def __init__(self, audio_source: str = "rtsp",
                 rtsp_url: str = "",
                 mic_device_index: int = None,
                 wyoming_host: str = "0.0.0.0",
                 wyoming_port: int = 10600,
                 phone_url: str = "",
                 sample_rate: int = 16000,
                 wake_words: list = None):
        self.sample_rate = sample_rate
        self.audio_source = audio_source

        self.on_speech: Optional[Callable[[str], None]] = None
        self.on_wake: Optional[Callable[[], None]] = None
        self.on_mode_change: Optional[Callable[[str], None]] = None

        capture_config = {
            "audio_source": audio_source,
            "rtsp_url": rtsp_url,
            "mic_device_index": mic_device_index,
            "wyoming_host": wyoming_host,
            "wyoming_port": wyoming_port,
            "phone_url": phone_url,
            "audio_sample_rate": sample_rate,
        }
        self._capture = create_capture(capture_config)
        vad_threshold = 1500.0 if audio_source == "mic" else (800.0 if audio_source == "phone" else 300.0)
        self._vad = SimpleVAD(sample_rate=sample_rate, energy_threshold=vad_threshold)
        self._wake_detector = WakeWordDetector(
            keywords=wake_words or WakeWordDetector.DEFAULT_KEYWORDS,
            sample_rate=sample_rate,
        )
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._mode = PipelineMode.STANDBY
        self._speech_buffer = bytearray()
        self._silence_chunks = 0
        self._speech_chunks = 0
        self._in_speech = False
        self._last_speech_time = 0.0
        self._stt_engine = None

        self._standby_window = bytearray()
        self._standby_window_max = sample_rate * 2 * int(self.STANDBY_WINDOW_SECONDS)
        self._last_wake_check_time = 0.0
        self._wake_check_cooldown = 5.0
        self._stt_fail_count = 0
        self._stt_fail_cooldown_until = 0.0
        self._standby_speech_frames = 0

    @property
    def mode(self) -> str:
        return self._mode.value

    def set_mode(self, mode: str):
        new_mode = PipelineMode(mode)
        if new_mode != self._mode:
            old = self._mode.value
            self._mode = new_mode
            _log(f"模式切换: {old} → {new_mode.value}")
            if self.on_mode_change:
                try:
                    self.on_mode_change(new_mode.value)
                except Exception:
                    pass

    def start(self):
        if self._running:
            return
        self._running = True
        self._capture.start()
        self._thread = threading.Thread(target=self._pipeline_loop, daemon=True)
        self._thread.start()
        _log(f"音频管线 v4 已启动（{self.audio_source}，待机模式）")

    def stop(self):
        self._running = False
        self._capture.stop()
        if self._thread:
            self._thread.join(timeout=5)
        _log("音频管线已停止")

    def start_task(self):
        self.set_mode("task")

    def end_task(self):
        self.set_mode("standby")

    def _get_stt(self):
        if self._stt_engine is None:
            try:
                from engine.stt_engine import STTEngine
                from desktop.config import load_config
                cfg = load_config()
                cfg["stt_provider"] = "faster_whisper"
                cfg["whisper_model"] = cfg.get("whisper_model", "base")
                self._stt_engine = STTEngine(cfg)
            except Exception as e:
                _log(f"STT 引擎初始化失败: {e}")
        return self._stt_engine

    def _pipeline_loop(self):
        if self.audio_source == "rtsp":
            _log("等待 RTSP 连接...")
            for _ in range(30):
                if not self._running:
                    return
                if self._capture.connected:
                    break
                time.sleep(1)
            if not self._capture.connected:
                _log("RTSP 连接超时，但继续尝试...")
        elif self.audio_source == "phone":
            _log("等待手机连接...")
            for _ in range(30):
                if not self._running:
                    return
                if self._capture.connected:
                    break
                time.sleep(1)
            if not self._capture.connected:
                _log("手机连接超时，但继续尝试...")
        else:
            time.sleep(1)

        _log("管线主循环开始")

        while self._running:
            try:
                if self._mode == PipelineMode.STANDBY:
                    self._loop_standby()
                elif self._mode == PipelineMode.DIALOG:
                    self._loop_dialog()
                elif self._mode == PipelineMode.TASK:
                    self._loop_task()
            except Exception as e:
                _log(f"管线异常: {e}")
                time.sleep(1)

    def _loop_standby(self):
        chunk = self._capture.read(duration=self.CHUNK_DURATION)
        if not chunk or len(chunk) < 320:
            time.sleep(self.STANDBY_POLL_INTERVAL)
            self._standby_speech_frames = 0
            return

        self._standby_window.extend(chunk)
        if len(self._standby_window) > self._standby_window_max:
            self._standby_window = self._standby_window[-self._standby_window_max:]

        is_speech = self._vad.is_speech(chunk)

        if not is_speech:
            self._standby_speech_frames = 0
            return

        self._standby_speech_frames += 1
        if self._standby_speech_frames < self.STANDBY_SPEECH_FRAMES:
            return

        now = time.time()
        if now < self._stt_fail_cooldown_until:
            return
        if now - self._last_wake_check_time < self._wake_check_cooldown:
            return

        self._last_wake_check_time = now
        self._standby_speech_frames = 0

        pcm_data = bytes(self._standby_window)
        self._standby_window.clear()

        keyword = self._wake_detector.detect(pcm_data)
        if keyword:
            _log(f"✅ 唤醒词检测到: {keyword}")
            self.set_mode("dialog")
            self._last_speech_time = time.time()
            self._stt_fail_count = 0
            if self.on_wake:
                try:
                    self.on_wake()
                except Exception:
                    pass
        else:
            self._stt_fail_count += 1
            if self._stt_fail_count >= 3:
                cooldown = min(300, 30 * (2 ** (self._stt_fail_count - 3)))
                self._stt_fail_cooldown_until = now + cooldown
                _log(f"唤醒词连续 {self._stt_fail_count} 次未匹配，冷却 {cooldown}秒")
            else:
                _log("未匹配到唤醒词，继续监听")

    def _loop_dialog(self):
        if time.time() - self._last_speech_time > self.DIALOG_TIMEOUT:
            _log("对话超时，回到待机模式")
            self.set_mode("standby")
            return

        chunk = self._capture.read(duration=self.CHUNK_DURATION)
        if not chunk or len(chunk) < 320:
            time.sleep(0.1)
            return

        is_speech = self._vad.is_speech(chunk)

        if is_speech:
            self._speech_buffer.extend(chunk)
            self._speech_chunks += 1
            self._silence_chunks = 0
            self._in_speech = True
            self._last_speech_time = time.time()
        else:
            if self._in_speech:
                self._silence_chunks += 1
                self._speech_buffer.extend(chunk)

                silence_sec = self._silence_chunks * self.CHUNK_DURATION
                speech_sec = self._speech_chunks * self.CHUNK_DURATION

                if silence_sec >= self._vad.silence_duration:
                    if speech_sec >= self._vad.min_speech_duration:
                        self._process_segment(bytes(self._speech_buffer))
                    self._reset_segment()
                elif speech_sec >= self.MAX_SEGMENT_DURATION:
                    self._process_segment(bytes(self._speech_buffer))
                    self._reset_segment()

    def _loop_task(self):
        chunk = self._capture.read(duration=self.CHUNK_DURATION)
        if not chunk or len(chunk) < 320:
            time.sleep(0.1)
            return

        is_speech = self._vad.is_speech(chunk)

        if is_speech:
            self._speech_buffer.extend(chunk)
            self._speech_chunks += 1
            self._silence_chunks = 0
            self._in_speech = True
            self._last_speech_time = time.time()
        else:
            if self._in_speech:
                self._silence_chunks += 1
                self._speech_buffer.extend(chunk)

                silence_sec = self._silence_chunks * self.CHUNK_DURATION
                speech_sec = self._speech_chunks * self.CHUNK_DURATION

                if silence_sec >= self._vad.silence_duration:
                    if speech_sec >= self._vad.min_speech_duration:
                        self._process_segment(bytes(self._speech_buffer))
                    self._reset_segment()
                elif speech_sec >= self.MAX_SEGMENT_DURATION:
                    self._process_segment(bytes(self._speech_buffer))
                    self._reset_segment()

    def _reset_segment(self):
        self._speech_buffer.clear()
        self._silence_chunks = 0
        self._speech_chunks = 0
        self._in_speech = False

    def _process_segment(self, pcm_data: bytes):
        if len(pcm_data) < self.sample_rate * 2 * 0.3:
            return

        wav_bytes = self._pcm_to_wav(pcm_data)

        stt = self._get_stt()
        if not stt:
            return

        try:
            result = stt.recognize_bytes(wav_bytes, "wav")
            text = result.get("text", "").strip()
            if text and len(text) > 1:
                _log(f"识别到语音 [{self._mode.value}]: {text[:80]}")
                if self.on_speech:
                    try:
                        self.on_speech(text)
                    except Exception as e:
                        _log(f"回调异常: {e}")
        except Exception as e:
            _log(f"STT 识别异常: {e}")

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000,
                    channels: int = 1, sample_width: int = 2,
                    normalize: bool = True) -> bytes:
        if normalize and len(pcm_data) >= 2:
            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            peak = np.max(np.abs(samples))
            if 0 < peak < 8000:
                gain = 8000.0 / peak
                samples = np.clip(samples * gain, -32767, 32767).astype(np.int16)
                pcm_data = samples.tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return buf.getvalue()

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "mode": self._mode.value,
            "audio_source": self.audio_source,
            "buffer_duration": round(self._capture.buffer_duration(), 1),
            "connected": self._capture.connected,
            "in_speech": self._in_speech,
            "stt_available": self._stt_engine is not None,
            "wake_words": self._wake_detector.keywords,
            "dialog_idle_seconds": round(
                time.time() - self._last_speech_time, 1
            ) if self._mode == PipelineMode.DIALOG else 0,
        }
