"""
HA 摄像头 — 从摄像头拉取当前帧
================================
优先级：
  1. 手机 IP Webcam（/shot.jpg，最简单可靠）
  2. RTSP 直连摄像头抓帧
  3. HA camera_proxy API

配置从 ha_config.json 读取。
"""
import json
from pathlib import Path
from typing import Optional

import cv2
import requests


class HACamera:
    """摄像头截图：手机优先 → RTSP → HA API"""

    def __init__(self, config_path: str = "ha_config.json"):
        self._ha_url = ""
        self._token = ""
        self._entity_id = ""
        self._rtsp_url = ""
        self._phone_url = ""
        self._load_config(config_path)

    def _load_config(self, config_path: str):
        # 尝试多个路径：当前工作目录 → ha_camera.py 所在的项目根目录
        paths_to_try = [
            Path(config_path),
            Path(__file__).parent.parent / config_path,
        ]
        p = None
        for candidate in paths_to_try:
            if candidate.exists():
                p = candidate
                break
        if p is None:
            print(f"[HACamera] 配置文件不存在（已尝试: {paths_to_try}）")
            return

        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            self._ha_url = cfg.get("base_url", "").rstrip("/")
            self._token = cfg.get("token", "")
            self._rtsp_url = cfg.get("rtsp_url", "")
            self._phone_url = cfg.get("phone_url", "")

            devices = cfg.get("devices", {})
            self._entity_id = devices.get("摄像头", "")

            if self._phone_url:
                print(f"[HACamera] 手机摄像头已配置: {self._phone_url}")
            if self._rtsp_url:
                print(f"[HACamera] RTSP 已配置: {self._rtsp_url[:40]}...")
            if all([self._ha_url, self._token, self._entity_id]):
                print(f"[HACamera] HA API 已配置: {self._ha_url} / {self._entity_id}")

            if not self._phone_url and not self._rtsp_url and not all([self._ha_url, self._token, self._entity_id]):
                print("[HACamera] 配置不完整，需要 phone_url / rtsp_url / HA API 配置")
        except Exception as e:
            print(f"[HACamera] 读取配置失败: {e}")

    @property
    def available(self) -> bool:
        return (bool(self._phone_url)
                or bool(self._rtsp_url)
                or all([self._ha_url, self._token, self._entity_id]))

    def capture(self) -> Optional[bytes]:
        """
        拉取摄像头当前帧，返回 JPEG bytes。
        优先级：手机WebSocket → 手机IP Webcam → RTSP → HA camera_proxy。
        """
        if not self.available:
            # 即使配置不完整，也尝试 WebSocket
            img = self._capture_ws()
            if img:
                return img
            print("[HACamera] 未配置，无法拉取")
            return None

        # 优先手机 WebSocket（外出模式）
        img = self._capture_ws()
        if img:
            return img

        # 手机 IP Webcam（局域网模式）
        if self._phone_url:
            img = self._capture_phone()
            if img:
                return img
            print("[HACamera] 手机抓帧失败，降级到 RTSP...")

        # RTSP
        if self._rtsp_url:
            img = self._capture_rtsp()
            if img:
                return img
            print("[HACamera] RTSP 抓帧失败，降级到 HA API...")

        return self._capture_ha_proxy()

    def _capture_ws(self) -> Optional[bytes]:
        """通过 WebSocket 从外出手机获取截图"""
        try:
            from hardware.phone_ws_server import get_phone_server
            ws = get_phone_server()
            if ws and ws.is_connected():
                img = ws.sync_capture(timeout=10)
                if img and len(img) > 1000:
                    print(f"[HACamera] WebSocket 截图成功，{len(img)} bytes")
                    return img
                if img:
                    print(f"[HACamera] WebSocket 截图数据过小，跳过")
        except ImportError:
            pass
        except Exception as e:
            print(f"[HACamera] WebSocket 截图异常: {e}")
        return None

    def _capture_phone(self) -> Optional[bytes]:
        """从手机 IP Webcam 拉取当前帧"""
        try:
            resp = requests.get(f"{self._phone_url}/shot.jpg", timeout=5)
            if resp.status_code == 200 and len(resp.content) > 1000:
                print(f"[HACamera] 手机抓帧成功，{len(resp.content)} bytes")
                return resp.content
            print(f"[HACamera] 手机抓帧失败: HTTP {resp.status_code}")
        except requests.exceptions.ConnectionError:
            print("[HACamera] 手机连接失败")
        except Exception as e:
            print(f"[HACamera] 手机异常: {e}")
        return None

    def _capture_rtsp(self) -> Optional[bytes]:
        try:
            cap = cv2.VideoCapture(self._rtsp_url)
            if not cap.isOpened():
                print("[HACamera] RTSP 连接失败")
                return None

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                print("[HACamera] RTSP 读取帧失败")
                return None

            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            img_bytes = bytes(buf)
            print(f"[HACamera] RTSP 抓帧成功，{len(img_bytes)} bytes")
            return img_bytes
        except Exception as e:
            print(f"[HACamera] RTSP 异常: {e}")
            return None

    def _capture_ha_proxy(self) -> Optional[bytes]:
        if not all([self._ha_url, self._token, self._entity_id]):
            return None

        url = f"{self._ha_url}/api/camera_proxy/{self._entity_id}"
        headers = {"Authorization": f"Bearer {self._token}"}

        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.exceptions.ConnectionError:
            print("[HACamera] 连接 HA 失败")
            return None
        except requests.exceptions.Timeout:
            print("[HACamera] HA 请求超时")
            return None

        if resp.status_code == 200 and len(resp.content) > 1000:
            print(f"[HACamera] HA API 拉取成功，{len(resp.content)} bytes")
            return resp.content

        print(f"[HACamera] HA API 拉取失败: HTTP {resp.status_code}")
        return None
