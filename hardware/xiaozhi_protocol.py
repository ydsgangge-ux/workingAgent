"""
小智通信协议解析
处理 WebSocket 消息的序列化/反序列化
"""
import json
from typing import Dict, Any


def parse_message(data: str) -> Dict[str, Any]:
    """解析小智发来的 JSON 控制消息"""
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        print(f"[Protocol] JSON 解析失败: {e}")
        return {"type": "unknown", "error": str(e)}


# ── 消息构建 ──────────────────────────────────────


def build_hello() -> str:
    """构建握手回复"""
    return json.dumps({
        "type": "hello",
        "version": 3,
        "transport": "websocket",
        "audio_params": {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        },
    })


def build_stt(text: str, finished: bool = True) -> str:
    """构建 STT 结果消息（在小智屏幕上显示）"""
    return json.dumps({
        "type": "stt",
        "text": text,
        "finished": finished,
    })


def build_tts_start(content: str) -> str:
    """构建 TTS 开始消息"""
    return json.dumps({
        "type": "tts",
        "state": "start",
        "role": "assistant",
        "content": content,
    })


def build_tts_stop() -> str:
    """构建 TTS 停止消息"""
    return json.dumps({
        "type": "tts",
        "state": "stop",
    })


def build_error(message: str) -> str:
    """构建错误消息"""
    return json.dumps({
        "type": "error",
        "message": message,
    })


# ── 消息类型判断 ──────────────────────────────────


def is_hello(msg: Dict) -> bool:
    return msg.get("type") == "hello"


def is_listen_start(msg: Dict) -> bool:
    return msg.get("type") == "listen" and msg.get("state") == "start"


def is_listen_stop(msg: Dict) -> bool:
    return msg.get("type") == "listen" and msg.get("state") == "stop"
