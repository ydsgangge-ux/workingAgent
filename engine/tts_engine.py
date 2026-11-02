"""
语音合成引擎（TTS）
优先级：edge-tts（微软在线，高质量）> pyttsx3（离线兜底）> 静默降级

安装：pip install edge-tts
推荐中文声音：
  zh-CN-XiaoxiaoNeural   小晓（女，温柔自然，默认）
  zh-CN-YunxiNeural      云希（男，活泼）
  zh-CN-YunjianNeural    云健（男，成熟稳重）
  zh-TW-HsiaoChenNeural  台湾中文（女）
"""

import asyncio
import base64
import json
import os
import re
import sys
import threading
import tempfile
import urllib.error
import urllib.request
from typing import Optional, Callable


# 可选声音列表（供 UI 展示）
VOICE_OPTIONS = [
    ("zh-CN-XiaoxiaoNeural",  "小晓·女·温柔（推荐）"),
    ("zh-CN-XiaoyiNeural",    "小艺·女·活泼"),
    ("zh-CN-YunxiNeural",     "云希·男·活泼"),
    ("zh-CN-YunjianNeural",   "云健·男·稳重"),
    ("zh-CN-YunyangNeural",   "云扬·男·新闻播报"),
    ("zh-TW-HsiaoChenNeural", "晓臻·台湾·女"),
    ("zh-HK-HiuMaanNeural",   "晓曼·粤语·女"),
]


# MiMo TTS 预置音色（mimo-v2.5-tts）
MIMO_VOICES = [
    ("冰糖", "冰糖·女·中文（默认）"),
    ("茉莉", "茉莉·女·中文"),
    ("苏打", "苏打·男·中文"),
    ("白桦", "白桦·男·中文"),
    ("Mia",    "Mia·女·英文"),
    ("Chloe",  "Chloe·女·英文"),
    ("Milo",   "Milo·男·英文"),
    ("Dean",   "Dean·男·英文"),
]

MIMO_MODELS = [
    ("mimo-v2.5-tts",            "预置音色（支持唱歌）"),
    ("mimo-v2.5-tts-voicedesign", "文本设计音色"),
    ("mimo-v2.5-tts-voiceclone",  "音色复刻（克隆声音）"),
]

MIMO_BASE = "https://api.xiaomimimo.com/v1"


def _load_tts_cfg() -> dict:
    try:
        from desktop.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _inherit_mimo_key(cfg: dict) -> str:
    """TTS 专用 Key 为空时，按优先级继承主 LLM / 视觉 Key"""
    for k in ("vision_api_key", "api_key", "image_api_key"):
        v = str(cfg.get(k) or "").strip()
        if v:
            return v
    return os.environ.get("MIMO_API_KEY", "")


def _clean_tts_text(text: str, keep_tags: bool = False) -> str:
    """去掉 Markdown 噪音。keep_tags=True 时保留 () []，供 MiMo 风格/音频标签使用"""
    src = text or ""
    if keep_tags:
        clean = re.sub(r'[*#`_<>|]', '', src)
    else:
        clean = re.sub(r'[*#`_\[\]()<>|]', '', src)
    return re.sub(r'\n+', '。', clean).strip()


