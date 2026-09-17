"""
Opus 音频编解码
小智使用 Opus 格式传输音频，需要与 PCM 互转才能送 STT/TTS

Opus 参数：
    采样率：16000 Hz
    声道：单声道
    帧时长：60ms（每帧 960 samples）
"""
import struct
from typing import List

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_DURATION_MS = 60
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 960 samples


class OpusDecoder:
    """Opus 二进制流 → PCM bytes（16bit, 16000Hz, 单声道）"""

    def __init__(self):
        self._decoder = None

    def _get_decoder(self):
        if self._decoder is None:
            import opuslib
            self._decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
        return self._decoder

    def decode(self, opus_data: bytes) -> bytes:
        """将一段 Opus 数据解码为 PCM bytes"""
        if not opus_data:
            return b""

        decoder = self._get_decoder()
        pcm_frames = bytearray()

        # Opus 帧不定长，尝试按帧边界逐个解码
        offset = 0
        while offset < len(opus_data):
            # 先尝试用整段剩余数据解码
            chunk = opus_data[offset:]
            try:
                pcm = decoder.decode(chunk, FRAME_SIZE)
                pcm_frames.extend(pcm)
                offset = len(opus_data)  # 成功则全部消耗
            except opuslib.OpusError:
                # 单帧解码失败，尝试逐字节缩小窗口
                for end in range(len(chunk), 0, -1):
                    try:
                        pcm = decoder.decode(chunk[:end], FRAME_SIZE)
                        pcm_frames.extend(pcm)
                        offset += end
                        break
                    except opuslib.OpusError:
                        continue
                else:
                    # 实在无法解码，跳过当前字节
                    offset += 1
        return bytes(pcm_frames)

    def decode_frame(self, frame: bytes) -> bytes:
        """解码单个 Opus 帧为 PCM bytes"""
        decoder = self._get_decoder()
        try:
            return decoder.decode(frame, FRAME_SIZE)
        except Exception as e:
            print(f"[Codec] 单帧解码失败: {e}")
            return b""


class OpusEncoder:
    """PCM bytes → Opus 帧列表"""

    def __init__(self):
        self._encoder = None

    def _get_encoder(self):
        if self._encoder is None:
            import opuslib
            self._encoder = opuslib.Encoder(
                SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_AUDIO
            )
        return self._encoder

    def encode(self, pcm_data: bytes) -> List[bytes]:
        """将 PCM bytes 编码为 Opus 帧列表（按 60ms 分帧）"""
        if not pcm_data:
            return []

        encoder = self._get_encoder()
        frames = []
        frame_bytes = FRAME_SIZE * 2  # 16bit = 2 bytes/sample

        for i in range(0, len(pcm_data), frame_bytes):
            chunk = pcm_data[i : i + frame_bytes]
            if len(chunk) < frame_bytes:
                # 最后一帧不足时补零对齐
                chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
            try:
                encoded = encoder.encode(chunk, FRAME_SIZE)
                frames.append(encoded)
            except Exception as e:
                print(f"[Codec] 编码错误: {e}")
                continue

        return frames

    def encode_frame(self, pcm_frame: bytes) -> bytes:
        """编码单帧 PCM 为 Opus bytes"""
        encoder = self._get_encoder()
        if len(pcm_frame) < FRAME_SIZE * 2:
            pcm_frame = pcm_frame + b"\x00" * (FRAME_SIZE * 2 - len(pcm_frame))
        try:
            return encoder.encode(pcm_frame, FRAME_SIZE)
        except Exception as e:
            print(f"[Codec] 单帧编码失败: {e}")
            return b""
