"""
server_start.py — workingAgent 服务器独立启动入口
专为无 GUI 的 Linux 云服务器设计，不依赖 PyQt6
不影响原有 main.py 桌面版

用法：
    python3 server_start.py

首次部署：
    1. pip install -r requirements_server.txt
    2. python3 server_start.py
    3. 浏览器打开 http://服务器IP:18765 完成注册和配置
"""

import sys
import os
import json
import threading
from pathlib import Path

# ── 修复 Windows 控制台编码（防止 emoji 等 UTF-8 字符导致崩溃）──
if sys.platform == "win32":
    import io
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, 'reconfigure'):
            try:
                _stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
        elif hasattr(_stream, 'buffer'):
            try:
                _stream = io.TextIOWrapper(_stream.buffer, encoding='utf-8', errors='replace')
            except Exception:
                pass

# 把项目根目录加入路径
APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

# ── 加载配置 ──────────────────────────────────────────────────
from pathlib import Path
from desktop.config import load_config, save_config, DB_FILE, PERSONALITY_FILE

config = load_config()

print("=" * 56)
print("  workingAgent 服务器独立启动")
print(f"  数据目录: {Path(DB_FILE).parent}")
print(f"  数据库:   {DB_FILE}")
print(f"  模型:     {config.get('llm_model', '未设置')}")
print(f"  服务商:   {config.get('api_provider', 'deepseek')}")
print("=" * 56)

# ── 数据库保护层 ──────────────────────────────────────────────
try:
    from engine.db_guard import init_guard
    init_guard(DB_FILE)
    print("[数据库] 保护层已初始化")
except Exception as e:
    print(f"[数据库] 保护层初始化失败（{e}），继续启动...")

# ── 初始化核心模块（复制自 main.py，去掉 GUI 部分）─────────────
from engine.models         import PersonalityCore
from engine.memory         import MemoryStore
from engine.memory_manager import HierarchicalMemoryManager
from engine.association    import MemoryAssociationNetwork
from engine.llm_client     import create_client
from engine.executor       import BLayerExecutor
from engine.agent          import ConsciousnessAgent
from engine.user_profile   import UserProfileManager
from engine.learner        import GrowthEngine, FormedCognitionStore
from engine.auth           import AuthManager

# 人格
if Path(PERSONALITY_FILE).exists():
    with open(PERSONALITY_FILE, encoding="utf-8") as f:
        personality = PersonalityCore.from_dict(json.load(f))
else:
    personality = PersonalityCore(
        name="工作助手", worldview="把每一件活认真干完，靠谱交付"
    )
    print(f"[人格] 未找到 {PERSONALITY_FILE}，使用默认人格")

# LLM 客户端
provider = config.get("api_provider", "deepseek")
llm = create_client(
    api_key      = config.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", ""),
    provider     = provider,
    model        = config.get("llm_model", None),
    ollama_model = config.get("ollama_model", "qwen2.5:7b"),
    ollama_url   = config.get("ollama_url", "http://localhost:11434"),
)

# 语言设置
try:
    from engine.i18n import set_language
    set_language(config.get("language", "zh"))
except Exception:
    pass

# 记忆 + 关联网络
store  = MemoryStore(DB_FILE)
net    = MemoryAssociationNetwork(DB_FILE)
memory = HierarchicalMemoryManager(store, net, llm_client=llm)

executor = BLayerExecutor(
    llm_client=llm,
    confirm_callback=None,
    max_tool_steps=8,
    verbose=True
)

# 注入用户文本采集回调：B 层 ask_user_input 通过 web 弹窗采集
try:
    from hardware.web_confirm import request_user_input
    executor.ask_input = lambda prompt: request_user_input(prompt)
except Exception as e:
    print(f"[WebInput] 用户文本采集注入失败: {e}")

# 用户画像
user_profile = UserProfileManager(DB_FILE)

# 成长引擎
growth = GrowthEngine(
    db_path=DB_FILE,
    personality_file=str(PERSONALITY_FILE),
    llm_client=llm
)
cognition = FormedCognitionStore(DB_FILE)

# 身份验证管理器
auth = AuthManager(DB_FILE)

# ── 硬件桥接层（可选，服务器上通常没有硬件）──────────────────
hardware_bridge = None
run_mode = config.get("mode", "screen")

if run_mode == "hardware":
    print("[模式] 硬件模式（hardware）")
    try:
        from hardware.bridge import Bridge
        hardware_bridge = Bridge()
        print("[硬件] Bridge 桥接层已初始化")
    except Exception as e:
        print(f"[硬件] Bridge 初始化失败: {e}")
else:
    print("[模式] 屏幕模式（screen）")
    # 屏幕模式下也尝试初始化 Bridge（视觉记忆可用）
    try:
        from hardware.bridge import Bridge
        hardware_bridge = Bridge()
        print("[硬件] Bridge 桥接层已初始化（视觉记忆可用）")
    except Exception as e:
        print(f"[硬件] Bridge 未初始化（{e}），视觉记忆不可用")

# 视觉流水线独立初始化（不依赖 Bridge）
try:
    from hardware.vision_pipeline import VisionPipeline
    _vp = VisionPipeline()
    # 跳过首次分析，避免 HACamera RTSP 阻塞启动
    # _vp.run_once(force=True)
    print("[硬件] 视觉流水线已初始化（跳过首次分析）")
