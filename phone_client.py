"""
phone_client.py
手机 WebSocket 客户端
============================
在 Termux 里运行，连接云端，响应截图指令。

安装：
    pkg install python
    pip install websockets requests

运行：
    python phone_client.py

配置：修改下方 CLOUD_WS_URL 和 WS_TOKEN
"""
import asyncio
import json
import base64
import requests
import time
import sys
from datetime import datetime

# ── 配置（修改这里）──────────────────────────────────────────
CLOUD_WS_URL = "ws://123.56.65.97:18766"    # 云端 WebSocket 地址
WS_TOKEN = "agi2025202620272028"             # 认证 token（与云端一致）
IP_WEBCAM_URL = "http://localhost:8080"      # IP Webcam 本地地址
RECONNECT_INTERVAL = 5                       # 断线重连间隔（秒）
HEARTBEAT_INTERVAL = 30                      # 心跳间隔（秒）


def capture_frame() -> bytes:
    """从 IP Webcam 拿当前截图"""
    try:
        resp = requests.get(
            f"{IP_WEBCAM_URL}/shot.jpg",
            timeout=5
        )
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
    except Exception as e:
        print(f"[Client] 截图失败: {e}")
    return b""


def get_sensors() -> dict:
    """从 IP Webcam 获取传感器数据（先唤醒 GPS）"""
    # 先触发 GPS 唤醒（termux-location 会激活 GPS 芯片）
    _wake_gps()

    try:
        resp = requests.get(
            f"{IP_WEBCAM_URL}/sensors.json",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                gps_raw = data.get("gps")
                if gps_raw:
                    print(f"[Client] GPS 有数据: {str(gps_raw)[:120]}")
            return data
    except Exception as e:
        print(f"[Client] 传感器读取失败: {e}")
    return {}


def _wake_gps():
    """用 termux-location 唤醒 GPS 芯片"""
    import subprocess
    try:
        result = subprocess.run(
            ["termux-location", "once"],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0 and result.stdout:
            import json
            loc = json.loads(result.stdout)
            lat = loc.get("latitude", 0)
            lng = loc.get("longitude", 0)
            if lat and lng:
                print(f"[Client] GPS 已唤醒: ({lat:.4f}, {lng:.4f})")
            else:
                print(f"[Client] GPS 唤醒中（未定位）")
    except FileNotFoundError:
        pass  # termux-location 未安装
    except Exception as e:
        print(f"[Client] GPS 唤醒异常: {e}")


def format_sensor_data(raw: dict) -> dict:
    """将 IP Webcam 原始传感器数据格式化为统一格式

    IP Webcam 原始格式：
      {"gps": {"data": [[timestamp, [lat, lng, alt, accuracy]], ...], "unit": "..."}}
      {"battery_level": {"data": [[timestamp, [level]], ...]}}
      {"light": {"data": [[timestamp, [lux]], ...]}}
      {"accelerometer": {"data": [[timestamp, [x, y, z]], ...]}}
    """
    # GPS
    lat, lng, accuracy = 0.0, 0.0, 0.0
    gps_entry = raw.get("gps", {})
    gps_data = gps_entry.get("data", []) if isinstance(gps_entry, dict) else []
    if gps_data:
        latest = gps_data[-1]
        if isinstance(latest, list) and len(latest) >= 2:
            vals = latest[1]
            if isinstance(vals, list) and len(vals) >= 2:
                lat, lng = float(vals[0]), float(vals[1])
                if len(vals) >= 4:
                    accuracy = float(vals[3])

    # 电量
    battery = 0
    batt_entry = raw.get("battery_level", {})
    batt_data = batt_entry.get("data", []) if isinstance(batt_entry, dict) else []
    if batt_data:
        latest = batt_data[-1]
        if isinstance(latest, list) and len(latest) >= 2:
            bv = latest[1]
            if isinstance(bv, list) and bv:
                battery = float(bv[0])
            elif isinstance(bv, (int, float)):
                battery = float(bv)

    # 光线
    light = 0
    light_entry = raw.get("light", {})
    light_data = light_entry.get("data", []) if isinstance(light_entry, dict) else []
    if light_data:
        latest = light_data[-1]
        if isinstance(latest, list) and len(latest) >= 2:
            lv = latest[1]
            if isinstance(lv, list) and lv:
                light = float(lv[0])

    # 加速度
    accel = [0, 0, 0]
    accel_entry = raw.get("accelerometer", {})
    accel_data = accel_entry.get("data", []) if isinstance(accel_entry, dict) else []
    if accel_data:
        latest = accel_data[-1]
        if isinstance(latest, list) and len(latest) >= 2:
            av = latest[1]
            if isinstance(av, list) and len(av) >= 3:
                accel = [float(av[0]), float(av[1]), float(av[2])]

    return {
        "gps": {"lat": lat, "lng": lng, "accuracy": accuracy},
        "battery": battery,
        "light": light,
        "accelerometer": accel,
        "timestamp": datetime.now().isoformat()
    }


async def handle_server(websocket):
    """处理云端发来的消息"""
    print("[Client] 已连接云端，等待指令...")

    # 认证
    await websocket.send(json.dumps({
        "type": "token",
        "token": WS_TOKEN,
    }))

    # 等待认证结果
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        auth_resp = json.loads(raw)
        if auth_resp.get("type") != "auth_ok":
            print(f"[Client] 认证失败: {auth_resp}")
            return
        print("[Client] 认证成功")
    except asyncio.TimeoutError:
        print("[Client] 认证超时")
        return

    # 心跳任务
    async def heartbeat():
        while True:
            try:
                await websocket.send(json.dumps({"type": "ping"}))
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except Exception:
                break

    asyncio.create_task(heartbeat())

    async for message in websocket:
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "capture_request":
                request_id = data.get("request_id", "")
                print(f"[Client] 收到截图指令: {request_id}")

                img_bytes = capture_frame()

                if img_bytes:
                    # 二进制帧传输：request_id(8字节补齐) + JPEG
                    rid_bytes = request_id.encode("ascii")[:8].ljust(8)
                    await websocket.send(rid_bytes + img_bytes)
                    print(f"[Client] 截图已返回: {len(img_bytes)} bytes")
                else:
                    # 截图失败，返回空响应
                    await websocket.send(json.dumps({
                        "type": "frame_response",
                        "request_id": request_id,
                        "image": "",
                    }))

            elif msg_type == "sensor_request":
                request_id = data.get("request_id", "")
                print(f"[Client] 收到传感器请求: {request_id}")

                raw = get_sensors()
                sensor_data = format_sensor_data(raw) if raw else {}

                await websocket.send(json.dumps({
                    "type": "sensor_response",
                    "request_id": request_id,
                    "data": sensor_data
                }))
                print(f"[Client] 传感器数据已返回 gps=({sensor_data.get('gps',{}).get('lat')}, {sensor_data.get('gps',{}).get('lng')})")

            elif msg_type == "pong":
                pass

        except Exception as e:
            print(f"[Client] 处理消息错误: {e}")


async def main():
    """主循环，自动重连"""
    print(f"[Client] 目标: {CLOUD_WS_URL}")
    print(f"[Client] IP Webcam: {IP_WEBCAM_URL}")
    print()

    while True:
        try:
            async with websockets.connect(CLOUD_WS_URL) as websocket:
                await handle_server(websocket)
        except Exception as e:
            print(f"[Client] 连接失败: {e}")
        print(f"[Client] {RECONNECT_INTERVAL}秒后重连...")
        await asyncio.sleep(RECONNECT_INTERVAL)


if __name__ == "__main__":
    # 检查依赖
    try:
        import websockets
    except ImportError:
        print("缺少依赖: pip install websockets")
        sys.exit(1)
    try:
        import requests
    except ImportError:
        print("缺少依赖: pip install requests")
        sys.exit(1)

    asyncio.run(main())
