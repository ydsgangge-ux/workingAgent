"""
传感器数据代理模块（Sensor Agent）
对接机器狗/机器人 SDK，将原始数字数据格式化成 A 层可直接理解的文字状态描述。

推送策略：
  1. 定时推送：每 30 秒轮询一次传感器数据
  2. 事件触发：检测到异常数据立即推送
  3. 主动查询：A 层通过工具按需拉取

安装：
  机器人SDK（可选，未安装不影响核心功能）：
    pip install paho-mqtt  （MQTT 协议，常见于机器人通信）

配置（在 config.json 中设置）：
  sensor_enabled: true/false
  sensor_type: "robot_dog" | "robot_arm" | "custom"
  sensor_mqtt_host: "localhost"
  sensor_mqtt_port: 1883
  sensor_push_interval: 30  （秒）
"""

import os
import sys
import json
import time
import threading
from typing import Optional, Dict, List, Callable
from datetime import datetime
from pathlib import Path


# ── 传感器数据类型定义 ─────────────────────────────────

class SensorDataType:
    """传感器数据类型"""
    BATTERY = "battery"           # 电量
    TEMPERATURE = "temperature"   # 温度
    IMU = "imu"                   # 姿态（加速度计+陀螺仪）
    MOTOR = "motor"               # 电机状态
    GPS = "gps"                   # GPS 位置
    ULTRASONIC = "ultrasonic"     # 超声波测距
    CAMERA = "camera"             # 摄像头状态
    PRESSURE = "pressure"         # 气压
    JOINT = "joint"               # 关节角度（机器狗/机械臂）
    SPEED = "speed"               # 移动速度
    OBSTACLE = "obstacle"         # 障碍物检测


# ── 异常阈值配置 ───────────────────────────────────────

ALERT_THRESHOLDS = {
    SensorDataType.BATTERY:       {"low": 15, "critical": 5},          # 百分比
    SensorDataType.TEMPERATURE:   {"high": 70, "critical": 85},       # 摄氏度
    SensorDataType.MOTOR:         {"high_temp": 65, "stall": True},    # 电机异常
    SensorDataType.OBSTACLE:      {"distance": 30},                    # 厘米
}