except Exception as e:
    print(f"[硬件] 视觉流水线初始化失败: {e}")

# ── SimLife（可选）────────────────────────────────────────────
simlife_client = None
try:
    from engine.simlife_client import SimLifeClient
    _sl = SimLifeClient()
    if _sl.is_available():
        simlife_client = _sl
        print("[SimLife] 生活状态模块已连接")
except Exception as e:
    print(f"[SimLife] 未启用（{e}）")

# SimLife 后端（后台线程）
try:
    from simlife.backend.main import app as simlife_app
    import uvicorn as _uvicorn
    def _run_simlife():
        _uvicorn.run(simlife_app, host="127.0.0.1", port=8769,
                      log_level="warning", access_log=False)
    _simlife_thread = threading.Thread(target=_run_simlife, daemon=True)
    _simlife_thread.start()
    print("[SimLife] 后端服务已在后台启动（端口 8769）")
except Exception as e:
    print(f"[SimLife] 后端自动启动失败（{e}）")

# ── 创建 Agent ────────────────────────────────────────────────
agent = ConsciousnessAgent(
    personality=personality,
    memory_manager=memory,
    b_layer_executor=executor,
    user_profile=user_profile,
    growth_engine=growth,
    cognition_store=cognition,
    auth_manager=auth,
    simlife_client=simlife_client,
    hardware_bridge=hardware_bridge,
    verbose=True
)

print(f"[就绪] 角色：{agent.personality.name}")

stats = MemoryStore(DB_FILE).get_stats()
print(f"[就绪] 记忆库：{stats['total']} 条")

# ── 启动定时任务调度器 ────────────────────────────────────────
try:
    from engine.task_scheduler import init_scheduler
    _task_scheduler = init_scheduler(on_trigger=lambda task: print(f"[定时任务] 触发: {task.get('name','?')}"))
    result = _task_scheduler.catchup_overdue()
    if result["catchup"] > 0 or result["expired"] > 0:
        print(f"[定时任务] 开机补执行: {result['catchup']}个, 过期丢弃: {result['expired']}个")
    _task_scheduler.start()
    print("[定时任务] 调度器已启动")
except Exception as e:
    print(f"[定时任务] 调度器启动失败: {e}")

# ── 手机 WebSocket 服务（外出摄像头）────────────────────────
try:
    from hardware.phone_ws_server import create_phone_server
    _phone_ws = create_phone_server(port=18766)
    print(f"[PhoneWS] 手机外出摄像头服务已创建（端口 18766）")
except Exception as e:
    print(f"[PhoneWS] 创建失败（{e}），外出摄像头不可用")

# ── 企业微信智能机器人（长连接，独立线程）──────────────────
try:
    from wecom_bot import WecomBot
    import asyncio
    import threading

    _wecom_bot = WecomBot(agent)

    def _run_wecom():
        asyncio.run(_wecom_bot.start())

    _wecom_thread = threading.Thread(target=_run_wecom, daemon=True)
    _wecom_thread.start()
    print("[WecomBot] 企业微信机器人已在后台启动")
except ImportError:
    print("[WecomBot] wecom-aibot-sdk-python 未安装，企业微信不可用")
    print("[WecomBot] 安装: pip install wecom-aibot-sdk-python")
except Exception as e:
    print(f"[WecomBot] 启动失败（{e}），企业微信不可用")

# ── 飞书智能机器人（长连接，独立线程）────────────────
try:
    from lark_bot import LarkBot
    import threading

    _lark_bot = LarkBot(agent)
    _lark_thread = threading.Thread(target=_lark_bot.start, daemon=True)
    _lark_thread.start()
    print("[LarkBot] 飞书机器人已在后台启动")
except ImportError:
    print("[LarkBot] lark-oapi 未安装，飞书不可用")
    print("[LarkBot] 安装: pip install lark-oapi>=1.6.5")
except Exception as e:
    print(f"[LarkBot] 启动失败（{e}），飞书不可用")

# ── 启动 Web 服务（主线程阻塞式，适合服务器）────────────────
import server as _server_module
import uvicorn

# 注入全局变量（和 start_server 做的事一样，但在主线程运行）
_server_module._agent        = agent
_server_module._auth_manager = agent.auth

# ── 启动 WebSocket 网页聊天服务（端口 18767）────────────────
import web_server as _ws
_ws.start_web_chat(agent, agent.auth, host="0.0.0.0", port=18767)

if __name__ == "__main__":
    port = int(os.environ.get("AGI_PORT", 18765))

    try:
        import socket
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "本机IP"

    print("=" * 56)
    print(f"  🌐 Web 服务已启动")
    print(f"  📱 网页聊天 → http://{ip}:18767")
    print(f"  📱 手机摄像头 → ws://{ip}:18766")
    print(f"  📱 手机/API → http://{ip}:{port}")
    print(f"  💻 本机网页聊天 → http://localhost:18767")
    print(f"  💻 本机 API    → http://localhost:{port}")
    print(f"  🔑 首次访问需注册账户")
    print("=" * 56)

    # 主线程直接运行 uvicorn（阻塞式，不会随主线程退出而停止）
    uvicorn.run(_server_module.app, host="0.0.0.0", port=port, log_level="info")
