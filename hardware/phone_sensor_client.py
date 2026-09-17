"""
手机传感器客户端 — IP Webcam HTTP API
======================================
通过 IP Webcam App（安卓）提供的 HTTP 接口，拉取：
  - 视频帧（/shot.jpg）
  - 传感器数据（/sensors.json）：GPS、加速度、陀螺仪、光线、电量
  - 格式化传感器文本供 A 层读取

手机端准备：
  1. 安装 IP Webcam App
  2. 启动服务，记下地址（如 http://192.168.1.88:8080）
  3. 手机和电脑连同一个 WiFi
  4. ⚠️ 在 App 底部"传感器"菜单里勾选需要的传感器（GPS、加速度等），
     否则 /sensors.json 返回空！
"""

import json
import threading
import time
from typing import Optional, Callable

import requests


def _log(msg: str):
    print(f"[PhoneSensor] {msg}")


class PhoneSensorClient:
    """从 IP Webcam 拉取手机所有传感器数据"""

    def __init__(self, phone_url: str):
        self.url = phone_url.rstrip("/")
        self._listeners: list[Callable[[str], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_sensors: dict = {}
        self._connected = False
        self._sensor_logged = False  # 只打印一次原始数据

    @property
    def connected(self) -> bool:
        return self._connected

    # ── 视频帧 ────────────────────────────────────────

    def capture_frame(self) -> bytes:
        """拉取当前视频帧（JPEG bytes），可直接送视觉记忆模块"""
        try:
            resp = requests.get(f"{self.url}/shot.jpg", timeout=5)
            if resp.status_code == 200 and len(resp.content) > 1000:
                return resp.content
        except requests.exceptions.ConnectionError:
            self._connected = False
        except Exception as e:
            _log(f"截图失败: {e}")
        return b""

    # ── 传感器数据 ────────────────────────────────────

    def get_sensors(self) -> dict:
        """拉取所有传感器数据（原始 JSON）"""
        try:
            resp = requests.get(f"{self.url}/sensors.json", timeout=5)
            if resp.status_code == 200:
                self._connected = True
                data = resp.json()
                self._last_sensors = data

                # 首次获取时打印原始数据结构，方便调试
                if not self._sensor_logged:
                    self._sensor_logged = True
                    keys = list(data.keys()) if isinstance(data, dict) else str(type(data))
                    _log(f"传感器原始键: {keys}")
                    if not data:
                        _log("⚠️ 传感器数据为空！请在 IP Webcam App 底部菜单勾选需要的传感器")

                return data
        except requests.exceptions.ConnectionError:
            self._connected = False
        except Exception as e:
            _log(f"传感器读取失败: {e}")
        return {}

    # ── 格式化文本（供 A 层读取）──────────────────────

    def format_sensor_text(self) -> str:
        """
        格式化传感器数据为 A 层可读文字。
        IP Webcam 的 /sensors.json 格式：
        {
          "accelerometer": {"data": [[x,y,z], ...], "desc": "..."},
          "battery_level": {"data": [level], "desc": "..."},
          "battery_charging": {"data": [0/1], "desc": "..."},
          "light": {"data": [lux], "desc": "..."},
          "gps": {"data": [[lat, lng, alt, accuracy, speed]], "desc": "..."},
          ...
        }
        """
        sensors = self.get_sensors()
        if not sensors:
            return "[手机传感器] 数据不可用（请在 IP Webcam App 勾选传感器）"

        parts = ["[手机传感器]"]

        # 电量
        batt = self._extract_sensor_value(sensors, "battery_level")
        if batt is not None:
            parts.append(f"电量{batt:.0f}%")

        # GPS
        gps_data = self._extract_sensor_array(sensors, "gps")
        if gps_data and len(gps_data) >= 2:
            lat, lng = gps_data[0], gps_data[1]
            parts.append(f"GPS({lat:.4f}, {lng:.4f})")

        # 光线
        light = self._extract_sensor_value(sensors, "light")
        if light is not None:
            parts.append(f"光线{light:.0f}lux")

        # 加速度
        accel_data = self._extract_sensor_array(sensors, "accelerometer")
        if accel_data and len(accel_data) >= 3:
            parts.append(f"加速度({accel_data[0]:.1f}, {accel_data[1]:.1f}, {accel_data[2]:.1f})")

        # 陀螺仪
        gyro_data = self._extract_sensor_array(sensors, "gyroscope")
        if gyro_data and len(gyro_data) >= 3:
            parts.append(f"陀螺仪({gyro_data[0]:.1f}, {gyro_data[1]:.1f}, {gyro_data[2]:.1f})")

        # 磁力计
        mag_data = self._extract_sensor_array(sensors, "magnetic_field")
        if mag_data and len(mag_data) >= 3:
            parts.append(f"磁力({mag_data[0]:.1f}, {mag_data[1]:.1f}, {mag_data[2]:.1f})")

        # 接近传感器
        prox = self._extract_sensor_value(sensors, "proximity")
        if prox is not None:
            parts.append(f"距离{prox:.1f}cm")

        if len(parts) == 1:
            return "[手机传感器] 已连接但无传感器数据（请在 App 勾选传感器）"

        return " ".join(parts)

    @staticmethod
    def _extract_sensor_value(sensors: dict, key: str):
        """
        从 IP Webcam 传感器 JSON 中提取单个值。
        IP Webcam 真实格式:
          {"key": {"data": [[timestamp, [value]], ...], "unit": "..."}}
        每个数据点是 [timestamp, [value]]，取最新一个数据点的 value。
        """
        entry = sensors.get(key)
        if not entry or not isinstance(entry, dict):
            return None
        data = entry.get("data")
        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        # 取最新数据点（最后一个）
        latest = data[-1]

        # 格式1: [timestamp, [value]]  — IP Webcam 标准格式
        if isinstance(latest, list) and len(latest) >= 2:
            ts = latest[0]
            val = latest[1]
            # val 可能是 [value] 或 [x, y, z]
            if isinstance(val, list) and len(val) > 0:
                return float(val[0])
            if isinstance(val, (int, float)):
                return float(val)

        # 格式2: [value] — 简单列表
        if isinstance(latest, (int, float)):
            return float(latest)

        return None

    @staticmethod
    def _extract_sensor_array(sensors: dict, key: str):
        """
        从 IP Webcam 传感器 JSON 中提取数组值（如加速度、GPS）。
        IP Webcam 真实格式:
          {"key": {"data": [[timestamp, [x, y, z]], ...], "unit": "..."}}
        每个数据点是 [timestamp, [x, y, z]]，取最新一个数据点的值数组。
        """
        entry = sensors.get(key)
        if not entry or not isinstance(entry, dict):
            return None
        data = entry.get("data")
        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        # 取最新数据点（最后一个）
        latest = data[-1]

        # 格式1: [timestamp, [x, y, z]]  — IP Webcam 标准格式
        if isinstance(latest, list) and len(latest) >= 2:
            val = latest[1]
            if isinstance(val, list) and len(val) > 0:
                return val

        # 格式2: [x, y, z] — 简单列表
        if isinstance(latest, list) and all(isinstance(v, (int, float)) for v in latest):
            return latest

        return None

    # ── 按需获取（视觉写入时调用）──────────────────────

    def get_gps(self) -> dict | None:
        """
        按需获取 GPS 数据，返回 {"lat": float, "lng": float, "alt": float, "accuracy": float}
        或 None（无 GPS 数据）。
        """
        sensors = self.get_sensors()
        if not sensors:
            return None

        gps_data = self._extract_sensor_array(sensors, "gps")
        if gps_data and len(gps_data) >= 2:
            result = {
                "lat": float(gps_data[0]),
                "lng": float(gps_data[1]),
            }
            if len(gps_data) >= 3:
                result["alt"] = float(gps_data[2])
            if len(gps_data) >= 4:
                result["accuracy"] = float(gps_data[3])
            if len(gps_data) >= 5:
                result["speed"] = float(gps_data[4])
            return result
        return None

    def get_battery(self) -> float | None:
        """按需获取电量百分比"""
        sensors = self.get_sensors()
        if not sensors:
            return None
        return self._extract_sensor_value(sensors, "battery_level")

    def get_light(self) -> float | None:
        """按需获取光线 lux"""
        sensors = self.get_sensors()
        if not sensors:
            return None
        return self._extract_sensor_value(sensors, "light")

    # ── 连接检测 ──────────────────────────────────────

    def check_connection(self) -> bool:
        """检测手机是否在线"""
        try:
            resp = requests.get(f"{self.url}/shot.jpg", timeout=3)
            self._connected = resp.status_code == 200
        except Exception:
            self._connected = False
        return self._connected
