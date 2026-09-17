"""
语音识别引擎（STT - Speech to Text）
优先级：讯飞云端 API > DeepSeek Whisper > 本地 Whisper > 静默降级

安装：
  讯飞：pip install websocket-client
  DeepSeek：无需额外安装（复用主 API Key）
  本地 Whisper：pip install openai-whisper

配置（在 config.json 中设置）：
  stt_provider: "xunfei" | "deepseek" | "whisper_local"
  xunfei_app_id / xunfei_api_key / xunfei_api_secret：讯飞开放平台凭证
"""

import os
import sys
import json
import time
import hashlib
import base64
import hmac
import tempfile
import threading
from typing import Optional, Callable, Dict
from pathlib import Path
from datetime import datetime


class STTEngine:
    """
    语音识别引擎
    支持多种后端：讯飞在线、DeepSeek API、本地 Whisper
    以工具插件形式运行，不修改 agent.py 主逻辑
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._backend = None  # 'xunfei' | 'deepseek' | 'whisper_local' | None
        self._lock = threading.Lock()

        # 配置项
        self.provider = self._config.get("stt_provider", "deepseek")
        self.language = self._config.get("stt_language", "zh")

        # 讯飞凭证
        self.xunfei_app_id = self._config.get("xunfei_app_id", "")
        self.xunfei_api_key = self._config.get("xunfei_api_key", "")
        self.xunfei_api_secret = self._config.get("xunfei_api_secret", "")

        # DeepSeek 配置（复用主 LLM 配置）
        self.api_key = self._config.get("api_key", "")
        self.api_base = self._config.get("stt_api_base", "https://api.deepseek.com")

    def _detect_backend(self) -> Optional[str]:
        """检测可用后端"""
        if self._backend is not None:
            return self._backend

        if self.provider == "xunfei":
            if self._check_xunfei():
                self._backend = "xunfei"
                return "xunfei"
        elif self.provider == "deepseek":
            if self._check_deepseek():
                self._backend = "deepseek"
                return "deepseek"
        elif self.provider == "faster_whisper":
            if self._check_faster_whisper():
                self._backend = "faster_whisper"
                return "faster_whisper"
        elif self.provider == "whisper_local":
            if self._check_whisper_local():
                self._backend = "whisper_local"
                return "whisper_local"

        if self._check_faster_whisper():
            self._backend = "faster_whisper"
            return "faster_whisper"
        if self._check_xunfei():
            self._backend = "xunfei"
            return "xunfei"
        if self._check_deepseek():
            self._backend = "deepseek"
            return "deepseek"

        self._backend = "none"
        return None

    def _check_xunfei(self) -> bool:
        return bool(self.xunfei_app_id and self.xunfei_api_key and self.xunfei_api_secret)

    def _check_deepseek(self) -> bool:
        return bool(self.api_key)

    def _check_whisper_local(self) -> bool:
        try:
            import whisper
            return True
        except ImportError:
            return False

    def _check_faster_whisper(self) -> bool:
        try:
            import faster_whisper
            return True
        except ImportError:
            return False

    def is_available(self) -> bool:
        return self._detect_backend() is not None

    def get_backend_name(self) -> str:
        b = self._detect_backend()
        return {
            "xunfei": "讯飞语音识别（在线）",
            "deepseek": "DeepSeek Whisper（在线）",
            "whisper_local": "本地 Whisper（离线）",
            "faster_whisper": "Faster Whisper（离线）",
            "none": "未配置"
        }.get(b or "none", "未知")

    def recognize_file(self, audio_path: str) -> Dict:
        """
        识别音频文件，返回 {"ok": bool, "text": str, "backend": str, ...}
        """
        if not os.path.isfile(audio_path):
            return {"ok": False, "error": f"音频文件不存在: {audio_path}"}

        backend = self._detect_backend()
        if not backend:
            return {"ok": False, "error": "无可用 STT 后端，请在设置中配置语音识别"}

        print(f"[STT] 后端={backend}, 文件={audio_path}")

        try:
            if backend == "xunfei":
                result = self._recognize_xunfei(audio_path)
            elif backend == "deepseek":
                result = self._recognize_deepseek(audio_path)
            elif backend == "whisper_local":
                result = self._recognize_whisper_local(audio_path)
            elif backend == "faster_whisper":
                result = self._recognize_faster_whisper(audio_path)
            else:
                result = {"ok": False, "error": "无可用后端"}

            if result.get("ok"):
                result["backend"] = backend
                print(f"[STT] 识别成功: {result['text'][:60]}...")
            else:
                print(f"[STT] 识别失败: {result.get('error', '未知错误')}")
            return result

        except Exception as e:
            return {"ok": False, "error": f"STT 异常: {e}"}

    def recognize_bytes(self, audio_bytes: bytes, format: str = "wav") -> Dict:
        """识别内存中的音频字节"""
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=f".{format}", prefix="agi_stt_"
        )
        try:
            tmp.write(audio_bytes)
            tmp.close()
            return self.recognize_file(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    # ── 讯飞实时语音转写 API ────────────────────────────
    def _recognize_xunfei(self, audio_path: str) -> Dict:
        """
        讯飞开放式语音转写 API
        文档：https://www.xfyun.cn/doc/asr/voicedictation/API.html
        """
        try:
            import websocket
            import ssl
        except ImportError:
            return {"ok": False, "error": "请安装 websocket-client: pip install websocket-client"}

        # 讯飞 WebSocket 鉴权
        url = self._build_xunfei_url()

        # 读取音频文件
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        # 讯飞要求 PCM 16bit 16kHz mono
        # 如果是 wav/mp3 等格式，尝试转换
        if not audio_path.lower().endswith(".pcm"):
            pcm_data = self._audio_to_pcm_16k(audio_data, audio_path)
            if pcm_data is None:
                return {
                    "ok": False,
                    "error": "音频格式转换失败，讯飞需要 PCM 16bit 16kHz 格式"
                }
            audio_data = pcm_data

        result_text = []
        error_msg = None
        finished = threading.Event()

        def on_message(ws, message):
            nonlocal error_msg
            try:
                data = json.loads(message)
                code = data.get("code", 0)
                if code != 0:
                    error_msg = f"讯飞错误(code={code}): {data.get('message', '')}"
                    ws.close()
                    return

                result = data.get("data", {}).get("result", {})
                ws_text = result.get("ws", [])
                for ws_item in ws_text:
                    for cw in ws_item.get("cw", []):
                        result_text.append(cw.get("w", ""))

                status = data.get("data", {}).get("status", 0)
                if status == 2:  # 最后一帧
                    ws.close()

            except json.JSONDecodeError:
                pass

        def on_error(ws, error):
            nonlocal error_msg
            error_msg = str(error)
            finished.set()

        def on_close(ws, close_status_code, close_msg):
            finished.set()

        def on_open(ws):
            # 发送音频数据（分片发送，每片 4096 字节）
            frame_size = 4096
            total_len = len(audio_data)
            offset = 0
            while offset < total_len:
                end = min(offset + frame_size, total_len)
                chunk = audio_data[offset:end]
                status = 2 if end >= total_len else 0  # 最后一帧 status=2
                ws.send(json.dumps({
                    "data": {
                        "status": status,
                        "format": "audio/L16;rate=16000",
                        "encoding": "raw",
                        "audio": base64.b64encode(chunk).decode()
                    }
                }))
                offset = end
                if status == 2:
                    break

        try:
            ws = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_open=on_open,
                on_close=on_close
            )
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            finished.wait(timeout=15)

        except Exception as e:
            return {"ok": False, "error": f"讯飞 WebSocket 连接失败: {e}"}

        if error_msg:
            return {"ok": False, "error": error_msg}

        text = "".join(result_text).strip()
        if not text:
            return {"ok": False, "error": "讯飞未识别到有效内容"}

        return {"ok": True, "text": text}

    def _build_xunfei_url(self) -> str:
        """构建讯飞 WebSocket 鉴权 URL"""
        from urllib.parse import urlencode, quote

        base_url = "wss://iat-api.xfyun.cn/v2/iat"
        api_key = self.xunfei_api_key
        api_secret = self.xunfei_api_secret

        timestamp = str(int(time.time()))
        signature_origin = f"host: iat-api.xfyun.cn\ndate: {timestamp}\nGET /v2/iat HTTP/1.1"

        signature_sha = hmac.new(
            api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        signature = base64.b64encode(signature_sha).decode()

        authorization_origin = (
            f'api_key="{api_key}", '
            f'algorithm="hmac-sha256", '
            f'headers="host date request-line", '
            f'signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode()).decode()

        params = urlencode({
            "authorization": authorization,
            "date": timestamp,
            "host": "iat-api.xfyun.cn"
        })
        return f"{base_url}?{params}"

    def _audio_to_pcm_16k(self, audio_data: bytes, source_path: str) -> Optional[bytes]:
        """将音频转换为 PCM 16bit 16kHz mono"""
        try:
            import subprocess
            tmp_in = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
            tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".pcm")
            tmp_in.write(audio_data)
            tmp_in.close()
            tmp_out.close()

            # 使用 ffmpeg 转换（最通用）
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp_in.name,
                     "-f", "s16le", "-acodec", "pcm_s16le",
                     "-ar", "16000", "-ac", "1", tmp_out.name],
                    capture_output=True, timeout=10
                )
                with open(tmp_out.name, "rb") as f:
                    pcm = f.read()
                return pcm if len(pcm) > 0 else None
            except FileNotFoundError:
                # ffmpeg 不可用，尝试 pydub
                pass
            finally:
                try:
                    os.unlink(tmp_in.name)
                    os.unlink(tmp_out.name)
                except Exception:
                    pass

            # pydub 回退
            try:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(source_path)
                audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                return audio.raw_data
            except ImportError:
                return None

        except Exception:
            return None

    # ── DeepSeek Whisper API ─────────────────────────────
    def _recognize_deepseek(self, audio_path: str) -> Dict:
        """
        使用 DeepSeek 的 Whisper API（兼容 OpenAI audio/transcriptions 格式）
        """
        try:
            import urllib.request

            # 检查是否支持 DeepSeek Whisper
            # DeepSeek 当前不支持 Whisper，回退到 OpenAI 兼容格式
            # 使用配置的 api_base，如果用户配了 OpenAI 的 key 和 base 也能用
            api_key = self.api_key
            base_url = self.api_base.rstrip("/")

            # 先尝试 DeepSeek 格式
            url = f"{base_url}/audio/transcriptions"

            # 构建 multipart form data
            boundary = "----AGISTTBoundary" + str(int(time.time() * 1000))

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

            filename = os.path.basename(audio_path)

            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + audio_bytes + f"\r\n--{boundary}\r\n".encode() + (
                f'Content-Disposition: form-data; name="model"\r\n\r\n'
                f"whisper-1\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="language"\r\n\r\n'
                f"{self.language}\r\n"
                f"--{boundary}--\r\n"
            ).encode()

            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}"
                }
            )

            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    text = data.get("text", "").strip()
                    if text:
                        return {"ok": True, "text": text}
                    return {"ok": False, "error": "API 返回空文本"}
            except Exception as e:
                err_str = str(e)
                if "404" in err_str or "not found" in err_str.lower():
                    return {
                        "ok": False,
                        "error": "当前 API 不支持音频识别（DeepSeek 暂不支持 Whisper）。"
                               "请在 config.json 中设置 stt_provider 为 'xunfei' 或 'whisper_local'"
                    }
                return {"ok": False, "error": f"API 请求失败: {e}"}

        except Exception as e:
            return {"ok": False, "error": f"DeepSeek STT 异常: {e}"}

    # ── 本地 Whisper ────────────────────────────────────
    def _recognize_whisper_local(self, audio_path: str) -> Dict:
        """使用本地 Whisper 模型进行语音识别"""
        try:
            import whisper

            # 使用 base 模型平衡速度和精度
            model_name = self._config.get("whisper_model", "base")
            model = whisper.load_model(model_name)

            # 识别
            result = model.transcribe(
                audio_path,
                language=self.language if self.language != "zh" else "zh",
                initial_prompt="以下是普通话的句子"
            )

            text = result.get("text", "").strip()
            if text:
                return {"ok": True, "text": text}
            return {"ok": False, "error": "Whisper 未识别到有效内容"}

        except Exception as e:
            return {"ok": False, "error": f"本地 Whisper 异常: {e}"}

    def _recognize_faster_whisper(self, audio_path: str) -> Dict:
        """使用 faster-whisper（CTranslate2 后端）进行语音识别"""
        try:
            from faster_whisper import WhisperModel

            model_size = self._config.get("whisper_model", "base")
            device = "cuda" if self._config.get("whisper_device") == "cuda" else "cpu"
            compute_type = "int8" if device == "cpu" else "float16"

            cache_key = f"{model_size}_{device}_{compute_type}"
            if not hasattr(self, '_fw_model_cache'):
                self._fw_model_cache = {}
            if cache_key not in self._fw_model_cache:
                model_path = self._find_faster_whisper_model(model_size)
                self._fw_model_cache[cache_key] = WhisperModel(
                    model_path, device=device, compute_type=compute_type
                )
            model = self._fw_model_cache[cache_key]

            segments, info = model.transcribe(
                audio_path,
                language=self.language if self.language != "zh" else "zh",
                initial_prompt="",
                vad_filter=False,
            )

            text_parts = []
            for segment in segments:
                text_parts.append(segment.text.strip())

            text = "".join(text_parts).strip()
            if text:
                return {"ok": True, "text": text}
            return {"ok": False, "error": "Faster Whisper 未识别到有效内容"}

        except Exception as e:
            return {"ok": False, "error": f"Faster Whisper 异常: {e}"}

    @staticmethod
    def _find_faster_whisper_model(model_size: str) -> str:
        """查找本地 faster-whisper 模型路径，优先使用缓存"""
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        model_dir = cache_dir / f"models--Systran--faster-whisper-{model_size}"
        if model_dir.exists():
            snapshots_dir = model_dir / "snapshots"
            if snapshots_dir.exists():
                for snap in snapshots_dir.iterdir():
                    if snap.is_dir() and (snap / "model.bin").exists():
                        return str(snap)
        return model_size

    @staticmethod
    def install_guide() -> str:
        return (
            "语音识别安装指南：\n\n"
            "方案1 - Faster Whisper（推荐，离线+快速）：\n"
            "  pip install faster-whisper\n"
            "  在 config.json 中设置 stt_provider: \"faster_whisper\"\n"
            "  首次运行会自动下载模型（~140MB）\n\n"
            "方案2 - 讯飞在线（中文效果最好）：\n"
            "  pip install websocket-client\n"
            "  在 config.json 中设置 xunfei_app_id / xunfei_api_key / xunfei_api_secret\n"
            "  申请地址：https://www.xfyun.cn/\n\n"
            "方案3 - DeepSeek Whisper：\n"
            "  需要兼容 OpenAI audio API 的服务\n"
            "  在 config.json 中设置 stt_provider: \"deepseek\"\n\n"
            "方案4 - 本地 Whisper（离线）：\n"
            "  pip install openai-whisper\n"
            "  在 config.json 中设置 stt_provider: \"whisper_local\"\n"
        )


# ── 录音工具函数 ────────────────────────────────────────

def record_audio(duration: float = 5.0, sample_rate: int = 16000) -> Optional[str]:
    """
    录制指定时长的音频，返回临时文件路径
    使用 pyaudio 或 sounddevice
    """
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".wav", prefix="agi_rec_"
    )
    tmp_path = tmp.name
    tmp.close()

    try:
        # 优先尝试 sounddevice（更简单）
        try:
            import sounddevice as sd
            import numpy as np
            import wave

            print(f"[录音] 开始录制 {duration} 秒...")
            audio = sd.rec(
                int(duration * sample_rate),
                samplerate=sample_rate,
                channels=1,
                dtype="int16"
            )
            sd.wait()  # 等待录制完成
            print("[录音] 录制完成")

            # 保存为 WAV
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(sample_rate)
                wf.writeframes(audio.tobytes())

            return tmp_path

        except ImportError:
            pass

        # 回退到 pyaudio
        try:
            import pyaudio
            import wave

            p = pyaudio.PyAudio()
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=sample_rate,
                input=True,
                frames_per_buffer=1024
            )

            print(f"[录音] 开始录制 {duration} 秒...")
            frames = []
            for _ in range(int(sample_rate / 1024 * duration)):
                data = stream.read(1024, exception_on_overflow=False)
                frames.append(data)

            stream.stop_stream()
            stream.close()
            p.terminate()
            print("[录音] 录制完成")

            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(b"".join(frames))

            return tmp_path

        except ImportError:
            pass

        # 最终回退：使用 Windows 内置录音
        if sys.platform == "win32":
            try:
                import subprocess
                # 使用 PowerShell 录音（Windows 10+）
                ps_script = f"""
                Add-Type -AssemblyName System.Runtime.WindowsRuntime
                $mediaCapture = New-Object Windows.Media.Capture.MediaCapture
                await $mediaCapture.InitializeAsync()
                $profile = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateWav([Windows.Media.MediaProperties.AudioEncodingQuality]::Auto)
                $file = await $mediaCapture.StartRecordToStorageFileAsync($profile, (New-Object Windows.Storage.StorageFile -ArgumentList "{tmp_path.replace(os.sep, '/')}", [Windows.Storage.CreationCollisionOption]::ReplaceExisting))
                Start-Sleep -Seconds {duration}
                await $mediaCapture.StopRecordAsync()
                """
                # PowerShell 方式比较复杂，这里简化为提示
                return None
            except Exception:
                pass

        return None

    except Exception as e:
        print(f"[录音] 录制失败: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return None


# 全局单例
_stt_instance: Optional[STTEngine] = None


def get_stt(config: dict = None) -> STTEngine:
    global _stt_instance
    if _stt_instance is None:
        _stt_instance = STTEngine(config)
    return _stt_instance