class TTSEngine:
    """
    语音合成引擎
    edge-tts 生成 mp3 → winsound/系统播放器 播放
    edge-tts 失败时自动降级到 pyttsx3（离线）
    """

    def __init__(self):
        self._backend    = None   # 'edge' | 'pyttsx3' | None
        self._pyttsx3_engine = None
        self._lock       = threading.Lock()
        self._play_thread: Optional[threading.Thread] = None
        self._stop_flag  = False

        # 配置
        self.voice       = "zh-CN-XiaoxiaoNeural"
        self.rate        = "+0%"
        self.volume      = "+0%"
        self.enabled     = True

    def _detect_backend(self):
        """检测可用后端（只执行一次）"""
        if self._backend is not None:
            return self._backend
        try:
            import edge_tts
            self._backend = "edge"
            return "edge"
        except ImportError:
            pass
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            for v in voices:
                if "zh" in v.id.lower() or "chinese" in v.name.lower():
                    engine.setProperty("voice", v.id)
                    break
            self._pyttsx3_engine = engine
            self._backend = "pyttsx3"
            return "pyttsx3"
        except Exception:
            pass
        self._backend = "none"
        return "none"

    def is_available(self) -> bool:
        return self._detect_backend() in ("edge", "pyttsx3")

    def get_backend_name(self) -> str:
        b = self._detect_backend()
        return {
            "edge":    "Microsoft Edge TTS（高质量）",
            "pyttsx3": "系统 TTS（离线兜底）",
            "none":    "未安装（pip install edge-tts）"
        }.get(b, "未知")

    def stop(self):
        """停止当前播放"""
        self._stop_flag = True
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if self._pyttsx3_engine is not None:
            try:
                self._pyttsx3_engine.stop()
            except Exception:
                pass

    def speak(self, text: str, on_done: Optional[Callable] = None,
              on_error: Optional[Callable] = None):
        """
        异步朗读文本（不阻塞 UI）
        """
        if not self.enabled or not text.strip():
            return

        # 停止上一条
        self._stop_flag = True
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=1.5)

        self._stop_flag = False

        def _run():
            cfg = _load_tts_cfg()
            if str(cfg.get("tts_provider", "edge") or "edge").lower() == "mimo":
                print(f"[TTS] 后端=MiMo TTS, 文本={text[:30]}...")
                if self._speak_mimo(text):
                    if on_done and not self._stop_flag:
                        on_done()
                    return
                print("[TTS] MiMo TTS 失败，降级到 Edge")

            backend = self._detect_backend()
            print(f"[TTS] 后端={backend}, 文本={text[:30]}...")
            try:
                if backend == "edge":
                    success = self._speak_edge(text)
                    if not success:
                        print("[TTS] edge-tts 失败，降级到 pyttsx3")
                        if self._pyttsx3_engine is None:
                            self._try_init_pyttsx3()
                        if self._pyttsx3_engine:
                            self._speak_pyttsx3(text)
                        else:
                            print("[TTS] 无可用后端")
                elif backend == "pyttsx3":
                    self._speak_pyttsx3(text)
                else:
                    print("[TTS] 无可用后端，请安装: pip install edge-tts")
                if on_done and not self._stop_flag:
                    on_done()
            except Exception as e:
                print(f"[TTS] 播放失败: {e}")
                if on_error:
                    on_error(str(e))

        self._play_thread = threading.Thread(target=_run, daemon=True)
        self._play_thread.start()

    def _try_init_pyttsx3(self):
        """尝试初始化 pyttsx3 作为降级方案"""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            for v in voices:
                if "zh" in v.id.lower() or "chinese" in v.name.lower():
                    engine.setProperty("voice", v.id)
                    break
            self._pyttsx3_engine = engine
            self._backend = "pyttsx3"
        except Exception as e:
            print(f"[TTS] pyttsx3 初始化失败: {e}")

    # ── 仅合成、不播放（供 QQ 等渠道取用）──────────────
    def synthesize(self, text: str, style: Optional[str] = None,
                   out_path: Optional[str] = None,
                   sing: bool = False) -> Optional[str]:
        """把文本合成成音频文件并返回路径（不播放）。失败返回 None。

        provider=edge → mp3；provider=mimo → wav。
        style 为自然语言风格指令（MiMo 专用），如"慵懒，语速慢一点"。
        sing=True 时给 MiMo 文本开头补 (唱歌) 标签，否则不会唱。
        支持 MiMo 的括号标签：(唱歌)歌词、(东北话)……
        """
        cfg = _load_tts_cfg()
        provider = str(cfg.get("tts_provider", "edge") or "edge").lower()
        keep_tags = provider == "mimo"
        clean = _clean_tts_text(text, keep_tags=keep_tags)
        if not clean:
            return None
        if provider == "mimo":
            clean = self._apply_mimo_tags(clean, cfg, sing)

        if not out_path:
            suffix = ".wav" if provider == "mimo" else ".mp3"
            out_path = tempfile.mktemp(prefix="agi_tts_", suffix=suffix)

        if provider == "mimo":
            ok = self._synth_mimo(clean, style, out_path, cfg)
        else:
            ok = self._synth_edge(clean, out_path, cfg)
        return out_path if ok else None

    @staticmethod
    def _apply_mimo_tags(text: str, cfg: dict, sing: bool) -> str:
        """唱歌必须靠 assistant 文本开头的 (唱歌) 标签触发"""
        stripped = text.lstrip()
        has_tag = stripped[:1] in ("(", "（", "[")
        if (sing or bool(cfg.get("tts_mimo_sing"))) and not has_tag:
            return "(唱歌)" + stripped
        return text

    def _synth_edge(self, text: str, path: str, cfg: dict) -> bool:
        """edge-tts 只合成到文件"""
        try:
            import edge_tts
        except ImportError:
            print("[TTS] 未安装 edge-tts，可运行: pip install edge-tts")
            return False
        voice = cfg.get("tts_voice") or self.voice
        try:
            rate = f"{int(cfg.get('tts_rate', 0) or 0):+d}%"
        except Exception:
            rate = "+0%"

        async def _go():
            await edge_tts.Communicate(text, voice, rate=rate).save(path)

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_go())
            finally:
                loop.close()
        except Exception as e:
            print(f"[TTS] edge 合成失败: {e}")
            return False
        return os.path.exists(path) and os.path.getsize(path) > 100

    def _synth_mimo(self, text: str, style: Optional[str],
                    path: str, cfg: dict) -> bool:
        """MiMo TTS：OpenAI 兼容接口，返回 base64 音频"""
        model = str(cfg.get("tts_mimo_model") or "mimo-v2.5-tts")
        base = str(cfg.get("tts_mimo_base_url") or "").strip() or MIMO_BASE
        key = str(cfg.get("tts_mimo_api_key") or "").strip() or _inherit_mimo_key(cfg)
        if not key:
            print("[TTS] MiMo 缺少 API Key")
            return False

        user_msg = style or str(cfg.get("tts_mimo_style") or "")
        if (not model.endswith("voicedesign")
                and text.lstrip().startswith(("(唱歌)", "（唱歌）", "[唱歌]"))):
            user_msg = user_msg or "请把下面的内容当作歌词演唱，旋律自然连贯，咬字清晰有感情。"
        audio = {"format": "wav"}

        if model.endswith("voicedesign"):
            # user 消息即音色描述（必填）
            user_msg = user_msg or "年轻女性，温柔清亮，语速偏慢"
            audio["optimize_text_preview"] = False
        elif model.endswith("voiceclone"):
            sample = str(cfg.get("tts_mimo_sample") or "").strip()
            if not sample or not os.path.exists(sample):
                print("[TTS] MiMo 音色复刻缺少样本文件（tts_mimo_sample）")
                return False
            mime = "audio/wav" if sample.lower().endswith(".wav") else "audio/mpeg"
            try:
                with open(sample, "rb") as f:
                    raw = f.read()
            except Exception as e:
                print(f"[TTS] 读取克隆样本失败: {e}")
                return False
            if len(raw) > 10 * 1024 * 1024:
                print("[TTS] 克隆样本超过 10MB")
                return False
            audio["voice"] = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        else:
            audio["voice"] = str(cfg.get("tts_mimo_voice") or "冰糖")

        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": text},
            ],
            "audio": audio,
        }
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            print(f"[TTS] MiMo HTTP {e.code}: {body}")
            return False
        except Exception as e:
            print(f"[TTS] MiMo 请求失败: {e}")
            return False

        node = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        b64 = (node.get("audio") or {}).get("data") or ""
        if not b64:
            print(f"[TTS] MiMo 未返回音频: {str(data)[:200]}")
            return False
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception as e:
            print(f"[TTS] 写入音频失败: {e}")
            return False
        return os.path.getsize(path) > 100

    def _speak_mimo(self, text: str) -> bool:
        """MiMo TTS 合成（wav）并播放"""
        path = self.synthesize(text)
        if not path:
            return False
        if self._stop_flag:
            try:
                os.unlink(path)
            except Exception:
                pass
            return False
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME)
                return True
            except Exception as e:
                print(f"[TTS] MiMo wav 播放失败: {e}")
                return False
            finally:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        cmd = ["afplay"] if sys.platform == "darwin" else ["ffplay", "-autoexit", "-nodisp"]
        return self._play_system(path, cmd)

    def _speak_edge(self, text: str) -> bool:
        """Edge TTS 合成 + 播放"""
        clean = re.sub(r'[*#`_\[\]()]', '', text)
        clean = re.sub(r'\n+', '。', clean).strip()
        if not clean:
            return False

        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".mp3", prefix="agi_tts_"
        )
        tmp.close()
        tmp_path = tmp.name

        try:
            import edge_tts

            async def _synthesize():
                communicate = edge_tts.Communicate(
                    text=clean,
                    voice=self.voice,
                    rate=self.rate,
                    volume=self.volume
                )
                await communicate.save(tmp_path)

            # 独立事件循环，避免与 Qt 事件循环冲突
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_synthesize())
            finally:
                loop.close()

            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 100:
                print("[TTS] edge-tts 生成的文件为空或过小")
                return False

            if self._stop_flag:
                os.unlink(tmp_path)
                return False

            # 播放
            return self._play_mp3(tmp_path)

        except Exception as e:
            print(f"[TTS] edge-tts 失败: {e}")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return False

    def _play_mp3(self, file_path: str) -> bool:
        """播放 mp3 文件"""
        if sys.platform == "win32":
            return self._play_winsound(file_path)
        elif sys.platform == "darwin":
            return self._play_system(file_path, ["afplay"])
        else:
            # Linux: 依次尝试
            for player in ["mpg123", "ffplay", "mplayer"]:
                if self._play_system(file_path, [player, "-q"]):
                    return True
            return False

    def _play_winsound(self, file_path: str) -> bool:
        """Windows MCI 播放 MP3（支持任意线程，无需消息循环）"""
        try:
            import ctypes
            mci = ctypes.windll.winmm
            alias = "agi_tts"
            # 关闭之前的
            mci.mciSendStringW(f"close {alias}", None, 0, None)
            # 打开并播放
            short_path = os.path.abspath(file_path)
            ret = mci.mciSendStringW(
                f'open "{short_path}" type mpegvideo alias {alias}',
                None, 0, None
            )
            if ret != 0:
                # 取错误信息
                buf = ctypes.create_unicode_buffer(256)
                mci.mciGetErrorStringW(ret, buf, 256)
                print(f"[TTS] MCI open 失败: {buf.value}")
                return False
            mci.mciSendStringW(f"play {alias} wait", None, 0, None)
            return True
        except Exception as e:
            print(f"[TTS] MCI 播放失败: {e}")
            return False
        finally:
            try:
                import ctypes
                ctypes.windll.winmm.mciSendStringW(f"close agi_tts", None, 0, None)
            except Exception:
                pass
            try:
                os.unlink(file_path)
            except Exception:
                pass

    def _play_system(self, file_path: str, cmd: list) -> bool:
        """用外部播放器播放"""
        try:
            import subprocess
            subprocess.run(cmd + [file_path], check=True, capture_output=True)
            return True
        except Exception as e:
            print(f"[TTS] 播放器失败: {e}")
            return False
        finally:
            try:
                os.unlink(file_path)
            except Exception:
                pass

    def _speak_pyttsx3(self, text: str):
        """pyttsx3 离线朗读"""
        clean = re.sub(r'[*#`_\[\]()]', '', text)
        clean = re.sub(r'\n+', '，', clean).strip()
        with self._lock:
            if self._pyttsx3_engine and not self._stop_flag:
                self._pyttsx3_engine.say(clean)
                self._pyttsx3_engine.runAndWait()

    @staticmethod
    def _estimate_mp3_duration(path: str) -> float:
        """估算 mp3 时长（秒）"""
        try:
            size = os.path.getsize(path)
            return max(1.0, size / 16000)
        except Exception:
            return 5.0

    def set_voice(self, voice_id: str):
        self.voice = voice_id

    def set_rate(self, percent: int):
        sign = "+" if percent >= 0 else ""
        self.rate = f"{sign}{percent}%"

    @staticmethod
    def install_guide() -> str:
        return (
            "安装 Edge TTS（推荐，完全免费）：\n"
            "  pip install edge-tts\n\n"
            "离线备选方案（无需联网）：\n"
            "  pip install pyttsx3\n\n"
            "安装完成后重启应用即可使用语音朗读。"
        )


# 全局单例
_tts_instance: Optional[TTSEngine] = None

def get_tts() -> TTSEngine:
    global _tts_instance
    if _tts_instance is None:
        _tts_instance = TTSEngine()
    return _tts_instance
