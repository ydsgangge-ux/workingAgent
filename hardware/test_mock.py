"""
三条 Mock 测试路径 — 逐条验证，零硬件依赖
=============================================

python -m hardware.test_mock 1    # 测试一：视觉记忆写入
python -m hardware.test_mock 2    # 测试二：传感器流输出
python -m hardware.test_mock 3    # 测试三：语音交互链路
python -m hardware.test_mock all  # 全跑
"""
import sys
import time


# ── 测试一：视觉记忆写入 ──────────────────────────

def test_vision_memory():
    """
    1. 拍一张照片（本地图片 / 摄像头）
    2. 多模态模型分析
    3. 写入 SQLite
    4. 查出来看看
    """
    print("=" * 60)
    print("测试一：视觉记忆写入")
    print("=" * 60)

    from hardware.vision_pipeline import VisionPipeline

    pipe = VisionPipeline()
    mem = pipe.run_once()

    if mem is None:
        print("[FAIL] 流水线返回空")
        return False

    print(f"\n  分析结果:")
    print(f"    描述:        {mem.description}")
    print(f"    物体:        {mem.objects}")
    print(f"    人物:        {mem.persons}")
    print(f"    置信度:      {mem.vision_confidence}")
    print(f"    事件:        {mem.event_summary}")

    # 检查存储
    count = pipe.store.count()
    print(f"\n  SQLite 中视觉记忆总数: {count}")

    if mem.vision_confidence >= 0.6 and count > 0:
        # 测试检索
        results = pipe.store.search(mem.description[:4])
        print(f"  检索 '{mem.description[:4]}': 命中 {len(results)} 条")
        if results:
            print(f"  首条 importance: {results[0]['importance']}")

    print("\n[PASS] 测试一完成\n")
    return True


# ── 测试二：传感器流 ──────────────────────────────

def test_sensor_stream(duration: int = 15):
    """
    1. 启动假传感器流
    2. 收集几组数据观察输出格式
    """
    print("=" * 60)
    print(f"测试二：传感器流（监控 {duration} 秒）")
    print("=" * 60)

    from hardware.mock_sensors import MockSensorStream
    from hardware.bridge import Bridge

    collected = []

    def on_packet(packet):
        text = Bridge._format_sensor_text(packet)
        collected.append(text)
        print(f"  [{packet.timestamp}] {text}")

    stream = MockSensorStream(interval=5.0)  # 5秒一组便于观察
    stream.on_data(on_packet)
    stream.start()

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()

    print(f"\n  共收到 {len(collected)} 组传感器数据")
    if collected:
        print(f"[PASS] 测试二完成\n")
        return True
    print("[FAIL] 未收到任何传感器数据\n")
    return False


# ── 测试三：语音交互 ──────────────────────────────

def test_voice():
    """
    1. 使用电脑麦克风录音
    2. STT 识别
    3. Agent 处理（如果有回调）
    4. TTS 输出
    """
    print("=" * 60)
    print("测试三：语音交互链路")
    print("=" * 60)

    try:
        from engine.stt_engine import STTEngine, record_audio
    except ImportError:
        print("[SKIP] 未找到 stt_engine，请确认项目已安装 stt 依赖")
        return False

    try:
        from engine.tts_engine import TTSEngine
    except ImportError:
        print("[SKIP] 未找到 tts_engine，请确认项目已安装 tts 依赖")
        return False

    # STT
    stt = STTEngine()
    print("  录音 5 秒...")
    audio_path = record_audio(duration=5.0)
    if not audio_path:
        print("[FAIL] 录音失败")
        return False

    result = stt.recognize_file(audio_path)
    text = result.get("text", "")
    if not text:
        print("[FAIL] 语音识别为空")
        return False
    print(f"  识别结果: {text}")

    # Agent 处理（如果有对接）
    from hardware.bridge import Bridge
    bridge = Bridge()
    bridge.on_sensor_data = lambda p: None  # 避免传感器回调干扰

    # TTS
    tts = TTSEngine()
    tts.speak(f"你说了: {text}", on_done=lambda: print("  TTS 播放完成"))

    print("[PASS] 测试三完成\n")
    return True


# ── 入口 ──────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:] if len(sys.argv) > 1 else ["1"]

    if "all" in args:
        test_vision_memory()
        test_sensor_stream(duration=20)
        test_voice()
    elif "1" in args:
        test_vision_memory()
    elif "2" in args:
        test_sensor_stream()
    elif "3" in args:
        test_voice()
    else:
        print("用法: python -m hardware.test_mock [1|2|3|all]")
