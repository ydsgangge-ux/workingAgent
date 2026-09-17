"""
视觉记忆流水线 — 图片 → 多模态模型 → 统一模板 → SQLite
============================================================
优先从 HA 摄像头拉取真实帧，HA 不可用时降级到本地图片。
模型从第一天就用真实的。

变化检测：帧差法，无变化不写入，节省 API 调用和存储。
自动分类：根据画面内容自动判断 memory_type（space/person/event）。
"""
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Optional, Literal

import cv2
import numpy as np

from .memory_schema import VisualMemory
from .visual_memory_store import VisualMemoryStore
from .ha_camera import HACamera
from .mock_camera import MockCamera
from .prompts import (
    VISION_PROMPT_STANDARD,
    VISION_PROMPT_SPACE,
    VISION_PROMPT_PERSON,
    FALLBACK_PROMPT,
)


_PROMPT_MAP = {
    "event": VISION_PROMPT_STANDARD,
    "space": VISION_PROMPT_SPACE,
    "person": VISION_PROMPT_PERSON,
    "interest": VISION_PROMPT_STANDARD,
}

_CHANGE_THRESHOLD = 15.0
_SPACE_INTERVAL = 3600


class VisionPipeline:
    """一次拍照→变化检测→分析→分类→存库的完整流水线"""

    def __init__(self, phone_sensor=None):
        self.ha_camera = HACamera()
        self.mock_camera = MockCamera()
        self.store = VisualMemoryStore()
        self._vision_client = None
        self._prev_frame_gray: Optional[np.ndarray] = None
        self._last_space_time: float = 0
        self._phone_sensor = phone_sensor  # PhoneSensorClient，按需获取 GPS
        self._image_mgr = None  # 延迟初始化 ImageManager
        self._last_img_bytes: Optional[bytes] = None  # 保留当前帧用于存图

    # ── 主入口 ────────────────────────────────────────

    def run_once(
        self,
        memory_type: Literal["event", "space", "person", "interest"] = "event",
        force: bool = False,
    ) -> Optional[VisualMemory]:
        """
        执行一轮完整流水线：
        1. 拍摄/读取图片
        2. 变化检测（帧差法），无变化则跳过
        3. 自动分类 memory_type（如未指定）
        4. 根据 memory_type 选择对应 prompt 调用多模态 API
        5. 人脸识别联动
        6. 按置信度决定写入记忆还是仅日志

        force=True: 跳过变化检测，强制分析（用于用户主动触发）
        """
        img_bytes = self._capture()
        if not img_bytes:
            print("[VisionPipeline] 无图像输入")
            return None

        # 保留当前帧用于后续存图
        self._last_img_bytes = img_bytes

        # ── 变化检测 ──
        if not force:
            diff_score = self._compute_change(img_bytes)
            if diff_score is not None and diff_score < _CHANGE_THRESHOLD:
                _log_event(f"[变化检测] 无显著变化 (diff={diff_score:.1f})，跳过")
                return None

        # ── 自动分类 ──
        if memory_type == "event":
            memory_type = self._classify_type(img_bytes)

        result = self._analyze(img_bytes, memory_type)
        if not result:
            return None

        result.memory_type = memory_type

        if memory_type == "space":
            result.importance = max(result.importance, 0.9)
            self._last_space_time = time.time()

        self._detect_persons(img_bytes, result)

        if result.persons and memory_type != "person":
            memory_type = "person"
            result.memory_type = "person"
            result.importance = max(result.importance, 0.8)

        # ── 注入 GPS（在低置信度提前返回之前执行）──
        self._inject_gps(result)

        if result.vision_confidence < 0.6:
            _log_event(
                f"[低置信度 {result.vision_confidence:.2f}] {result.description}"
            )
            return result

        # ── 判断是否存图 ──
        self._maybe_save_image(img_bytes, result)

        self.store.insert(result)
        _log_event(f"[视觉记忆已写入] ({memory_type}) {result.description}")
        return result

    # ── GPS 注入 ──────────────────────────────────────

    def _inject_gps(self, result: VisualMemory):
        """
        从手机传感器获取 GPS，解析为语义位置，注入视觉记忆。

        获取优先级：
        1. WebSocket（phone_ws_server.get_sensor_data()）— 远程手机
        2. HTTP 直连（PhoneSensorClient.get_gps()）— 局域网手机

        位置解析流程：
            原始 GPS → match_landmark（个人地标库，最高优先级）
                      → reverse_geocode（高德 API，兜底）
                      → 语义文本 + 置信度
            结果写入 VisualMemory 的 location 字段，并融入 description。
        """
        gps = self._get_gps_ws() or self._get_gps_http()
        if not gps:
            return

        try:
            result.gps = {"lat": gps["lat"], "lng": gps["lng"]}
            result.gps_accuracy = gps.get("accuracy")
            result.scene_type = "outdoor"

            # 调用 location_resolver 解析语义位置
            from .location_resolver import resolve_location

            location = resolve_location(gps["lat"], gps["lng"])
            result.landmark_ref = location["landmark_ref"]
            result.location_confidence = location["location_confidence"]

            # 将位置描述融入 description，让检索更自然
            location_text = location["location_text"]
            if location_text and location_text != "未知位置":
                desc = result.description or ""
                result.description = f"{location_text}：{desc}" if desc else location_text

        except Exception:
            pass

    def _get_gps_ws(self) -> dict | None:
        """通过 WebSocket 获取手机传感器 GPS（适用于远程手机）"""
        try:
            from .phone_ws_server import get_phone_server
            import asyncio
            import concurrent.futures

            server = get_phone_server()
            if server is None:
                print("[VisionPipeline] WS GPS: get_phone_server() 返回 None")
                return None
            if not server.is_connected():
                print("[VisionPipeline] WS GPS: 手机未连接")
                return None
            if server._loop is None:
                print("[VisionPipeline] WS GPS: server._loop 为 None")
                return None

            print("[VisionPipeline] WS GPS: 正在发送传感器请求...")
            future = asyncio.run_coroutine_threadsafe(
                server.get_sensor_data(), server._loop
            )
            try:
                sensor_data = future.result(timeout=10.0)
            except concurrent.futures.TimeoutError:
                print("[VisionPipeline] WS GPS: 请求超时 (10s)")
                return None
            except Exception as e:
                print(f"[VisionPipeline] WS GPS: future 异常: {e}")
                return None
            if not sensor_data:
                return None

            gps = sensor_data.get("gps", {})
            if gps.get("lat"):
                print(f"[VisionPipeline] WebSocket GPS: ({gps['lat']}, {gps['lng']})")
                return {
                    "lat": gps["lat"],
                    "lng": gps["lng"],
                    "accuracy": gps.get("accuracy", 0),
                }
        except Exception as e:
            print(f"[VisionPipeline] WebSocket GPS 获取失败: {e}")
        return None

    def _get_gps_http(self) -> dict | None:
        """通过 HTTP 直连 IP Webcam 获取 GPS（适用于局域网手机）"""
        if not self._phone_sensor:
            return None
        try:
            gps = self._phone_sensor.get_gps()
            if gps:
                print(f"[VisionPipeline] HTTP GPS: ({gps['lat']}, {gps['lng']})")
            return gps
        except Exception:
            return None

    # ── 图片保存判断 ──────────────────────────────────

    def _maybe_save_image(self, img_bytes: bytes, result: VisualMemory):
        """
        根据存图策略判断是否保存图片：
        - person: 检测到人脸就存
        - space: 室内空间，每空间最多4张
        - outdoor: 按距离分级，坐标半径内无图就存
        - event: importance >= 0.8 时存
        """
        try:
            from .image_manager import ImageManager
            if self._image_mgr is None:
                self._image_mgr = ImageManager()

            # 统计同空间已有图片数
            existing_count = 0
            if result.memory_type == "space":
                # 用描述关键词统计
                keyword = result.description[:10] if result.description else ""
                existing_count = self.store.count_space_images(keyword)

            should, category = self._image_mgr.should_save(
                memory_type=result.memory_type,
                scene_type=result.scene_type,
                importance=result.importance,
                persons=result.persons,
                gps=result.gps,
                description=result.description,
                existing_count=existing_count,
            )

            if not should:
                return

            # 室外：检查坐标半径内是否已有图
            if category == "outdoor" and result.gps:
                radius = self._image_mgr.get_outdoor_radius(result.description)
                nearby = self.store.query_nearby(
                    lat=result.gps["lat"],
                    lng=result.gps["lng"],
                    radius_meters=radius,
                    limit=1,
                )
                if nearby:
                    _log_event(f"[存图] 室外坐标半径{radius}m内已有图，跳过")
                    return

            # 生成标签
            label = ""
            if category == "person" and result.persons:
                label = result.persons[0].get("name", result.persons[0].get("id", "unknown"))
            elif category == "space":
                label = result.description[:15]
            elif category == "outdoor":
                label = "outdoor"

            rel_path = self._image_mgr.save_image(
                img_bytes=img_bytes,
                category=category,
                label=label,
                gps=result.gps,
            )

            if rel_path:
                result.image_path = rel_path
                result.image_category = category
                _log_event(f"[存图] 已保存: {category}/{rel_path.split('/')[-1]}")

        except Exception as e:
            print(f"[VisionPipeline] 存图失败: {e}")

    # ── 变化检测（帧差法） ──────────────────────────

    def _compute_change(self, img_bytes: bytes) -> Optional[float]:
        """
        计算当前帧与上一帧的差异分数。
        返回 None 表示首帧（无对比基准）。
        """
        try:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return None
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if self._prev_frame_gray is None:
                self._prev_frame_gray = gray
                return None

            diff = cv2.absdiff(self._prev_frame_gray, gray)
            score = float(np.mean(diff))
            self._prev_frame_gray = gray
            return score
        except Exception:
            return None

    # ── 自动分类 memory_type ────────────────────────

    def _classify_type(self, img_bytes: bytes) -> str:
        """
        根据画面内容自动判断记忆类型：
        - 检测到人脸 → person
        - 距上次 space 记录超过1小时 → space
        - 其他 → event
        """
        try:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                from engine.face_recognition_engine import detect_faces
                faces = detect_faces(rgb)
                if faces:
                    return "person"
        except Exception:
            pass

        if time.time() - self._last_space_time > _SPACE_INTERVAL:
            return "space"

        return "event"

    # ── 拍照：仅使用真实摄像头 ─────────────────────

    def _capture(self) -> Optional[bytes]:
        """
        从真实摄像头拉取帧。
        摄像头不可用时返回 None，不降级到 Mock（避免写入虚假视觉记忆）。
        """
        try:
            # HACamera.capture() 已含 WebSocket → IP Webcam → RTSP → HA API 优先级
            # 即使 available=False 也会尝试 WebSocket
            img = self.ha_camera.capture()
            if img:
                return img
        except Exception as e:
            print(f"[VisionPipeline] 摄像头抓帧异常: {e}")

        print("[VisionPipeline] 无可用摄像头，跳过本轮视觉记忆")
        return None

    # ── 调用真实多模态 API ───────────────────────────

    def _analyze(
        self,
        img_bytes: bytes,
        memory_type: str = "event",
    ) -> Optional[VisualMemory]:
        """
        调用真实多模态模型分析图片。
        先用专用 prompt（期望 JSON），
        若 JSON 解析失败则自动降级到 FALLBACK_PROMPT（纯文本）。
        """
        suffix = ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(img_bytes)
            tmp_path = f.name

        try:
            client = self._get_vision_client()
            if not client:
                print("[VisionPipeline] 无可用多模态客户端")
                return None

            # 第1轮：专用 prompt（期望 JSON 格式）
            prompt = _PROMPT_MAP.get(memory_type, VISION_PROMPT_STANDARD)
            resp = client.analyze(file_path=tmp_path, question=prompt)
            if resp.get("ok"):
                mem = self._parse_response(resp["description"], prefer_json=True)
                if mem is not None:
                    return mem  # JSON 解析成功
                # JSON 解析失败 → 第2轮降级

            # 第2轮：降级到简单 prompt（纯文本模式）
            print(f"[VisionPipeline] 降级到 FALLBACK_PROMPT")
            resp = client.analyze(file_path=tmp_path, question=FALLBACK_PROMPT)
            if not resp.get("ok"):
                print(f"[VisionPipeline] 降级也失败: {resp.get('error', '')}")
                return None

            return self._parse_response(resp["description"], prefer_json=False)

        except Exception as e:
            print(f"[VisionPipeline] 分析异常: {e}")
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _get_vision_client(self):
        if self._vision_client is None:
            from engine.vision_client import create_vision_client

            self._vision_client = create_vision_client()
        return self._vision_client

    # ── 解析 LLM 返回 ───────────────────────────────

    @staticmethod
    def _parse_response(raw_text: str, prefer_json: bool = True) -> Optional[VisualMemory]:
        """
        从 LLM 返回的文本构建 VisualMemory。

        prefer_json=True（第一轮专用 prompt）：
            只接受 JSON 格式，失败返回 None → 触发上层降级。
        prefer_json=False（FALLBACK_PROMPT）：
            全部作为纯文本处理。
        """
        # 清理 markdown 代码块包裹
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            # 去掉 ```json 和 ```
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        # 尝试 JSON
        json_match = re.search(r'\{[\s\S]*\}', cleaned)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                # 兼容不同字段名
                description = (
                    str(data.get("description", ""))
                    or str(data.get("desc", ""))
                    or str(data.get("scene_description", ""))
                )
                objects = data.get("objects") or data.get("items") or []
                persons = data.get("persons") or data.get("people") or []
                event_summary = (
                    str(data.get("event_summary", ""))
                    or str(data.get("event", ""))
                    or str(data.get("summary", ""))
                )
                # persons 字段兼容：如果 LLM 返回的 person 没有 id/name，补上
                normalized_persons = []
                for p in persons:
                    if isinstance(p, dict):
                        if "id" not in p:
                            p["id"] = p.get("name", "unknown")
                        if "action" not in p:
                            p["action"] = "在场"
                        normalized_persons.append(p)
                    elif isinstance(p, str):
                        normalized_persons.append({"id": p, "name": p, "action": "在场"})

                return VisualMemory(
                    description=description,
                    objects=objects if isinstance(objects, list) else [],
                    persons=normalized_persons,
                    event_summary=event_summary,
                    subjective_note=str(data.get("subjective_note", "")),
                    vision_confidence=float(data.get("vision_confidence", 0.0) or 0.0),
                )
            except Exception:
                pass

        # prefer_json=True 且无有效 JSON → 返回 None 触发降级
        if prefer_json:
            return None

        # prefer_json=False：纯文本降级
        desc = cleaned.strip()
        desc = re.sub(r'^[：:]\s*', "", desc)

        conf = 0.5
        conf_match = re.search(r"(?:置信度|confidence)\s*[:：]?\s*([0-9.]+)", desc)
        if conf_match:
            try:
                conf = float(conf_match.group(1))
                desc = desc.replace(conf_match.group(0), "").strip()
            except Exception:
                pass
        elif len(desc) > 10:
            conf = 0.85

        return VisualMemory(
            description=desc[:200],
            vision_confidence=min(conf, 1.0),
        )

    # ── 人脸识别联动 ────────────────────────────────

    def _detect_persons(self, img_bytes: bytes, result: VisualMemory):
        """
        对抓到的帧做人脸检测+识别，
        将结果写入 result.persons 字段。
        """
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            from engine.face_recognition_engine import detect_faces, can_identify, FaceDatabase
            faces = detect_faces(rgb)
            if not faces:
                return

            persons = []
            if can_identify():
                try:
                    from engine.face_recognition_engine import get_face_db_path
                    face_db = FaceDatabase(get_face_db_path())
                    id_result = face_db.identify(rgb)
                    if id_result.get("identified"):
                        persons.append({
                            "id": id_result["user_id"],
                            "name": id_result.get("label", id_result["user_id"]),
                            "action": "在场",
                            "confidence": id_result.get("confidence", 0),
                        })
                except Exception:
                    pass

            if not persons:
                for i, face in enumerate(faces):
                    persons.append({
                        "id": f"unknown_{i+1}",
                        "name": f"未识别人{i+1}",
                        "action": "在场",
                        "confidence": face.get("confidence", 0),
                    })

            result.persons = persons
            names = [p["name"] for p in persons]
            _log_event(f"[人脸识别] 检测到 {len(faces)} 张脸: {', '.join(names)}")
        except ImportError:
            pass
        except Exception as e:
            pass


# ── 事件日志 ───────────────────────────────────────
_event_log = []


def _log_event(text: str):
    _event_log.append(text)
    print(f"[Hardware] {text}")
