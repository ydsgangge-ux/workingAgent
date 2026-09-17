"""
假传感器数据流 — 模拟机器狗 SDK 输出
=====================================
每 30 秒输出一组传感器数据，通过回调分发。
纯 Python，零硬件依赖。
"""
import time
import random
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class SensorPacket:
    """一次传感器数据快照"""
    battery: float = 80.0
    collision: bool = False
    motion: str = "idle"          # idle / walking / turning
    indoor_coords: dict = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    timestamp: str = ""


class MockSensorStream:
    """周期性输出假传感器数据"""

    def __init__(self, interval: float = 30.0):
        self.interval = interval
        self._x, self._y = 0.0, 0.0
        self._listeners: list[Callable[[SensorPacket], None]] = []
        self._thread: Optional[threading.Thread] = None

    def on_data(self, callback: Callable[[SensorPacket], None]):
        """注册数据回调"""
        self._listeners.append(callback)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._thread = None  # daemon thread will exit on main exit

    # ── 内部 ─────────────────────────────────────────

    def _loop(self):
        while True:
            packet = self._generate()
            for cb in self._listeners:
                try:
                    cb(packet)
                except Exception as e:
                    print(f"[MockSensor] callback error: {e}")
            time.sleep(self.interval)

    def _generate(self) -> SensorPacket:
        self._x += random.uniform(-1.5, 1.5)
        self._y += random.uniform(-1.5, 1.5)
        return SensorPacket(
            battery=round(random.uniform(50, 100), 1),
            collision=random.random() < 0.05,
            motion=random.choice(["idle", "walking", "turning"]),
            indoor_coords={"x": round(self._x, 2), "y": round(self._y, 2)},
            timestamp=time.strftime("%H:%M:%S"),
        )
