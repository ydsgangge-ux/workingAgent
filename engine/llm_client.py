"""
LLM 客户端
支持：DeepSeek / OpenAI / Anthropic Claude / Google Gemini / Groq / Ollama / Mock

所有客户端统一接口：
  generate(prompt, system, max_tokens, temperature, messages,
           thinking, thinking_effort, thinking_budget) -> str

思考模式（Thinking Mode）：
  - thinking:         bool  — True=开启, False=关闭, None=跟随模型默认
  - thinking_effort:  str   — "low"/"medium"/"high"/"max"（深度控制）
  - thinking_budget:  int   — 思考 token 预算（Claude/Gemini/通义/智谱用）
  各子类通过 _build_thinking_params() 翻译成厂商专属格式
"""

import json
import urllib.request
import urllib.error
from typing import List, Dict, Optional


# ── 通用 OpenAI 格式客户端（DeepSeek/OpenAI/Groq/任何兼容接口）──────────
class OpenAICompatClient:
    """兼容 OpenAI Chat Completions API 格式的通用客户端"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        self.model    = model

    # ── 思考模式翻译（子类按需覆盖）────────────────────────────────
    def _build_thinking_params(self, thinking: Optional[bool],
                               thinking_effort: Optional[str],
                               thinking_budget: Optional[int]) -> Dict:
        """将统一的思考参数翻译成厂商格式。基类默认不支持。"""
        return {}

    # ── generate ──────────────────────────────────────────────────
    def generate(self, prompt: str, system: str = None,
                 max_tokens: int = 1000, temperature: float = 0.7,
                 messages: List[Dict] = None, model: str = None,
                 thinking: Optional[bool] = None,
                 thinking_effort: Optional[str] = None,
                 thinking_budget: Optional[int] = None) -> str:
        if messages is None:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
        else:
            msgs = messages

        use_model = model or self.model
        body = {
            "model": use_model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": temperature
        }

        # 合并思考参数
        extra = self._build_thinking_params(thinking, thinking_effort, thinking_budget)
        if extra:
            body.update(extra)
            # 思考模式下大多数厂商不支持 temperature
            body.pop("temperature", None)

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {self.api_key}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err[:300]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")


# ── DeepSeek（V4 默认开启思考）─────────────────────────────────────────
class DeepSeekClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        super().__init__(api_key,
                         "https://api.deepseek.com/v1",
                         model)

    def _build_thinking_params(self, thinking, thinking_effort, thinking_budget):
        if thinking is False:
            return {"thinking": {"type": "disabled"}}
        if thinking is True:
            effort = thinking_effort or "high"
            return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
        return {}  # None = 模型默认（V4 默认开启）


# ── OpenAI（o-series 支持 reasoning_effort）───────────────────────────
class OpenAIClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        super().__init__(api_key,
                         "https://api.openai.com/v1",
                         model)

    def _build_thinking_params(self, thinking, thinking_effort, thinking_budget):
        if thinking is True and thinking_effort:
            return {"reasoning_effort": thinking_effort}
        return {}


# ── Groq（免费额度充足，速度极快，无思考模式）─────────────────────────
class GroqClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        super().__init__(api_key,
                         "https://api.groq.com/openai/v1",
                         model)


# ── 通义千问 (Qwen / 阿里云 DashScope) ────────────────────────────────
class QwenClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "qwen-plus"):
        super().__init__(api_key,
                         "https://dashscope.aliyuncs.com/compatible-mode/v1",
                         model)

    def _build_thinking_params(self, thinking, thinking_effort, thinking_budget):
        params = {}
        if thinking is not None:
            params["enable_thinking"] = thinking
        if thinking_budget:
            params["thinking_budget"] = thinking_budget
        return params


# ── 智谱 GLM (ZhipuAI) ───────────────────────────────────────────────
class ZhipuClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "glm-4-flash"):
        super().__init__(api_key,
                         "https://open.bigmodel.cn/api/paas/v4",
                         model)

    def _build_thinking_params(self, thinking, thinking_effort, thinking_budget):
        params = {}
        if thinking is not None:
            params["enable_thinking"] = thinking
        if thinking_budget:
            params["thinking_budget"] = thinking_budget
        return params


# ── 豆包 (Doubao / 字节跳动 火山引擎) ─────────────────────────────────
class DoubaoClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "doubao-pro-32k"):
        super().__init__(api_key,
                         "https://ark.cn-beijing.volces.com/api/v3",
                         model)

    def _build_thinking_params(self, thinking, thinking_effort, thinking_budget):
        if thinking is False:
            return {"thinking": {"type": "disabled"}}
        if thinking is True:
            return {"thinking": {"type": "enabled"}}
        return {}


# ── Kimi (Moonshot / 月之暗面) ────────────────────────────────────────
class KimiClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "moonshot-v1-8k"):
        super().__init__(api_key,
                         "https://api.moonshot.cn/v1",
                         model)

    def _build_thinking_params(self, thinking, thinking_effort, thinking_budget):
        if thinking is False:
            return {"thinking": {"type": "disabled"}}
        if thinking is True:
            return {"thinking": {"type": "enabled"}}
        return {}  # None = 模型默认（K2.5 默认开启）


# ── 文心一言 (Baidu ERNIE) ─────────────────────────────────────────────
class BaiduClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "ernie-speed-128k"):
        super().__init__(api_key,
                         "https://qianfan.baidubce.com/v2",
                         model)


# ── 讯飞星火 (SparkDesk) ─────────────────────────────────────────────
class SparkClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "generalv3.5"):
        super().__init__(api_key,
                         "https://spark-api-open.xf-yun.com/v1",
                         model)


# ── 小米 MiMo（OpenAI 兼容）─────────────────────────────────────────
class MiMoClient(OpenAICompatClient):
    def __init__(self, api_key: str, model: str = "mimo-v2.5"):
        super().__init__(api_key,
                         "https://api.xiaomimimo.com/v1",
                         model)


# ── Anthropic Claude ──────────────────────────────────────────────────
class ClaudeClient:
    BASE_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str = "claude-3-5-haiku-20241022"):
        self.api_key = api_key
        self.model   = model

    def generate(self, prompt: str, system: str = None,
                 max_tokens: int = 1000, temperature: float = 0.7,
                 messages: List[Dict] = None,
                 thinking: Optional[bool] = None,
                 thinking_effort: Optional[str] = None,
                 thinking_budget: Optional[int] = None) -> str:
        if messages is None:
            msgs = [{"role": "user", "content": prompt}]
        else:
            # 过滤掉 system 角色（Claude 单独传）
            msgs = [m for m in messages if m.get("role") != "system"]
            if not msgs:
                msgs = [{"role": "user", "content": prompt}]

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": msgs
        }
        if system:
            body["system"] = system

        # Claude 思考模式
        if thinking is True:
            body["thinking"] = {"type": "enabled"}
            if thinking_budget:
                body["thinking"]["budget_tokens"] = thinking_budget
            # 思考模式不支持 temperature
            body.pop("temperature", None)
        elif thinking is False:
            # 显式关闭（Claude 默认就是关闭的，但保持一致性）
            pass

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.BASE_URL, data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
                # Claude 思考模式：优先返回 text block，忽略 thinking block
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"]
                return data["content"][0]["text"]
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Claude HTTP {e.code}: {err[:300]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")


# ── Google Gemini ─────────────────────────────────────────────────────
class GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-1.5-flash"):
        self.api_key = api_key
        self.model   = model

    def generate(self, prompt: str, system: str = None,
                 max_tokens: int = 1000, temperature: float = 0.7,
                 messages: List[Dict] = None,
                 thinking: Optional[bool] = None,
                 thinking_effort: Optional[str] = None,
                 thinking_budget: Optional[int] = None) -> str:
        # 构建 Gemini 格式
        contents = []
        if messages:
            for m in messages:
                role = "user" if m["role"] == "user" else "model"
                contents.append({"role": role,
                                  "parts": [{"text": m["content"]}]})
        else:
            if system:
                contents.append({"role": "user",
                                  "parts": [{"text": f"[System]: {system}\n\n{prompt}"}]})
            else:
                contents.append({"role": "user", "parts": [{"text": prompt}]})

        gen_config = {
            "maxOutputTokens": max_tokens,
        }
        if thinking is True:
            gen_config["thinkingConfig"] = {
                "thinkingBudget": thinking_budget or 8192
            }
        else:
            gen_config["temperature"] = temperature

        body = {
            "contents": contents,
            "generationConfig": gen_config
        }
        if system and not messages:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        url = (f"{self.BASE_URL}/{self.model}:generateContent"
               f"?key={self.api_key}")
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini HTTP {e.code}: {err[:300]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")


# ── Ollama 本地 ───────────────────────────────────────────────────────
class OllamaClient:
    def __init__(self, model: str = "qwen2.5:7b",
                 base_url: str = "http://localhost:11434"):
        self.model    = model
        self.base_url = base_url.rstrip("/")
        self.api_key  = "ollama"

    def generate(self, prompt: str, system: str = None,
                 max_tokens: int = 1000, temperature: float = 0.7,
                 messages: List[Dict] = None, model: str = None,
                 thinking: Optional[bool] = None,
                 thinking_effort: Optional[str] = None,
                 thinking_budget: Optional[int] = None) -> str:
        if messages is None:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
        else:
            msgs = messages

        payload = json.dumps({
            "model": model or self.model, "messages": msgs, "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature}
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())["message"]["content"]
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Ollama connection failed ({self.base_url}): {e.reason}\n"
                "Please run: ollama serve"
            )

    def list_models(self) -> List[str]:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                return [m["name"] for m in json.loads(r.read()).get("models", [])]
        except Exception:
            return []

    def is_running(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3)
            return True
        except Exception:
            return False


# ── Mock（无 API 时降级）─────────────────────────────────────────────
class MockClient:
    def generate(self, prompt: str, system: str = None,
                 max_tokens: int = 1000, temperature: float = 0.7,
                 messages: List[Dict] = None, model: str = None,
                 thinking: Optional[bool] = None,
                 thinking_effort: Optional[str] = None,
                 thinking_budget: Optional[int] = None) -> str:
        if any(k in prompt for k in ["emotion", "needs_deep_memory",
                                      "情绪类型", "初步感受", "感知结果"]):
            return json.dumps({
                "emotion": {"primary": "curious", "secondary": None,
                            "intensity": 0.6, "valence": 0.4},
                "initial_thoughts": "Interesting question.",
                "topic_tags": ["conversation"], "needs_deep_memory": True,
                "task_type": "chat", "task_description": ""
            }, ensure_ascii=False)
        if any(k in prompt for k in ["inner_reasoning", "storage_decision",
                                      "need_tools", "response_intent"]):
            return json.dumps({
                "inner_reasoning": "Need to respond thoughtfully.",
                "response_intent": "Give a helpful response",
                "response_tone": "natural",
                "need_tools": False, "tool_task": "",
                "storage_decision": {"should_store": False, "importance": 0.3,
                    "modality": "semantic", "what_to_remember": "", "reason": "Mock"}
            }, ensure_ascii=False)
        return ("(Mock mode) Please configure an API key in Settings.\n"
                "Supported: DeepSeek / OpenAI / Claude / Gemini / Groq / "
                "Qwen / Zhipu / Doubao / Kimi / Baidu / SparkDesk / Ollama")


# ── 工厂函数 ─────────────────────────────────────────────────────────
PROVIDER_INFO = {
    "deepseek": {
        "name": "DeepSeek",
        "url":  "https://platform.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "thinking_capable": True,
    },
    "openai": {
        "name": "OpenAI",
        "url":  "https://platform.openai.com",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo",
                   "o3-mini", "o4-mini"],
        "default_model": "gpt-4o-mini",
        "thinking_capable": True,
    },
    "claude": {
        "name": "Anthropic Claude",
        "url":  "https://console.anthropic.com",
        "models": ["claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022",
                   "claude-3-opus-20240229"],
        "default_model": "claude-3-5-haiku-20241022",
        "thinking_capable": True,
    },
    "gemini": {
        "name": "Google Gemini",
        "url":  "https://aistudio.google.com",
        "models": ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash-exp",
                   "gemini-2.5-flash-preview-05-20"],
        "default_model": "gemini-1.5-flash",
        "thinking_capable": True,
    },
    "groq": {
        "name": "Groq (Free tier available)",
        "url":  "https://console.groq.com",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                   "mixtral-8x7b-32768", "gemma2-9b-it"],
        "default_model": "llama-3.3-70b-versatile",
        "thinking_capable": False,
    },
    # ── 国产大模型 ──
    "qwen": {
        "name": "通义千问 Qwen",
        "url":  "https://dashscope.console.aliyun.com",
        "models": ["qwen-turbo", "qwen-plus", "qwen-max", "qwen-long",
                   "qwen-vl-plus", "qwen-math-plus"],
        "default_model": "qwen-plus",
        "thinking_capable": True,
    },
    "zhipu": {
        "name": "智谱 GLM",
        "url":  "https://open.bigmodel.cn",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4-long",
                   "glm-4v-plus"],
        "default_model": "glm-4-flash",
        "thinking_capable": True,
    },
    "doubao": {
        "name": "豆包 Doubao",
        "url":  "https://console.volcengine.com/ark",
        "models": ["doubao-pro-32k", "doubao-pro-128k", "doubao-lite-32k",
                   "doubao-pro-4k"],
        "default_model": "doubao-pro-32k",
        "thinking_capable": True,
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "url":  "https://platform.moonshot.cn",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "default_model": "moonshot-v1-8k",
        "thinking_capable": True,
    },
    "baidu": {
        "name": "文心一言 Baidu",
        "url":  "https://console.bce.baidu.com/qianfan",
        "models": ["ernie-speed-128k", "ernie-lite-8k", "ernie-4.0-8k",
                   "ernie-4.0-turbo-8k"],
        "default_model": "ernie-speed-128k",
        "thinking_capable": False,
    },
    "spark": {
        "name": "讯飞星火 SparkDesk",
        "url":  "https://xinghuo.xfyun.cn",
        "models": ["generalv3.5", "generalv3", "4.0Ultra"],
        "default_model": "generalv3.5",
        "thinking_capable": False,
    },
    "mimo": {
        "name": "小米 MiMo",
        "url":  "https://mimo.mi.com",
        "models": ["mimo-v2.5", "mimo-v2.5-pro"],
        "default_model": "mimo-v2.5",
        "thinking_capable": True,
    },
    "ollama": {
        "name": "Ollama (Local, Free)",
        "url":  "https://ollama.ai",
        "models": [],
        "default_model": "qwen2.5:7b",
        "thinking_capable": False,
    },
}


def create_client(api_key: str = None, provider: str = "deepseek",
                  model: str = None,
                  ollama_model: str = "qwen2.5:7b",
                  ollama_url: str = "http://localhost:11434") -> object:

    info = PROVIDER_INFO.get(provider, {})
    effective_model = model or info.get("default_model", "")

    if provider == "ollama":
        client = OllamaClient(model=ollama_model, base_url=ollama_url)
        if client.is_running():
            print("[OK] Ollama connected, model:", ollama_model)
        else:
            print("[WARN] Ollama not running (%s), run: ollama serve" % ollama_url)
        return client

    if not api_key or api_key in ("", "YOUR_API_KEY_HERE"):
        print("[WARN] No API key configured, using Mock mode")
        return MockClient()

    if provider == "deepseek":
        print("[OK] DeepSeek API configured (%s)" % effective_model)
        return DeepSeekClient(api_key, model=effective_model)
    elif provider == "openai":
        print("[OK] OpenAI API configured (%s)" % effective_model)
        return OpenAIClient(api_key, model=effective_model)
    elif provider == "claude":
        print("[OK] Anthropic Claude configured (%s)" % effective_model)
        return ClaudeClient(api_key, model=effective_model)
    elif provider == "gemini":
        print("[OK] Google Gemini configured (%s)" % effective_model)
        return GeminiClient(api_key, model=effective_model)
    elif provider == "groq":
        print("[OK] Groq configured (%s)" % effective_model)
        return GroqClient(api_key, model=effective_model)
    elif provider == "qwen":
        print("[OK] 通义千问 configured (%s)" % effective_model)
        return QwenClient(api_key, model=effective_model)
    elif provider == "zhipu":
        print("[OK] 智谱 GLM configured (%s)" % effective_model)
        return ZhipuClient(api_key, model=effective_model)
    elif provider == "doubao":
        print("[OK] 豆包 configured (%s)" % effective_model)
        return DoubaoClient(api_key, model=effective_model)
    elif provider == "kimi":
        print("[OK] Kimi configured (%s)" % effective_model)
        return KimiClient(api_key, model=effective_model)
    elif provider == "baidu":
        print("[OK] 文心一言 configured (%s)" % effective_model)
        return BaiduClient(api_key, model=effective_model)
    elif provider == "spark":
        print("[OK] 讯飞星火 configured (%s)" % effective_model)
        return SparkClient(api_key, model=effective_model)
    elif provider == "mimo":
        print("[OK] 小米 MiMo configured (%s)" % effective_model)
        return MiMoClient(api_key, model=effective_model)

    print("[WARN] Unknown provider, using Mock mode")
    return MockClient()
