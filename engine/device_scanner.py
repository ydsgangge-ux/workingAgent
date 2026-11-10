"""
设备发现层（只读）

扫描当前环境里"看得见"的设备，供 A 层理解自己能操作什么：
  1. 本机   — 串口、摄像头、音频、USB、蓝牙（Windows PnP 枚举 + 注册表）
  2. 局域网 — ARP 邻居表 + 常见端口探测
  3. UPnP   — SSDP 组播发现（电视/路由器/媒体设备）
  4. 家居   — HomeAssistant 已接入的 entity

设计原则：
  - 全部只读，不下发任何控制指令，不发任何写操作
  - 不引入新依赖，缺什么就跳过什么（每个源独立 try）
  - 总耗时控制在数秒内，任一源超时不影响其它源
  - 只扫本机所在网段，不做跨网段探测

注意：发现 != 可控。能不能控制取决于是否存在对应协议/驱动。
"""

import json
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# 常见端口 → 设备类型猜测
PORT_HINTS = {
    80: "http",
    443: "https",
    554: "rtsp(摄像头)",
    1883: "mqtt",
    5000: "http(常见于NAS/自建服务)",
    8080: "http-alt",
    8123: "homeassistant",
    9100: "printer",
    37777: "dahua(摄像头)",
}

# 端口探测超时（秒）
PORT_TIMEOUT = 0.35
# 最多探测的主机数，防止扫太大网段
MAX_HOSTS = 24

# 系统基础设施噪声：这些不是"可接入设备"，只是驱动栈组件
_NOISE_PATTERNS = (
    "enumerator", "protocol tdi", "transport", "object push",
    "audio gateway", "virtual", "proxy", "service",
    "root hub", "host controller", "generic usb hub",
    "bluetooth device (rfcomm",
)


def _is_noise(name: str) -> bool:
    low = (name or "").lower()
    return any(p in low for p in _NOISE_PATTERNS)


def _run(cmd: List[str], timeout: float = 6.0,
         encoding: str = "gbk") -> str:
    """执行外部命令，失败返回空串（只读工具，不抛异常）"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = r.stdout or b""
        if isinstance(out, bytes):
            return out.decode(encoding, errors="ignore")
        return out
    except Exception:
        return ""


# ══════════════════════════════════════════════════
# 1. 本机设备
# ══════════════════════════════════════════════════

def scan_local() -> List[Dict]:
    """枚举本机外设：串口、摄像头、音频、USB、蓝牙"""
    devices: List[Dict] = []

    # ── 串口：注册表 HARDWARE\\DEVICEMAP\\SERIALCOMM（无需 pyserial）──
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DEVICEMAP\SERIALCOMM"
        )
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
                devices.append({
                    "id": f"serial:{value}",
                    "kind": "serial",
                    "name": f"{value} ({name})",
                    "protocol": "serial",
                    "capabilities": ["read", "write"],
                    "note": "串口设备，需知道其通信协议才能控制",
                })
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
    except Exception:
        pass

    # ── PnP 枚举：摄像头/音频/USB/蓝牙 ──
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Class -in @('Camera','Image','AudioEndpoint','USB','Bluetooth','MEDIA') } | "
        "Select-Object Class,FriendlyName,Status | ConvertTo-Json -Compress"
    )
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
               timeout=10, encoding="utf-8")
    if raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            for d in data:
                cls = (d.get("Class") or "").strip()
                name = (d.get("FriendlyName") or "").strip()
                status = (d.get("Status") or "").strip()
                if not name or status.lower() not in ("ok", ""):
                    continue
                if _is_noise(name):
                    continue
                kind = {
                    "Camera": "camera", "Image": "scanner",
                    "AudioEndpoint": "audio", "MEDIA": "audio",
                    "USB": "usb", "Bluetooth": "bluetooth",
                }.get(cls, "unknown")
                caps = {
                    "camera": ["snapshot", "video"],
                    "scanner": ["scan"],
                    "audio": ["play", "record"],
                    "usb": ["unknown"],
                    "bluetooth": ["pair"],
                }.get(kind, ["unknown"])
                devices.append({
                    "id": f"local:{kind}:{name}",
                    "kind": kind,
                    "name": name,
                    "protocol": "driver",
                    "capabilities": caps,
                    "note": "系统已装驱动",
                })
        except Exception:
            pass

    # 同 id 去重
    seen = set()
    uniq = []
    for d in devices:
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        uniq.append(d)
    return uniq


# ══════════════════════════════════════════════════
# 2. 局域网主机
# ══════════════════════════════════════════════════

def _local_ip() -> str:
    """取本机在局域网中的 IP（不发包，只看路由表）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def _arp_hosts() -> List[Tuple[str, str]]:
    """读 ARP 邻居表，返回 (ip, mac) 列表，仅限本机网段"""
    out = _run(["arp", "-a"], timeout=5, encoding="gbk")
    hosts = []
    my_ip = _local_ip()
    prefix = ".".join(my_ip.split(".")[:3]) if my_ip else ""
    for line in out.splitlines():
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F\-]{11,17})", line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2).upper()
        if ip.endswith(".255") or mac.startswith("FF-FF-FF"):
            continue
        # 只保留本机网段，避免扫到别人的网络
        if prefix and not ip.startswith(prefix + "."):
            continue
        if ip == my_ip:
            continue
        hosts.append((ip, mac))
    return hosts[:MAX_HOSTS]


