"""
假视觉输入 — 替代机器狗摄像头
================================
第一阶段：读本地图片文件（mock_images/ 目录）
第二阶段：切到电脑摄像头（无图片文件时降级）
"""
import cv2
import random
from pathlib import Path
from typing import Optional


class MockCamera:
    """Mock 摄像头：优先读本地图片，无图则用电脑摄像头"""

    def __init__(self, image_dir: str = "mock_images"):
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(exist_ok=True)
        self._cap: Optional[cv2.VideoCapture] = None

    def capture(self) -> Optional[bytes]:
        """
        返回 JPEG bytes。
        有图片文件时随机选一张，没有时用电脑摄像头。
        """
        files = (
            list(self.image_dir.glob("*.jpg"))
            + list(self.image_dir.glob("*.png"))
            + list(self.image_dir.glob("*.jpeg"))
        )
        if files:
            img_path = random.choice(files)
            return img_path.read_bytes()

        return self._capture_webcam()

    def _capture_webcam(self) -> Optional[bytes]:
        if self._cap is None:
            self._cap = cv2.VideoCapture(0)
            if not self._cap.isOpened():
                print("[MockCamera] 无可用的摄像头")
                return None
        ret, frame = self._cap.read()
        if not ret:
            return None
        _, buf = cv2.imencode(".jpg", frame)
        return buf.tobytes()

    def release(self):
        if self._cap:
            self._cap.release()
            self._cap = None
