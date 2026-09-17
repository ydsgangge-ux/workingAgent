"""
location_resolver.py
GPS 坐标 → 语义位置描述
优先匹配个人地标库，未命中则调用高德逆地理编码

使用方式：
    resolve_location(31.2304, 121.4737)
    → {"landmark_ref": "我家", "location_text": "在我家附近", ...}

配置：
    1. 高德 Key：设置环境变量 AMAP_KEY，或在 ha_config.json 中添加 "amap_key"
    2. 个人地标：hardware/landmarks.json（示例）或 /root/.workingagent/landmarks.json（生效）
"""
import json
import math
import os
from pathlib import Path
from typing import Optional

import requests


# ── 配置优先级：环境变量 > ha_config.json > 空 ──
def _get_amap_key() -> str:
    """获取高德 API Key"""
    key = os.environ.get("AMAP_KEY", "")
    if key:
        return key
    try:
        cfg_path = Path(__file__).parent.parent / "ha_config.json"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("amap_key", "")
    except Exception:
        pass
    return ""


AMAP_KEY = _get_amap_key()
if not AMAP_KEY:
    print("[LocationResolver] 警告：未配置高德 AMAP_KEY，逆地理编码不可用")


# ── 浏览器 GPS 缓存（手机打开网页聊天时通过浏览器 API 上报）──
_browser_gps = {}  # {"lat": 31.23, "lng": 121.47, "accuracy": 15, "updated_at": "..."}


def update_browser_gps(lat: float, lng: float, accuracy: float = 0):
    """从网页前端接收浏览器定位结果"""
    from datetime import datetime
    _browser_gps.update({
        "lat": lat,
        "lng": lng,
        "accuracy": accuracy,
        "updated_at": datetime.now().isoformat(),
    })
    print(f"[LocationResolver] 浏览器GPS已更新: ({lat:.4f}, {lng:.4f}) 精度={accuracy:.0f}m")


def get_browser_gps() -> dict:
    """获取浏览器上报的GPS（用于手机传感器无数据时的兜底）"""
    return _browser_gps.copy() if _browser_gps else {}


def _load_landmarks() -> dict:
    """
    加载个人地标库
    优先级：/root/.workingagent/landmarks.json > hardware/landmarks.json
    """
    paths = [
        Path("/root/.workingagent/landmarks.json"),
        Path(__file__).parent / "landmarks.json",
    ]
    for p in paths:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print(f"[LocationResolver] 加载地标库: {p}")
                return data
            except Exception as e:
                print(f"[LocationResolver] 加载地标库失败 {p}: {e}")
    return {}


def _haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """计算两个 GPS 坐标之间的直线距离（米）"""
    R = 6371000  # 地球半径
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def match_landmark(lat: float, lng: float) -> Optional[dict]:
    """
    匹配个人地标库
    返回 {"name": "公司", "distance": 45.2} 或 None
    """
    landmarks = _load_landmarks()
    best = None
    best_dist = float("inf")
    for name, info in landmarks.items():
        dist = _haversine_distance(lat, lng, info["lat"], info["lng"])
        if dist <= info["radius"] and dist < best_dist:
            best = {"name": name, "distance": round(dist, 1)}
            best_dist = dist
    return best


def reverse_geocode(lat: float, lng: float) -> str:
    """调用高德 API 逆地理编码，返回地址描述"""
    if not AMAP_KEY:
        return "未知位置"
    try:
        url = "https://restapi.amap.com/v3/geocode/regeo"
        params = {
            "location": f"{lng},{lat}",  # 高德是 经度,纬度
            "key": AMAP_KEY,
            "radius": 1000,
            "extensions": "base",
        }
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        if data.get("status") == "1":
            return data["regeocode"]["formatted_address"]
    except Exception as e:
        print(f"[LocationResolver] 逆地理编码失败: {e}")
    return "未知位置"


def resolve_location(lat: float, lng: float) -> dict:
    """
    主函数：GPS 坐标 → 完整语义位置信息
    返回字段对应 VisualMemory 模板

    返回示例（命中地标）：
        {"landmark_ref": "公司", "location_text": "在公司附近", "location_confidence": 0.95, "gps": {...}}
    返回示例（未命中）：
        {"landmark_ref": None, "location_text": "上海市浦东新区xx路附近", "location_confidence": 0.6, "gps": {...}}

    当 GPS 为 0,0 时自动用地标库第一个地标（"我家"）做兜底，
    避免室内无 GPS 信号时完全丢失位置感知。
    """
    # GPS 为 0,0 → 依次兜底：浏览器GPS → 地标库默认位置
    if lat == 0.0 and lng == 0.0:
        # 优先用浏览器上报的 GPS
        browser = get_browser_gps()
        if browser.get("lat"):
            lat, lng = browser["lat"], browser["lng"]
            print(f"[LocationResolver] 使用浏览器GPS兜底: ({lat:.4f}, {lng:.4f})")
            # 不 return，用浏览器坐标继续下面的地标匹配逻辑

        # 次选地标库第一个
        landmarks = _load_landmarks()
        for name, info in landmarks.items():
            return {
                "landmark_ref": name,
                "location_text": f"在{name}附近（默认位置）",
                "location_confidence": 0.6,
                "gps": {"lat": info["lat"], "lng": info["lng"]},
            }
        return {
            "landmark_ref": None,
            "location_text": "未知位置（无 GPS 信号）",
            "location_confidence": 0.0,
            "gps": {"lat": 0.0, "lng": 0.0},
        }

    landmark = match_landmark(lat, lng)

    if landmark:
        return {
            "landmark_ref": landmark["name"],
            "location_text": f"在{landmark['name']}附近",
            "location_confidence": 0.95,
            "gps": {"lat": lat, "lng": lng},
        }

    address = reverse_geocode(lat, lng)
    return {
        "landmark_ref": None,
        "location_text": address,
        "location_confidence": 0.6 if address != "未知位置" else 0.0,
        "gps": {"lat": lat, "lng": lng},
    }
