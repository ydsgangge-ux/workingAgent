"""简化版服务器启动脚本 - 绕过硬件初始化问题"""
import sys, os, json
from pathlib import Path

os.environ["PYTHONIOENCODING"] = "utf-8"

# 编码修复
if sys.platform == "win32":
    import io
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, 'reconfigure'):
            try: _s.reconfigure(encoding='utf-8', errors='replace')
            except: pass

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
os.chdir(str(APP_DIR))

DB_FILE = str(APP_DIR / "work.db")
from desktop.config import load_config
config = load_config()

# 人格
PERSONALITY_FILE = r"C:\Users\Administrator\AppData\Roaming\workingAgent\personality.json"
from engine.models import PersonalityCore
if Path(PERSONALITY_FILE).exists():
    with open(PERSONALITY_FILE, encoding="utf-8") as f:
        personality = PersonalityCore.from_dict(json.load(f))
else:
    personality = PersonalityCore(name="AGI助手", worldview="保持好奇，认真生活")

print("[启动] AGI Web 服务器")

# 初始化核心模块
from engine.llm_client import create_client
llm = create_client(
    api_key=config.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", ""),
    provider=config.get("api_provider", "deepseek"),
    model=config.get("llm_model", None),
)

from engine.memory import MemoryStore
from engine.association import MemoryAssociationNetwork
store = MemoryStore(DB_FILE)
net = MemoryAssociationNetwork(DB_FILE)

from engine.memory_manager import HierarchicalMemoryManager
memory = HierarchicalMemoryManager(store, net, llm_client=llm)

from engine.executor import BLayerExecutor
executor = BLayerExecutor(llm_client=llm, confirm_callback=None, max_tool_steps=8, verbose=True)

from engine.auth import AuthManager
auth = AuthManager(DB_FILE)

from engine.agent import ConsciousnessAgent
agent = ConsciousnessAgent(personality=personality, memory_manager=memory, b_layer_executor=executor, auth_manager=auth, verbose=True)

# 启动 Web 服务
import server as sv
sv._agent = agent
sv._auth_manager = auth

# 启动 WebSocket 网页聊天服务（端口 18767）
import web_server as ws
ws.start_web_chat(agent, auth, host="0.0.0.0", port=18767)

import uvicorn
print(f"[启动] 网页聊天 → http://localhost:18767")
print(f"[启动] API 服务 → http://localhost:18765")
uvicorn.run(sv.app, host="0.0.0.0", port=18765, log_level="info")