class SensorAgent:
    """
    传感器数据代理
    以工具插件形式运行，不修改 agent.py 主逻辑
    支持模拟模式（无硬件时使用模拟数据）
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._lock = threading.Lock()
        self._latest_data: Dict = {}
        self._alert_callbacks: List[Callable] = []
        self._push_thread: Optional[threading.Thread] = None
        self._stop_flag = False
        self._last_push_time = 0

        # 配置
        self.enabled = self._config.get("sensor_enabled", False)
        self.sensor_type = self._config.get("sensor_type", "robot_dog")
        self.push_interval = self._config.get("sensor_push_interval", 30)
        self.mqtt_host = self._config.get("sensor_mqtt_host", "localhost")
        self.mqtt_port = self._config.get("sensor_mqtt_port", 1883)
        self.mock_mode = self._config.get("sensor_mock", True)  # 默认模拟模式

        # MQTT 客户端（懒初始化）
        self._mqtt_client = None
        self._mqtt_connected = False

    def is_available(self) -> bool:
        """检查传感器模块是否可用"""
        return self.enabled or self.mock_mode

    def start_push_loop(self, callback: Callable = None):
        """
        启动定时推送循环
        callback: 回调函数，收到数据时调用 callback(data_dict)
        """
        if callback:
            self._alert_callbacks.append(callback)

        if self._push_thread and self._push_thread.is_alive():
            return  # 已在运行

        self._stop_flag = False
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        print(f"[Sensor] 定时推送已启动，间隔 {self.push_interval} 秒")

    def stop_push_loop(self):
        """停止定时推送"""
        self._stop_flag = True
        if self._push_thread and self._push_thread.is_alive():
            self._push_thread.join(timeout=5)
        print("[Sensor] 定时推送已停止")

    def _push_loop(self):
        """后台推送循环"""
        while not self._stop_flag:
            try:
                data = self.get_all_sensors()
                alerts = self._check_alerts(data)

                if alerts:
                    alert_text = self._format_alerts(alerts)
                    for cb in self._alert_callbacks:
                        try:
                            cb(alert_text, is_alert=True)
                        except Exception:
                            pass

                self._last_push_time = time.time()

            except Exception as e:
                print(f"[Sensor] 推送循环异常: {e}")

            # 等待下一个推送周期
            for _ in range(self.push_interval):
                if self._stop_flag:
                    break
                time.sleep(1)

    def _check_alerts(self, data: Dict) -> List[Dict]:
        """检查数据是否超出异常阈值"""
        alerts = []

        # 电量检测
        battery = data.get(SensorDataType.BATTERY, {})
        level = battery.get("level", 100)
        if level <= ALERT_THRESHOLDS[SensorDataType.BATTERY]["critical"]:
            alerts.append({"type": "critical", "sensor": "battery",
                           "message": f"电量严重不足（{level}%），请立即充电"})
        elif level <= ALERT_THRESHOLDS[SensorDataType.BATTERY]["low"]:
            alerts.append({"type": "warning", "sensor": "battery",
                           "message": f"电量偏低（{level}%），建议充电"})

        # 温度检测
        temp = data.get(SensorDataType.TEMPERATURE, {})
        value = temp.get("value", 25)
        if value >= ALERT_THRESHOLDS[SensorDataType.TEMPERATURE]["critical"]:
            alerts.append({"type": "critical", "sensor": "temperature",
                           "message": f"温度过高（{value}°C），有烧毁风险"})
        elif value >= ALERT_THRESHOLDS[SensorDataType.TEMPERATURE]["high"]:
            alerts.append({"type": "warning", "sensor": "temperature",
                           "message": f"温度偏高（{value}°C），请注意散热"})

        # 障碍物检测
        obstacle = data.get(SensorDataType.OBSTACLE, {})
        distance = obstacle.get("nearest_distance_cm", 999)
        if distance <= ALERT_THRESHOLDS[SensorDataType.OBSTACLE]["distance"]:
            alerts.append({"type": "warning", "sensor": "obstacle",
                           "message": f"前方障碍物距离仅 {distance}cm，请注意避障"})

        # 电机检测
        motor = data.get(SensorDataType.MOTOR, {})
        if motor.get("stall"):
            alerts.append({"type": "critical", "sensor": "motor",
                           "message": "电机堵转，请立即检查"})

        return alerts

    def _format_alerts(self, alerts: List[Dict]) -> str:
        """将告警格式化为文字描述"""
        lines = []
        for a in alerts:
            icon = "🚨" if a["type"] == "critical" else "⚠️"
            lines.append(f"{icon} {a['message']}")
        return "\n".join(lines)

    def get_all_sensors(self) -> Dict:
        """获取所有传感器数据（原始字典格式）"""
        with self._lock:
            if self.mock_mode:
                data = self._generate_mock_data()
            else:
                data = self._read_real_sensors()

            self._latest_data = data
            return data

    def get_status_text(self) -> str:
        """
        获取格式化的传感器状态文字描述
        这是 A 层可以直接理解的自然语言描述
        """
        data = self.get_all_sensors()
        return self._format_for_prompt(data)

    def _format_for_prompt(self, data: Dict) -> str:
        """将传感器数据格式化为 A 层可理解的自然语言"""
        lines = ["【当前传感器状态】"]

        # 电量
        battery = data.get(SensorDataType.BATTERY, {})
        if battery:
            level = battery.get("level", 100)
            voltage = battery.get("voltage", 0)
            if level > 50:
                status = "充足"
            elif level > 20:
                status = "偏低"
            else:
                status = "不足，需要充电"
            lines.append(f"- 电池：{level}%（{voltage}V），状态{status}")

        # 温度
        temp = data.get(SensorDataType.TEMPERATURE, {})
        if temp:
            value = temp.get("value", 25)
            lines.append(f"- 温度：{value}°C")

        # 姿态
        imu = data.get(SensorDataType.IMU, {})
        if imu:
            roll = imu.get("roll", 0)
            pitch = imu.get("pitch", 0)
            yaw = imu.get("yaw", 0)
            if abs(roll) < 5 and abs(pitch) < 5:
                posture = "平稳站立"
            elif abs(roll) > 45 or abs(pitch) > 45:
                posture = "严重倾斜"
            else:
                posture = "轻微倾斜"
            lines.append(f"- 姿态：{posture}（俯仰{pitch:.1f}°，横滚{roll:.1f}°，航向{yaw:.1f}°）")

        # 移动速度
        speed = data.get(SensorDataType.SPEED, {})
        if speed:
            v = speed.get("linear_x", 0)
            lines.append(f"- 速度：{abs(v):.2f} m/s")

        # 关节角度
        joints = data.get(SensorDataType.JOINT, {})
        if joints:
            joint_angles = joints.get("angles", {})
            if joint_angles:
                parts = [f"{k}:{v:.0f}°" for k, v in list(joint_angles.items())[:4]]
                lines.append(f"- 关节角度：{', '.join(parts)}")

        # 障碍物
        obstacle = data.get(SensorDataType.OBSTACLE, {})
        if obstacle:
            dist = obstacle.get("nearest_distance_cm", 999)
            if dist < 100:
                lines.append(f"- 障碍物：前方 {dist}cm 处检测到障碍物")
            else:
                lines.append("- 障碍物：前方畅通")

        # GPS
        gps = data.get(SensorDataType.GPS, {})
        if gps:
            lat = gps.get("latitude", 0)
            lon = gps.get("longitude", 0)
            if lat != 0 or lon != 0:
                lines.append(f"- 位置：({lat:.6f}, {lon:.6f})")

        # 时间戳
        timestamp = data.get("timestamp", datetime.now().isoformat())
        lines.append(f"- 更新时间：{timestamp}")

        return "\n".join(lines)

    # ── 模拟数据生成 ────────────────────────────────────

    def _generate_mock_data(self) -> Dict:
        """生成模拟传感器数据（无硬件时使用）"""
        import random
        t = datetime.now()

        # 模拟电量缓慢下降
        hour = t.hour
        base_level = 85 - hour * 2
        level = max(5, min(100, base_level + random.randint(-3, 3)))

        return {
            "timestamp": t.isoformat(),
            "source": "mock",
            SensorDataType.BATTERY: {
                "level": level,
                "voltage": round(11.1 + level / 100 * 1.2, 2),
                "charging": False
            },
            SensorDataType.TEMPERATURE: {
                "value": round(35 + random.uniform(-3, 8), 1),
                "sensor_count": 3
            },
            SensorDataType.IMU: {
                "roll": round(random.uniform(-2, 2), 1),
                "pitch": round(random.uniform(-1, 1), 1),
                "yaw": round(random.uniform(0, 360), 1),
                "accel": {"x": round(random.uniform(-0.1, 0.1), 3),
                          "y": round(random.uniform(-0.1, 0.1), 3),
                          "z": round(9.7 + random.uniform(-0.1, 0.1), 3)}
            },
            SensorDataType.SPEED: {
                "linear_x": round(random.uniform(-0.01, 0.01), 3),
                "linear_y": 0,
                "angular_z": round(random.uniform(-0.01, 0.01), 3)
            },
            SensorDataType.MOTOR: {
                "stall": False,
                "temperatures": [round(random.uniform(30, 50), 1) for _ in range(4)],
                "currents": [round(random.uniform(0.1, 0.5), 2) for _ in range(4)]
            },
            SensorDataType.JOINT: {
                "angles": {
                    "FL_hip": round(random.uniform(-10, 10), 1),
                    "FL_knee": round(random.uniform(-20, 20), 1),
                    "FR_hip": round(random.uniform(-10, 10), 1),
                    "FR_knee": round(random.uniform(-20, 20), 1),
                    "BL_hip": round(random.uniform(-10, 10), 1),
                    "BL_knee": round(random.uniform(-20, 20), 1),
                    "BR_hip": round(random.uniform(-10, 10), 1),
                    "BR_knee": round(random.uniform(-20, 20), 1),
                }
            },
            SensorDataType.OBSTACLE: {
                "nearest_distance_cm": round(random.uniform(50, 500), 0),
                "direction": random.choice(["front", "left", "right", "back"])
            },
            SensorDataType.GPS: {
                "latitude": round(31.2304 + random.uniform(-0.001, 0.001), 6),
                "longitude": round(121.4737 + random.uniform(-0.001, 0.001), 6),
                "altitude": round(random.uniform(3, 15), 1),
                "satellites": random.randint(6, 12)
            },
            SensorDataType.PRESSURE: {
                "value": round(random.uniform(1010, 1020), 1),
                "altitude_estimate": round(random.uniform(3, 15), 1)
            }
        }

    # ── 真实传感器读取 ──────────────────────────────────

    def _read_real_sensors(self) -> Dict:
        """从真实硬件读取传感器数据"""
        result = {"timestamp": datetime.now().isoformat(), "source": "real"}

        # 尝试 MQTT 连接
        if self._try_mqtt_read(result):
            return result

        # 尝试串口读取（常见的机器人通信方式）
        if self._try_serial_read(result):
            return result

        # 如果都失败了，回退到模拟数据
        print("[Sensor] 真实传感器读取失败，使用模拟数据")
        return self._generate_mock_data()

    def _try_mqtt_read(self, result: Dict) -> bool:
        """尝试通过 MQTT 读取传感器数据"""
        try:
            import paho.mqtt.client as mqtt

            received = threading.Event()
            data_holder = {}

            def on_connect(client, userdata, flags, rc, properties=None):
                client.subscribe("sensor/data/#")

            def on_message(client, userdata, msg):
                try:
                    payload = json.loads(msg.payload.decode())
                    data_holder["data"] = payload
                    received.set()
                except Exception:
                    pass

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect
            client.on_message = on_message

            client.connect(self.mqtt_host, self.mqtt_port, timeout=5)
            client.loop_start()

            if received.wait(timeout=3):
                result.update(data_holder["data"])
                client.loop_stop()
                client.disconnect()
                return True

            client.loop_stop()
            client.disconnect()
            return False

        except ImportError:
            return False
        except Exception as e:
            print(f"[Sensor] MQTT 读取失败: {e}")
            return False

    def _try_serial_read(self, result: Dict) -> bool:
        """尝试通过串口读取传感器数据"""
        try:
            import serial
            import serial.tools.list_ports

            # 查找可用串口
            ports = serial.tools.list_ports.comports()
            if not ports:
                return False

            # 连接第一个可用串口
            port = ports[0].device
            ser = serial.Serial(port, baudrate=115200, timeout=2)

            # 发送查询命令
            ser.write(b"GET_SENSORS\n")
            response = ser.read_all()

            ser.close()

            if response:
                data = json.loads(response.decode().strip())
                result.update(data)
                return True

        except ImportError:
            return False
        except Exception as e:
            print(f"[Sensor] 串口读取失败: {e}")
            return False

    # ── 控制指令（发给机器人的命令）─────────────────────

    def send_command(self, command: str, params: dict = None) -> Dict:
        """
        发送控制指令到机器人
        command: "walk" / "sit" / "stand" / "stop" / "turn_left" / "turn_right" / "custom"
        """
        if self.mock_mode:
            print(f"[Sensor] 模拟模式：发送指令 {command} {params or ''}")
            return {
                "ok": True,
                "command": command,
                "message": f"模拟模式：已发送指令 '{command}'",
                "mode": "mock"
            }

        try:
            import paho.mqtt.client as mqtt

            payload = {"command": command, "params": params or {}, "ts": datetime.now().isoformat()}
            sent = threading.Event()
            error_holder = {}

            def on_connect(client, userdata, flags, rc, properties=None):
                client.publish("robot/command", json.dumps(payload), qos=1)
                sent.set()

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect

            client.connect(self.mqtt_host, self.mqtt_port, timeout=5)
            client.loop_start()

            if sent.wait(timeout=5):
                client.loop_stop()
                client.disconnect()
                return {"ok": True, "command": command, "message": f"指令 '{command}' 已发送"}

            client.loop_stop()
            client.disconnect()
            return {"ok": False, "error": "指令发送超时"}

        except ImportError:
            return {"ok": False, "error": "请安装 paho-mqtt: pip install paho-mqtt"}
        except Exception as e:
            return {"ok": False, "error": f"指令发送失败: {e}"}

    def get_latest_data(self) -> Dict:
        """获取最近一次的传感器数据（不重新读取）"""
        return self._latest_data.copy()


# ── 全局单例 ────────────────────────────────────────────

_sensor_instance: Optional[SensorAgent] = None


def get_sensor_agent(config: dict = None) -> SensorAgent:
    global _sensor_instance
    if _sensor_instance is None:
        _sensor_instance = SensorAgent(config)
    return _sensor_instance


def init_sensor_agent(config: dict = None):
    """初始化传感器代理（在启动时调用）"""
    global _sensor_instance
    _sensor_instance = SensorAgent(config)
    if _sensor_instance.is_available():
        print(f"[Sensor] 传感器代理已初始化（{_sensor_instance.sensor_type}，"
              f"{'模拟' if _sensor_instance.mock_mode else '真实'}模式）")
    return _sensor_instance
