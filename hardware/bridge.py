"""
桥接层 v3 — 状态机驱动 + 视觉/音频/手机联动
==========================================
三种模式：
  待机 (standby)  → 视觉低频抓帧，音频只做唤醒词检测
  对话 (dialog)   → 视觉高频抓帧，音频完整 STT
  任务 (task)     → 视觉持续分析，音频监听任务指令

手机终端支持：
  - 手机作为音频源（phone_url → IP Webcam 麦克风）
  - 手机作为摄像头源（phone_url → /shot.jpg）
  - 手机传感器数据推送（GPS、电量、加速度等）
  - TTS 音频推送到手机扬声器播放

推送方式：
1. 格式化文字 → agent.process(text)
2. 写入视觉记忆库
3. 通过回调通知 UI
4. 音频管线 → 多源 → VAD → STT → A 层
"""
import json
import threading
import time
from typing import Callable, Optional

from .memory_schema import VisualMemory
from .mock_sensors import SensorPacket
from .visual_memory_store import VisualMemoryStore


class Bridge:
    """
    硬件数据 → 现有系统的转换层（状态机驱动）。

    使用方式：
        bridge = Bridge(on_agent_input=my_agent.process)
        bridge.on_sensor_data(packet)
    """

    CAPTURE_INTERVAL_STANDBY = 300
    CAPTURE_INTERVAL_ACTIVE = 30
    DIALOG_TIMEOUT = 30.0

    def __init__(self, on_agent_input: Optional[Callable[[str], None]] = None):
        self._agent_callback = on_agent_input
        self._store = VisualMemoryStore()
        self._sensor_stream = None
        self._latest_sensor_text = ""
        self._periodic_thread = None
        self._periodic_stop = threading.Event()
        self._audio_pipeline = None
        self._mode = "standby"
        self._last_speech_time = 0.0
        self._capture_interval = self.CAPTURE_INTERVAL_STANDBY

        # 手机终端
        self._phone_sensor = None

        # 加载配置
        self._ha_cfg = {}
        try:
            ha_config_path = str(
                __import__('pathlib').Path(__file__).parent.parent / "ha_config.json"
            )
            with open(ha_config_path, "r", encoding="utf-8") as f:
                self._ha_cfg = json.load(f)
        except Exception:
            pass

        self._start_sensor_stream()
        self._start_phone_sensor()
        self._start_periodic_capture()
        self._start_audio_pipeline()

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str):
        if mode not in ("standby", "dialog", "task"):
            return
        old = self._mode
        self._mode = mode
        if mode == "standby":
            self._capture_interval = self.CAPTURE_INTERVAL_STANDBY
        else:
            self._capture_interval = self.CAPTURE_INTERVAL_ACTIVE
        _log_event(f"[模式] {old} → {mode} (抓帧间隔 {self._capture_interval}s)")
        if self._audio_pipeline:
            try:
                if mode == "standby":
                    self._audio_pipeline.set_mode("standby")
                elif mode == "dialog":
                    self._audio_pipeline.set_mode("dialog")
                elif mode == "task":
                    self._audio_pipeline.start_task()
            except Exception as e:
                _log_event(f"[模式] 音频管线切换失败: {e}")

    def enter_dialog(self):
        self.set_mode("dialog")
        self._last_speech_time = time.time()

    def enter_task(self):
        self.set_mode("task")

    def exit_to_standby(self):
        self.set_mode("standby")

    # ── 接口1：视觉记忆写入后通知 A 层 ──────────────

    def on_visual_memory(self, mem: VisualMemory):
        """
        视觉记忆写入/更新时调用。
        将 event_summary 推入现有系统的事件流，让 A 层感知。
        """
        if mem.vision_confidence < 0.6:
            text = f"[视觉]（低置信度）似乎看到了{mem.description}"
        else:
            text = self._format_memory_text(mem)

        _log_event(text)
        if self._agent_callback:
            self._agent_callback(text)

    @staticmethod
    def _format_memory_text(mem: VisualMemory) -> str:
        parts = [f"[视觉] 看到: {mem.description}"]
        if mem.objects:
            obj_names = [o.get("label", "?") for o in mem.objects[:5]]
            parts.append(f"  物体: {'、'.join(obj_names)}")
        if mem.persons:
            for p in mem.persons[:2]:
                parts.append(f"  人物: {p.get('name','?')} 在{p.get('action','?')}")
        if mem.event_summary:
            parts.append(f"  事件: {mem.event_summary}")
        return "\n".join(parts)

    # ── 接口2：传感器状态格式化推送 ──────────────────

    def on_sensor_data(self, packet: SensorPacket):
        """
        传感器数据到达时调用。
        格式化成自然语言文字，推给 A 层直接读取。
        """
        text = self.format_sensor_text(packet)
        _log_event(text)
        if self._agent_callback:
            self._agent_callback(text)

    @staticmethod
    def format_sensor_text(packet: SensorPacket) -> str:
        parts = [
            f"当前电量{packet.battery:.0f}%",
            f"运动状态：{packet.motion}",
            f"位置({packet.indoor_coords['x']:.1f}, {packet.indoor_coords['y']:.1f})",
        ]
        if packet.collision:
            parts.append("[碰撞警告]")
        return "，".join(parts)

    # ── 接口3：A 层主动查询视觉记忆 ──────────────────

    def query_visual_memory(self, natural_language_query: str) -> str:
        """
        接收 A 层的自然语言查询，检索视觉记忆库，
        返回格式化文字结果供 A 层直接使用。

        使用方式（在 agent 提示词中）：
            bridge.query_visual_memory("扳手在哪里")
            → "扳手最后一次看到是在工位B桌面左侧，14:32记录"
        """
        results = self._store.search(natural_language_query, top_k=3)
        if not results:
            return f"未找到与「{natural_language_query}」相关的视觉记忆"

        lines = []
        for i, r in enumerate(results, 1):
            desc = r.get("description", "")
            ts = r.get("timestamp", "")[:19]  # 截断到秒
            conf = r.get("vision_confidence", 0)
            imp = r.get("importance", 0)
            img = r.get("image_path", "")
            img_info = f"，图片: {img}" if img else ""
            lines.append(
                f"{i}. [{ts}] {desc}（置信度{conf:.2f}，重要性{imp:.2f}{img_info}）"
            )
        return "\n".join(lines)

    def get_recent_vision(self, limit: int = 3) -> str:
        """
        获取最近 N 条视觉记忆，按时间倒序。
        用于 A 层上下文注入，不需要关键词匹配。
        """
        results = self._store.get_recent(limit=limit)
        if not results:
            return ""

        lines = []
        for i, r in enumerate(results, 1):
            desc = r.get("description", "")
            ts = r.get("timestamp", "")[:19]
            conf = r.get("vision_confidence", 0)
            line = f"{i}. [{ts}] {desc}（置信度{conf:.2f}）"

            persons_raw = r.get("persons", "")
            if persons_raw:
                try:
                    import json
                    persons = json.loads(persons_raw) if isinstance(persons_raw, str) else persons_raw
                    if persons:
                        names = [p.get("name", p.get("id", "?")) for p in persons]
                        line += f"\n   识别到的人: {', '.join(names)}"
                except Exception:
                    pass

            lines.append(line)
        return "\n".join(lines)

    # ── 接口4：传感器流管理 ──────────────────────────

    def _start_sensor_stream(self):
        """启动传感器数据流（后台线程持续更新）"""
        try:
            from .mock_sensors import MockSensorStream
            self._sensor_stream = MockSensorStream(interval=30)

            def _on_packet(packet):
                self._latest_sensor_text = self.format_sensor_text(packet)

            self._sensor_stream.on_data(_on_packet)
            self._sensor_stream.start()
        except Exception as e:
            print(f"[Bridge] 传感器流启动失败: {e}")

    # ── 手机终端 ──────────────────────────────────────

    def _start_phone_sensor(self):
        """初始化手机传感器客户端（按需获取，不启动定时推送）"""
        phone_url = self._ha_cfg.get("phone_url", "")
        if not phone_url:
            return

        try:
            from .phone_sensor_client import PhoneSensorClient
            self._phone_sensor = PhoneSensorClient(phone_url)
            _log_event(f"[手机] 手机终端已连接: {phone_url}")
        except Exception as e:
            _log_event(f"[手机] 手机终端启动失败: {e}")

    def get_phone_gps(self) -> dict | None:
        """按需获取手机 GPS 数据"""
        if not self._phone_sensor:
            return None
        try:
            return self._phone_sensor.get_gps()
        except Exception:
            return None

    async def get_phone_sensor_text(self) -> str:
        """
        通过 WebSocket 向手机请求传感器数据，格式化为 A 层可读文字。
        使用 phone_ws_server 的 get_sensor_data() 方法。
        失败或手机未连接返回空字符串。
        """
        try:
            from .phone_ws_server import get_phone_server

            server = get_phone_server()
            if server is None or not server.is_connected():
                return ""

            data = await server.get_sensor_data()
            if not data:
                return ""

            gps = data.get("gps", {})
            lat = gps.get("lat", 0)
            lng = gps.get("lng", 0)
            battery = data.get("battery", 0)
            light = data.get("light", 0)

            text_parts = [f"[手机传感器] 电量{battery:.0f}%"]
            if lat != 0 or lng != 0:
                text_parts.append(f"GPS({lat:.4f}, {lng:.4f})")
            if light > 0:
                text_parts.append(f"光线{light:.0f}lux")
            return " | ".join(text_parts)

        except Exception as e:
            print(f"[Bridge] 获取手机传感器数据失败: {e}")
            return ""

    async def get_current_location_text(self) -> str:
        """
        获取当前位置的语义描述（"在公司附近" / "上海市浦东新区xx路"），
        供 A 层对话使用。通过 WebSocket 从手机获取 GPS 后调用 location_resolver 解析。
        失败或手机未连接返回空字符串。
        """
        try:
            from .phone_ws_server import get_phone_server
            from .location_resolver import resolve_location

            server = get_phone_server()
            if server is None or not server.is_connected():
                return ""

            sensor_data = await server.get_sensor_data()
            gps = sensor_data.get("gps", {})

            if not gps.get("lat"):
                return ""

            location = resolve_location(gps["lat"], gps["lng"])
            return location["location_text"]

        except Exception as e:
            print(f"[Bridge] 获取当前位置失败: {e}")
            return ""

    def get_current_sensor_text(self) -> str:
        """获取最近一次传感器数据格式化文本"""
        if self._latest_sensor_text:
            return self._latest_sensor_text
        if self._sensor_stream:
            try:
                packet = self._sensor_stream._generate()
                self._latest_sensor_text = self.format_sensor_text(packet)
                return self._latest_sensor_text
            except Exception:
                pass
        return "传感器数据暂不可用"

    # ── 接口5：定时抓帧 ──────────────────────────────

    def _start_periodic_capture(self):
        """启动后台定时抓帧线程（待机5分钟/活跃30秒一帧，每6小时清理一次）"""
        _loop_count = 0
        CLEANUP_EVERY = 72

        def _loop():
            nonlocal _loop_count
            while not self._periodic_stop.is_set():
                self._periodic_stop.wait(timeout=self._capture_interval)
                if self._periodic_stop.is_set():
                    break

                if self._mode == "dialog":
                    if time.time() - self._last_speech_time > self.DIALOG_TIMEOUT:
                        _log_event("[对话超时] 回到待机模式")
                        self.set_mode("standby")

                try:
                    from .vision_pipeline import VisionPipeline
                    vp = VisionPipeline(phone_sensor=self._phone_sensor)
                    result = vp.run_once()
                    if result and result.persons:
                        names = [p.get("name", "?") for p in result.persons]
                        _log_event(f"[定时视觉] 检测到人物: {', '.join(names)}")
                    elif result:
                        _log_event(f"[定时视觉] {result.description[:60]}")
                except Exception as e:
                    print(f"[Bridge] 定时抓帧失败: {e}")

                _loop_count += 1
                if _loop_count >= CLEANUP_EVERY:
                    _loop_count = 0
                    try:
                        self._store.decay_importance()
                        self._store.cleanup()
                        stats = self._store.count_by_type()
                        # 图片清理
                        try:
                            from .image_manager import ImageManager
                            img_mgr = ImageManager()
                            img_stats = img_mgr.cleanup(store=self._store)
                            _log_event(f"[记忆维护] 衰减+清理完成: {stats}, 图片清理: {img_stats}")
                        except Exception as img_e:
                            _log_event(f"[记忆维护] 衰减+清理完成: {stats}, 图片清理失败: {img_e}")
                    except Exception as e:
                        print(f"[Bridge] 记忆清理失败: {e}")

        self._periodic_thread = threading.Thread(target=_loop, daemon=True)
        self._periodic_thread.start()
        print(f"[Bridge] 定时抓帧已启动（待机 {self.CAPTURE_INTERVAL_STANDBY}s / 活跃 {self.CAPTURE_INTERVAL_ACTIVE}s）")

    def stop_periodic_capture(self):
        """停止定时抓帧"""
        self._periodic_stop.set()
        if self._periodic_thread:
            self._periodic_thread.join(timeout=5)
        print("[Bridge] 定时抓帧已停止")

    # ── 接口6：记忆清理 ──────────────────────────────

    def cleanup_memories(self):
        """执行一次记忆清理"""
        self._store.cleanup()
        _log_event("[记忆清理] 已执行")

    # ── 接口7：音频管线（RTSP 麦克风 → STT → A 层）───

    def _start_audio_pipeline(self):
        """启动音频采集 + VAD + STT 管线（状态机模式，支持多音频源）"""
        audio_source = self._ha_cfg.get("audio_source", "rtsp")
        rtsp_url = self._ha_cfg.get("rtsp_url", "")
        phone_url = self._ha_cfg.get("phone_url", "")
        wake_words = self._ha_cfg.get("wake_words")

        if audio_source == "rtsp" and not rtsp_url:
            _log_event("[音频] 未配置 RTSP URL，回退到本地麦克风")
            audio_source = "mic"

        if audio_source == "phone" and not phone_url:
            _log_event("[音频] 未配置手机 URL，回退到本地麦克风")
            audio_source = "mic"

        try:
            from .audio_pipeline import AudioPipeline, WakeWordDetector
            self._audio_pipeline = AudioPipeline(
                audio_source=audio_source,
                rtsp_url=rtsp_url,
                mic_device_index=self._ha_cfg.get("mic_device_index"),
                wyoming_host=self._ha_cfg.get("wyoming_host", "0.0.0.0"),
                wyoming_port=self._ha_cfg.get("wyoming_port", 10600),
                phone_url=phone_url,
                wake_words=wake_words,
            )
            self._audio_pipeline.on_speech = self._on_speech_detected
            self._audio_pipeline.on_wake = self._on_wake_word
            self._audio_pipeline.on_mode_change = self._on_audio_mode_change
            self._audio_pipeline.start()
            words = wake_words or WakeWordDetector.DEFAULT_KEYWORDS
            _log_event(f"[音频] 音频管线已启动（源: {audio_source}，待机模式，唤醒词: {words}）")
        except Exception as e:
            _log_event(f"[音频] 音频管线启动失败: {e}")

    def _on_wake_word(self):
        """唤醒词检测到，进入对话模式"""
        _log_event("[音频·唤醒] 检测到唤醒词！进入对话模式")
        self.enter_dialog()
        if self._agent_callback:
            self._agent_callback("[摄像头听到唤醒词] 你好，我在听，请说。")

    def _on_audio_mode_change(self, new_mode: str):
        """音频管线模式变化时的回调"""
        if new_mode == "standby" and self._mode != "standby":
            self.set_mode("standby")

    def _on_speech_detected(self, text: str):
        """VAD 检测到语音并 STT 转写后，推入 A 层"""
        _log_event(f"[音频·语音] 摄像头听到: {text[:80]}")
        self._last_speech_time = time.time()
        if self._agent_callback:
            self._agent_callback(f"[摄像头听到] {text}")

    def get_audio_status(self) -> dict:
        """获取音频管线状态"""
        if self._audio_pipeline:
            status = self._audio_pipeline.get_status()
            status["bridge_mode"] = self._mode
            return status
        return {"running": False, "bridge_mode": self._mode, "reason": "未启动"}

    def stop_all(self):
        """停止所有后台线程"""
        self.stop_periodic_capture()
        if self._audio_pipeline:
            self._audio_pipeline.stop()
            self._audio_pipeline = None
        if self._phone_sensor:
            self._phone_sensor = None
        self._mode = "standby"
        _log_event("[Bridge] 所有后台线程已停止")


# ── 日志 ──────────────────────────────────────────
_event_log = []


def _log_event(text: str):
    _event_log.append(text)
    print(f"[Bridge] {text}")


def get_event_log() -> list:
    return list(_event_log)