def _probe_port(ip: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(PORT_TIMEOUT)
            return s.connect_ex((ip, port)) == 0
    except Exception:
        return False


def scan_lan(enable_port_scan: bool = True) -> List[Dict]:
    """扫描局域网在线主机，可选探测常见端口"""
    hosts = _arp_hosts()
    if not hosts:
        return []

    devices: List[Dict] = []
    if not enable_port_scan:
        for ip, mac in hosts:
            devices.append({
                "id": f"lan:{ip}",
                "kind": "host",
                "name": f"{ip} ({mac})",
                "protocol": "tcp",
                "capabilities": ["unknown"],
                "note": "在线主机，端口未探测",
            })
        return devices

    # 并发探测：每个主机探一遍常见端口
    tasks = [(ip, mac, port) for ip, mac in hosts for port in PORT_HINTS]
    results: Dict[str, List[int]] = {ip: [] for ip, _ in hosts}

    def _job(t):
        ip, _mac, port = t
        return ip, port, _probe_port(ip, port)

    try:
        with ThreadPoolExecutor(max_workers=32) as ex:
            for ip, port, ok in ex.map(_job, tasks):
                if ok:
                    results[ip].append(port)
    except Exception:
        pass

    for ip, mac in hosts:
        ports = sorted(results.get(ip, []))
        if not ports:
            continue
        hints = [PORT_HINTS.get(p, str(p)) for p in ports]
        kind = "unknown"
        caps = ["unknown"]
        if 8123 in ports:
            kind, caps = "homeassistant", ["ha_api"]
        elif 554 in ports or 37777 in ports:
            kind, caps = "camera", ["snapshot", "video"]
        elif 1883 in ports:
            kind, caps = "mqtt_broker", ["mqtt"]
        elif 9100 in ports:
            kind, caps = "printer", ["print"]
        elif 80 in ports or 443 in ports:
            kind, caps = "http_service", ["http"]
        devices.append({
            "id": f"lan:{ip}",
            "kind": kind,
            "name": f"{ip} ({mac})",
            "protocol": "tcp",
            "capabilities": caps,
            "ports": ports,
            "note": "开放端口: " + ", ".join(f"{p}({h})" for p, h in zip(ports, hints)),
        })
    return devices


# ══════════════════════════════════════════════════
# 3. UPnP / SSDP
# ══════════════════════════════════════════════════

SSDP_ADDR = ("239.255.255.250", 1900)


def scan_ssdp(timeout: float = 1.5) -> List[Dict]:
    """SSDP 组播发现 UPnP 设备（只发标准 M-SEARCH 查询）"""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n\r\n"
    ).encode()

    found: Dict[str, Dict] = {}
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.4)
        for _ in range(2):
            sock.sendto(msg, SSDP_ADDR)
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            text = data.decode("utf-8", errors="ignore")
            server = ""
            st = ""
            location = ""
            for line in text.splitlines():
                low = line.lower()
                if low.startswith("server:"):
                    server = line.split(":", 1)[1].strip()
                elif low.startswith("st:"):
                    st = line.split(":", 1)[1].strip()
                elif low.startswith("location:"):
                    location = line.split(":", 1)[1].strip()
            key = addr[0]
            if key in found:
                continue
            found[key] = {
                "id": f"upnp:{addr[0]}",
                "kind": "upnp",
                "name": (server or st or "UPnP 设备")[:80],
                "protocol": "upnp",
                "capabilities": ["upnp"],
                "note": f"service={st[:60]}" + (f" | {location[:80]}" if location else ""),
            }
    except Exception:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return list(found.values())


# ══════════════════════════════════════════════════
# 4. HomeAssistant 已接入设备
# ══════════════════════════════════════════════════

def _ha_config_path() -> Path:
    p = Path(__file__).resolve().parent
    app = p.parent
    root = app if (app / "main.py").exists() else Path.cwd()
    return root / "ha_config.json"


def scan_homeassistant() -> List[Dict]:
    """读 HA 的全部 entity，按域分类"""
    cfg_path = _ha_config_path()
    if not cfg_path.exists():
        return []
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        base = cfg.get("base_url", "").rstrip("/")
        token = cfg.get("token", "")
        if not base or not token:
            return []
        import requests
        resp = requests.get(
            f"{base}/api/states",
            headers={"Authorization": f"Bearer {token}"},
            timeout=6,
        )
        if resp.status_code != 200:
            return []
        states = resp.json()
    except Exception:
        return []

    cap_map = {
        "light": ["on", "off", "brightness"],
        "switch": ["on", "off"],
        "cover": ["open", "close", "position"],
        "climate": ["on", "off", "temperature"],
        "fan": ["on", "off", "speed"],
        "media_player": ["play", "pause", "volume"],
        "camera": ["snapshot"],
        "lock": ["lock", "unlock"],
        "sensor": ["read"],
        "binary_sensor": ["read"],
    }
    devices = []
    for s in states:
        eid = s.get("entity_id", "")
        if "." not in eid:
            continue
        domain = eid.split(".", 1)[0]
        if domain not in cap_map:
            continue
        attrs = s.get("attributes", {}) or {}
        name = attrs.get("friendly_name") or eid
        devices.append({
            "id": eid,
            "kind": domain,
            "name": name,
            "protocol": "homeassistant",
            "capabilities": cap_map[domain],
            "state": s.get("state", "unknown"),
        })
    return devices


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════

def scan_all(scope: str = "all", port_scan: bool = True) -> Dict:
    """
    汇总扫描（各来源并发，总耗时约等于最慢的那个）

    scope: local | lan | all
      local — 仅本机外设
      lan   — 本机 + 局域网 + UPnP
      all   — 再加 HomeAssistant
    """
    scope = (scope or "all").lower()
    errors: List[str] = []

    jobs = {"本机": (scan_local, ())}
    if scope in ("lan", "all"):
        jobs["局域网"] = (scan_lan, (port_scan,))
        jobs["UPnP"] = (scan_ssdp, ())
    if scope == "all":
        jobs["HomeAssistant"] = (scan_homeassistant, ())

    results: Dict[str, List[Dict]] = {}

    def _job(item):
        label, (fn, args) = item
        try:
            return label, fn(*args) or []
        except Exception as e:
            return label, [{"__error__": f"{label}: {e}"}]

    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for label, items in ex.map(_job, jobs.items()):
                results[label] = items
    except Exception as e:
        errors.append(str(e))

    devices: List[Dict] = []
    for label, items in results.items():
        for it in items:
            if "__error__" in it:
                errors.append(it["__error__"])
            else:
                devices.append(it)

    summary = build_summary(devices)
    return {
        "ok": True,
        "count": len(devices),
        "scope": scope,
        "my_ip": _local_ip(),
        "devices": devices,
        "summary": summary,
        "errors": errors,
    }


def build_summary(devices: List[Dict]) -> str:
    """生成给 A 层读的精简文字清单"""
    if not devices:
        return "未发现可接入设备。"

    groups: Dict[str, List[Dict]] = {}
    for d in devices:
        groups.setdefault(d.get("kind", "unknown"), []).append(d)

    label = {
        "serial": "串口", "camera": "摄像头", "scanner": "扫描仪",
        "audio": "音频", "usb": "USB", "bluetooth": "蓝牙",
        "host": "局域网主机", "http_service": "HTTP 服务",
        "homeassistant": "HomeAssistant", "mqtt_broker": "MQTT Broker",
        "printer": "打印机", "upnp": "UPnP 设备",
        "light": "灯", "switch": "开关", "cover": "窗帘",
        "climate": "空调", "fan": "风扇", "media_player": "媒体播放器",
        "sensor": "传感器", "binary_sensor": "二元传感器", "lock": "锁",
    }

    lines = []
    order = ["serial", "camera", "scanner", "audio", "usb", "bluetooth",
             "printer", "host", "http_service", "mqtt_broker", "upnp",
             "light", "switch", "cover", "climate", "fan",
             "media_player", "lock", "sensor", "binary_sensor"]
    keys = [k for k in order if k in groups] + \
           [k for k in groups if k not in order]

    for k in keys:
        items = groups[k]
        names = [i.get("name", "") for i in items][:8]
        more = f" 等{len(items)}个" if len(items) > 8 else ""
        lines.append(f"- {label.get(k, k)}（{len(items)}）：" +
                     "、".join(n for n in names if n) + more)

    lines.append("")
    lines.append("注意：能发现不代表能控制，私有协议设备需要额外适配才能操作。")
    return "\n".join(lines)
