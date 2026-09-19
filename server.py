"""
workingAgent Console Server v2
- 由 main.py 调用 start_server(agent, auth_manager) 启动
- 完全共享同一个 ConsciousnessAgent 实例和 memory.db
- 手机用桌面端密码短语登录，记忆/人格完全互通
"""

import os
import json
import secrets
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException, Depends, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, FileResponse
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    raise ImportError("请先安装依赖：pip install fastapi uvicorn")

try:
    import jwt as pyjwt
except ImportError:
    raise ImportError("请先安装：pip install PyJWT")

# ── 全局共享实例（由 start_server 注入）─────────────────────
_agent        = None
_auth_manager = None
_SECRET_KEY   = os.environ.get("AGI_SECRET_KEY", secrets.token_hex(32))
ALGORITHM     = "HS256"
TOKEN_EXPIRE_DAYS = 30

# ── JWT ──────────────────────────────────────────────────────
def _create_token(user_id: str, name: str) -> str:
    payload = {
        "sub":  user_id,
        "name": name,
        "exp":  datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return pyjwt.encode(payload, _SECRET_KEY, algorithm=ALGORITHM)

def _decode_token(token: str) -> Optional[dict]:
    try:
        return pyjwt.decode(token, _SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None

def _get_current_user(request: Request) -> dict:
    token = request.cookies.get("agi_token")
    if not token:
        ah = request.headers.get("Authorization", "")
        if ah.startswith("Bearer "):
            token = ah[7:]
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = _decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期")
    if _auth_manager:
        user = _auth_manager.get_user(payload["sub"])
        if not user:
            raise HTTPException(status_code=401, detail="账户不存在")
    return {"user_id": payload["sub"], "name": payload.get("name", "")}

# ── FastAPI ──────────────────────────────────────────────────
app = FastAPI(title="workingAgent Console", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 简易登录速率限制 ──────────────────────────────────────
_login_attempts: dict[str, list[float]] = {}
_LOGIN_MAX = 5          # 最大尝试次数
_LOGIN_WINDOW = 300     # 时间窗口（秒）
_LOGIN_LOCKOUT = 60     # 锁定时长（秒）

class LoginRequest(BaseModel):
    passphrase: str

class ChatRequest(BaseModel):
    message: str = ""
    image: Optional[str] = None

class SimLifeModeRequest(BaseModel):
    enabled: bool

# ── 路由 ─────────────────────────────────────────────────────
@app.post("/api/login")
async def login(req: LoginRequest, response: Response):
    if not _auth_manager:
        raise HTTPException(status_code=503, detail="认证服务未就绪")

    # 速率限制
    import time as _time
    now = _time.time()
    ip = "unknown"
    attempts = _login_attempts.get(ip, [])
    # 清除过期记录
    attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
    # 检查是否锁定
    if len(attempts) >= _LOGIN_MAX:
        last = attempts[-_LOGIN_MAX]
        if now - last < _LOGIN_LOCKOUT:
            raise HTTPException(status_code=429, detail="登录尝试过多，请稍后再试")
        attempts = attempts[-(_LOGIN_MAX - 1):]  # 允许再试
    attempts.append(now)
    _login_attempts[ip] = attempts

    user = _auth_manager.verify_passphrase(req.passphrase)
    if not user:
        raise HTTPException(status_code=401, detail="密码短语错误")

    # 18765 端口仅允许管理员访问（第一个注册用户 + 名称匹配 ADMIN_NAME）
    admin_ids = _get_admin_user_ids()
    if admin_ids and user.user_id not in admin_ids:
        raise HTTPException(status_code=403, detail="仅管理员可访问管理页面")

    # 登录成功，清除记录
    _login_attempts.pop(ip, None)
    token = _create_token(user.user_id, user.name)
    response.set_cookie(
        key="agi_token", value=token,
        max_age=TOKEN_EXPIRE_DAYS * 86400,
        httponly=True, samesite="lax"
    )
    return {"ok": True, "name": user.name}

@app.post("/api/logout")
async def logout(response: Response, current: dict = Depends(_get_current_user)):
    response.delete_cookie("agi_token")
    return {"ok": True}

@app.get("/api/me")
async def me(current: dict = Depends(_get_current_user)):
    return {"user_id": current["user_id"], "name": current["name"]}

@app.post("/api/chat")
async def chat(req: ChatRequest, current: dict = Depends(_get_current_user)):
    if not req.message.strip() and not getattr(req, 'image', None):
        raise HTTPException(status_code=400, detail="消息不能为空")
    if _agent is None:
        raise HTTPException(status_code=503, detail="AGI引擎未就绪")

    user_id = current["user_id"]
    user    = _auth_manager.get_user(user_id) if _auth_manager else None

    # 临时切换 auth 为该用户，让 agent.process() 读到正确的 user_id
    if _auth_manager and user:
        _auth_manager.login(user)

    # 构造消息（含图片信息）
    msg = req.message.strip()
    image_url = getattr(req, 'image', None)
    if image_url:
        # 将 web URL 转为本地路径，让 agent 能访问到文件
        local_path = None
        if image_url.startswith("/images/"):
            from engine.image_gen import get_image_dir
            rel = image_url[len("/images/"):]
            candidate = get_image_dir() / rel
            if candidate.exists():
                local_path = str(candidate)
        if local_path:
            msg = f"[图片:{local_path}]\n{msg}" if msg else f"[图片:{local_path}]"
        else:
            msg = f"[用户发送了一张图片: {image_url}]\n{msg}" if msg else f"[用户发送了一张图片: {image_url}]"

    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        # 使用专用线程池，避免阻塞默认线程池导致其他 API 无响应
        if not hasattr(chat, '_pool'):
            chat._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='agi-chat')
        loop   = asyncio.get_event_loop()
        print(f"[chat] 开始处理: {msg[:50]}", flush=True)
        result = await asyncio.wait_for(
            loop.run_in_executor(chat._pool, _agent.process, msg),
            timeout=120
        )
        print(f"[chat] 处理完成", flush=True)
        reply  = result.get("response", str(result))

        # 检查工具调用结果中是否有图片
        for step in result.get("tool_steps", []):
            step_result = step.get("result", {})
            if isinstance(step_result, dict) and step_result.get("image_path"):
                img_path = step_result["image_path"]
                filename = Path(img_path).name
                reply += f"\n[img:/images/{filename}]"
    except Exception as e:
        reply = f"引擎错误：{e}"

    return {"reply": reply, "timestamp": datetime.now().isoformat()}

@app.get("/")
async def index():
    return HTMLResponse(_HTML)

@app.get("/api/simlife")
async def simlife_status(current: dict = Depends(_get_current_user)):
    """获取 SimLife 状态和场景模式"""
    if not _agent or not _agent.simlife:
        return {"available": False}
    mode = getattr(_agent, 'simlife_mode', False)
    summary = None
    try:
        summary = _agent.simlife.get_life_summary()
    except Exception:
        pass
    return {
        "available": True,
        "scene_mode": mode,
        "summary": summary,
    }

@app.post("/api/simlife/mode")
async def simlife_set_mode(req: SimLifeModeRequest, current: dict = Depends(_get_current_user)):
    """切换 SimLife 场景模式（面对面）"""
    if not _agent or not _agent.simlife:
        raise HTTPException(status_code=503, detail="SimLife 未启用")
    _agent.simlife_mode = req.enabled
    # 同步到 user_profile.json（让 SimLife web 面板也能感知）
    try:
        from pathlib import Path
        profile_path = Path(__file__).parent / "simlife" / "data" / "user_profile.json"
        if profile_path.exists():
            import json
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
            profile["entered"] = req.enabled
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return {"ok": True, "scene_mode": req.enabled}

# ── 邀请码（环境变量可覆盖）──────────────────────────────
INVITE_CODE = os.environ.get("AGI_INVITE_CODE", "agi2025202620272028")
ADMIN_NAME  = os.environ.get("AGI_ADMIN_NAME", "YOUNT")

# ── 管理员检查 ──────────────────────────────────────────────
def _get_admin_user_ids() -> list:
    """返回管理员 user_id 列表（第一个注册用户 + 名称匹配的用户）"""
    if not _auth_manager:
        return []
    users = _auth_manager.list_users()
    if not users:
        return []
    ids = [users[0].user_id]  # 第一个注册用户
    for u in users:
        if u.name == ADMIN_NAME and u.user_id not in ids:
            ids.append(u.user_id)
    return ids

def _require_admin(request: Request) -> dict:
    """要求当前登录用户是管理员"""
    user = _get_current_user(request)
    admin_ids = _get_admin_user_ids()
    if admin_ids and user["user_id"] not in admin_ids:
        raise HTTPException(status_code=403, detail="仅管理员可修改此设置")
    return user

# ── 路由：注册 / 配置 / 人格 / 硬件 ─────────────────────────

class RegisterRequest(BaseModel):
    name: str
    passphrase: str
    invite_code: str = ""

@app.post("/api/register")
async def register(req: RegisterRequest, response: Response):
    """注册新用户（需要邀请码）"""
    if not _auth_manager:
        raise HTTPException(status_code=503, detail="认证服务未就绪")
    name = req.name.strip()
    pp   = req.passphrase.strip()
    code = req.invite_code.strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写用户名")
    if len(pp) < 4:
        raise HTTPException(status_code=400, detail="密码短语至少4个字符")
    if code != INVITE_CODE:
        raise HTTPException(status_code=403, detail="邀请码错误")
    user = _auth_manager.create_user(name, pp)
    # 非管理员注册后不自动登录（18765只允许管理员操作）
    admin_ids = _get_admin_user_ids()
    is_admin_user = user.user_id in admin_ids
    if is_admin_user:
        token = _create_token(user.user_id, user.name)
        response.set_cookie(
            key="agi_token", value=token,
            max_age=TOKEN_EXPIRE_DAYS * 86400,
            httponly=True, samesite="lax"
        )
    return {"ok": True, "name": user.name, "is_admin": is_admin_user}

@app.get("/api/has_user")
async def has_user():
    """检查是否已有注册用户（前端用来决定显示登录还是注册）"""
    if not _auth_manager:
        return {"has_user": False}
    return {"has_user": _auth_manager.has_any_user()}

@app.get("/api/is_admin")
async def is_admin(current: dict = Depends(_get_current_user)):
    """检查当前用户是否是管理员"""
    admin_ids = _get_admin_user_ids()
    return {"is_admin": bool(current["user_id"] in admin_ids)}

# ── 配置文件路径 ─────────────────────────────────────────────
def _get_data_root() -> Path:
    """获取数据目录（与 desktop/config.py 一致）"""
    import sys
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "workingAgent"
    return Path.home() / ".workingagent"

def _get_project_root() -> Path:
    return Path(__file__).parent

@app.get("/api/config")
async def get_config(current: dict = Depends(_get_current_user)):
    """读取 config.json"""
    data_root = _get_data_root()
    cfg_path = data_root / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    return cfg

class ConfigUpdate(BaseModel):
    config: dict

@app.post("/api/config")
async def save_config(req: ConfigUpdate, current: dict = Depends(_require_admin)):
    """保存 config.json"""
    data_root = _get_data_root()
    cfg_path = data_root / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(req.config, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return {"ok": True}

@app.get("/api/personality")
async def get_personality(current: dict = Depends(_get_current_user)):
    """读取 personality.json"""
    data_root = _get_data_root()
    p_path = data_root / "personality.json"
    if p_path.exists():
        try:
            p = json.loads(p_path.read_text(encoding="utf-8"))
        except Exception:
            p = {}
    else:
        p = {}
    return p

class PersonalityUpdate(BaseModel):
    personality: dict

@app.post("/api/personality")
async def save_personality(req: PersonalityUpdate, current: dict = Depends(_require_admin)):
    """保存 personality.json"""
    data_root = _get_data_root()
    p_path = data_root / "personality.json"
    p_path.parent.mkdir(parents=True, exist_ok=True)
    p_path.write_text(
        json.dumps(req.personality, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return {"ok": True}

@app.get("/api/hardware")
async def get_hardware(current: dict = Depends(_get_current_user)):
    """读取 ha_config.json"""
    ha_path = _get_project_root() / "ha_config.json"
    if ha_path.exists():
        try:
            ha = json.loads(ha_path.read_text(encoding="utf-8"))
        except Exception:
            ha = {}
    else:
        ha = {}
    return ha

class HardwareUpdate(BaseModel):
    hardware: dict

@app.post("/api/hardware")
async def save_hardware(req: HardwareUpdate, current: dict = Depends(_require_admin)):
    """保存 ha_config.json"""
    ha_path = _get_project_root() / "ha_config.json"
    try:
        ha_path.parent.mkdir(parents=True, exist_ok=True)
        ha_path.write_text(
            json.dumps(req.hardware, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[Config] ha_config.json 已保存 ({len(req.hardware)} 个字段)")
        return {"ok": True}
    except PermissionError:
        print(f"[Config] 写入权限不足: {ha_path}")
        raise HTTPException(status_code=500, detail=f"配置文件 {ha_path} 无写入权限")
    except Exception as e:
        print(f"[Config] 保存 ha_config.json 失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")

@app.get("/api/providers")
async def get_providers():
    """返回所有可选的 LLM / Vision / TTS 提供商信息（无需登录）"""
    result = {"llm": {}, "vision": {}, "tts_voices": []}
    try:
        from engine.llm_client import PROVIDER_INFO
        result["llm"] = PROVIDER_INFO
    except Exception:
        pass
    try:
        from engine.vision_client import VISION_PROVIDER_INFO
        result["vision"] = VISION_PROVIDER_INFO
    except Exception:
        pass
    try:
        from engine.tts_engine import VOICE_OPTIONS
        result["tts"] = [{"id": v[0], "name": v[1]} for v in VOICE_OPTIONS]
    except Exception:
        pass
    return result


# ── 对外启动接口（由 main.py 的 _on_engine_ready 调用）───────
def start_server(agent, auth_manager, host="0.0.0.0", port=18765):
    """在 daemon 线程里启动 uvicorn，不阻塞 Qt 主线程"""
    global _agent, _auth_manager
    _agent        = agent
    _auth_manager = auth_manager

    def _run():
        try:
            import socket
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "本机IP"
        print(f"\n📱 手机访问 → http://{ip}:{port}")
        print(f"💻 本机访问 → http://localhost:{port}")
        print(f"🔑 登录方式：桌面端密码短语\n")
        uvicorn.run(app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── FastAPI startup：启动 PhoneWS 服务 ─────────────────────
_phone_ws_task = None  # 保持强引用，防止 task 被 GC 回收

@app.on_event("startup")
async def _on_startup():
    """uvicorn 事件循环启动后，启动 PhoneWS WebSocket 服务"""
    global _phone_ws_task
    try:
        from hardware.phone_ws_server import get_phone_server
        ws = get_phone_server()
        if ws:
            import asyncio
            _phone_ws_task = asyncio.create_task(ws.start())
            print("[PhoneWS] WebSocket 服务已在后台启动")
    except Exception as e:
        print(f"[PhoneWS] 启动失败: {e}")


class ClearMemoryRequest(BaseModel):
    scope: str = "all"


@app.post("/api/clear_memory")
async def clear_memory(req: ClearMemoryRequest, current: dict = Depends(_require_admin)):
    """清除记忆（多步确认后调用）"""
    from engine.db_guard import guarded_connect
    db_path = _get_data_root() / "memory.db"
    scope = req.scope

    try:
        with guarded_connect(str(db_path)) as conn:
            deleted_msg = ""
            if scope == "all":
                conn.execute("DELETE FROM memories")
                for tbl in ("memory_edges", "memory_entities", "formed_cognition"):
                    try:
                        conn.execute(f"DELETE FROM {tbl}")
                    except Exception:
                        pass
                try:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='memories'")
                except Exception:
                    pass
                deleted_msg = "全部记忆及关联网络"
            elif scope in ("detail", "outline", "summary"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE level=?", (scope,)
                ).fetchone()[0]
                conn.execute("DELETE FROM memories WHERE level=?", (scope,))
                deleted_msg = f"{count} 条 {scope} 层记忆"
            elif scope in ("emotional", "semantic", "visual",
                           "auditory", "procedural", "autobio"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE modality=?", (scope,)
                ).fetchone()[0]
                conn.execute("DELETE FROM memories WHERE modality=?", (scope,))
                deleted_msg = f"{count} 条 {scope} 模态记忆"
            else:
                return {"ok": False, "error": f"未知范围: {scope}"}

            conn.commit()
        print(f"[Config] 记忆已清除: {deleted_msg}")
        return {"ok": True, "message": f"✅ 已清除：{deleted_msg}"}
    except Exception as e:
        print(f"[Config] 清除记忆失败: {e}")
        return {"ok": False, "error": str(e)}


# ── 记忆库查看（仅管理员）─────────────────────────────────
@app.get("/api/memories")
async def list_memories(
    current: dict = Depends(_require_admin),
    q: str = "",
    user_id: str = "",
    level: str = "",
    modality: str = "",
    limit: int = 200,
    offset: int = 0,
):
    """查询所有用户的记忆库（管理员权限）

    支持关键词搜索、按用户/层级/模态筛选、分页。
    返回记忆列表 + 统计信息（按用户/层级/模态分组计数）。
    """
    from engine.db_guard import guarded_connect
    db_path = _get_data_root() / "memory.db"
    if limit > 1000:
        limit = 1000
    if limit < 1:
        limit = 200

    where_parts = []
    params = []
    if q.strip():
        where_parts.append("content LIKE ?")
        params.append(f"%{q.strip()}%")
    if user_id:
        where_parts.append("user_id = ?")
        params.append(user_id)
    if level:
        where_parts.append("level = ?")
        params.append(level)
    if modality:
        where_parts.append("modality = ?")
        params.append(modality)
    where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    try:
        with guarded_connect(str(db_path)) as conn:
            # 检查 user_name 列是否存在（旧库迁移兼容）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
            has_user_name = "user_name" in cols

            select_cols = (
                "id, content, modality, level, emotion_json, importance, "
                "tags_json, created_at, access_count, user_id"
            )
            if has_user_name:
                select_cols += ", user_name"

            rows = conn.execute(
                f"SELECT {select_cols} FROM memories{where_clause} "
                f"ORDER BY created_at DESC, importance DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)
            ).fetchall()

            total = conn.execute(
                f"SELECT COUNT(*) FROM memories{where_clause}", params
            ).fetchone()[0]

            # 分组统计（始终基于全量，不受筛选影响，用于侧边筛选器）
            user_stats = conn.execute(
                "SELECT user_id, COUNT(*) FROM memories GROUP BY user_id ORDER BY COUNT(*) DESC"
            ).fetchall()
            level_stats = conn.execute(
                "SELECT level, COUNT(*) FROM memories GROUP BY level"
            ).fetchall()
            modality_stats = conn.execute(
                "SELECT modality, COUNT(*) FROM memories GROUP BY modality"
            ).fetchall()

            # 尝试合并 user_name 到用户统计
            user_name_map = {}
            if has_user_name:
                for r in conn.execute(
                    "SELECT DISTINCT user_id, user_name FROM memories WHERE user_name != ''"
                ).fetchall():
                    user_name_map[r[0]] = r[1]

            items = []
            for r in rows:
                # 补齐 user_name
                uid = r[9]
                uname = r[10] if has_user_name else ""
                if not uname and uid in user_name_map:
                    uname = user_name_map[uid]
                # 解析 emotion
                try:
                    emo = json.loads(r[4]) if r[4] else {}
                except Exception:
                    emo = {}
                # 解析 tags
                try:
                    tags = json.loads(r[6]) if r[6] else []
                except Exception:
                    tags = []
                items.append({
                    "id": r[0],
                    "content": r[1],
                    "modality": r[2],
                    "level": r[3],
                    "emotion": emo,
                    "importance": round(r[5], 2) if r[5] is not None else 0,
                    "tags": tags,
                    "created_at": r[7],
                    "access_count": r[8],
                    "user_id": uid,
                    "user_name": uname,
                })

            return {
                "ok": True,
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
                "stats": {
                    "total": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                    "by_user": [
                        {
                            "user_id": u[0],
                            "user_name": user_name_map.get(u[0], ""),
                            "count": u[1],
                        }
                        for u in user_stats
                    ],
                    "by_level": [{"level": l[0], "count": l[1]} for l in level_stats],
                    "by_modality": [{"modality": m[0], "count": m[1]} for m in modality_stats],
                },
            }
    except Exception as e:
        return {"ok": False, "error": str(e), "items": [], "total": 0, "stats": {}}


@app.get("/api/skills")
async def list_skills(
    current: dict = Depends(_require_admin),
    q: str = "",
):
    """查询技能库（workingAgent 阶段3 · 可复用技能沉淀，管理员权限）

    返回 skills 表全量：任务标题、工具序列、步骤摘要、使用次数、时间。
    支持关键词搜索（标题/工具/步骤）。
    """
    from engine.db_guard import guarded_connect
    db_path = _get_data_root() / "memory.db"
    try:
        where_sql = ""
        params = ()
        if q.strip():
            where_sql = (" WHERE title LIKE ? OR tool_sequence LIKE ? "
                         "OR step_titles LIKE ?")
            kw = f"%{q.strip()}%"
            params = (kw, kw, kw)
        with guarded_connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, title, tool_sequence, step_titles, "
                "usage_count, created_at, last_used_at "
                f"FROM skills{where_sql} "
                "ORDER BY usage_count DESC, last_used_at DESC",
                params
            ).fetchall()
            items = []
            for r in rows:
                def _json(v, default):
                    try:
                        return json.loads(v) if v else default
                    except Exception:
                        return default
                items.append({
                    "id": r[0],
                    "title": r[1],
                    "tool_sequence": _json(r[2], []),
                    "step_titles": _json(r[3], []),
                    "usage_count": r[4],
                    "created_at": r[5],
                    "last_used_at": r[6],
                })
            total = conn.execute(
                f"SELECT COUNT(*) FROM skills{where_sql}", params
            ).fetchone()[0]
        return {"ok": True, "items": items, "total": total}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": [], "total": 0}


@app.get("/api/phone_status")
async def phone_status(current: dict = Depends(_get_current_user)):
    """查询手机 WebSocket 连接状态"""
    try:
        from hardware.phone_ws_server import get_phone_server
        ws = get_phone_server()
        if ws:
            return ws.get_status()
    except Exception:
        pass
    return {"connected": False, "error": "PhoneWS 未启动"}


class LocationUpdate(BaseModel):
    lat: float
    lng: float
    accuracy: float = 0


@app.post("/api/update_location")
async def update_location(req: LocationUpdate):
    """接收网页浏览器上报的 GPS 坐标"""
    from hardware.location_resolver import update_browser_gps
    update_browser_gps(req.lat, req.lng, req.accuracy)
    return {"ok": True}


@app.get("/api/test_sensor")
async def test_sensor():
    """测试：手动获取手机传感器数据（无需登录，用于调试）"""
    try:
        from hardware.phone_ws_server import get_phone_server
        ws = get_phone_server()
        if not ws or not ws.is_connected():
            return {"ok": False, "error": "手机未连接"}

        data = await ws.get_sensor_data()
        if not data:
            return {"ok": False, "error": "传感器数据为空"}

        gps = data.get("gps", {})
        # 打印到服务端日志，方便排查
        print(f"[DebugSensor] 原始返回键: {list(data.keys())}")
        print(f"[DebugSensor] GPS: {gps}")

        return {
            "ok": True,
            "gps": gps,
            "battery": data.get("battery"),
            "light": data.get("light"),
            "has_location": bool(gps.get("lat")),
            "raw_keys": list(data.keys()),
        }
    except Exception as e:
        print(f"[DebugSensor] 异常: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/static/phone_client.py")
async def download_phone_client():
    """下载手机客户端脚本（Termux 用，无需登录）"""
    p = _get_project_root() / "phone_client.py"
    if p.exists():
        content = p.read_text(encoding="utf-8")
        from fastapi.responses import Response
        return Response(content=content, media_type="text/x-python",
                        headers={"Content-Disposition": 'attachment; filename="phone_client.py"'})
    raise HTTPException(status_code=404, detail="phone_client.py 不存在")


# ── 图片 API ────────────────────────────────────────────────

@app.post("/api/generate_image")
async def api_generate_image(req: dict, current: dict = Depends(_get_current_user)):
    """生成图片（Cogview-3-Flash / pollinations.ai）"""
    prompt = req.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    size = req.get("size", "1024x1024")
    try:
        from engine.image_gen import generate_image_with_prompt
        import asyncio
        loop = asyncio.get_event_loop()
        image_path = await loop.run_in_executor(None, generate_image_with_prompt, prompt, size)
        if image_path:
            # 返回相对 URL
            filename = Path(image_path).name
            return {"ok": True, "image_url": f"/images/{filename}", "image_path": image_path}
        return {"ok": False, "error": "图片生成失败"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/upload_image")
async def api_upload_image(req: dict, current: dict = Depends(_get_current_user)):
    """上传图片（聊天中发送图片，base64 格式）"""
    import base64
    b64_data = req.get("data", "")
    filename = req.get("filename", "image.jpg")
    if not b64_data:
        raise HTTPException(status_code=400, detail="未选择文件")
    try:
        from engine.image_gen import get_image_dir
        img_dir = get_image_dir() / "uploads"
        img_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
        save_path = img_dir / safe_name

        # 去掉 data:image/xxx;base64, 前缀
        if "," in b64_data:
            b64_data = b64_data.split(",", 1)[1]
        content = base64.b64decode(b64_data)

        with open(save_path, "wb") as f:
            f.write(content)
        return {"ok": True, "image_url": f"/images/uploads/{safe_name}", "image_path": str(save_path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/images/{path:path}")
async def serve_image(path: str):
    """静态图片文件服务"""
    from engine.image_gen import get_image_dir
    img_path = get_image_dir() / path
    if img_path.exists() and img_path.is_file():
        return FileResponse(str(img_path))
    raise HTTPException(status_code=404, detail="图片不存在")


# ── 文件下载 ────────────────────────────────────────────────
_DOWNLOAD_DIR: Optional[Path] = None


def _get_download_dir() -> Path:
    """获取文件下载目录（用于 Office 文件等生成物的 Web 交付）"""
    global _DOWNLOAD_DIR
    if _DOWNLOAD_DIR is None:
        _DOWNLOAD_DIR = _get_project_root() / "downloads"
        _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _DOWNLOAD_DIR


@app.get("/api/download/{filename:path}")
async def download_file(filename: str):
    """下载生成的文件（Word/PPT/Excel/PDF 等）"""
    from fastapi.responses import FileResponse as FR
    safe_path = _get_download_dir() / filename
    # 安全校验：防止路径穿越
    try:
        safe_path = safe_path.resolve()
        if not str(safe_path).startswith(str(_get_download_dir().resolve())):
            raise HTTPException(status_code=403, detail="禁止访问")
    except Exception:
        raise HTTPException(status_code=403, detail="禁止访问")
    if safe_path.exists() and safe_path.is_file():
        return FR(str(safe_path), filename=safe_path.name)
    raise HTTPException(status_code=404, detail="文件不存在")


# ── 高危工具确认 API ────────────────────────────────────────

@app.get("/api/confirm_stream")
async def confirm_stream(current: dict = Depends(_get_current_user)):
    """SSE 流：推送确认请求到前端"""
    from hardware.web_confirm import create_sse_queue, remove_sse_queue
    import json
    import asyncio

    q = create_sse_queue()

    async def event_generator():
        try:
            while True:
                try:
                    data = q.get_nowait()
                    yield f"data: {data}\n\n"
                except Exception:
                    # 无数据时不阻塞事件循环，每 2 秒发一次 keepalive
                    yield f": keepalive\n\n"
                    await asyncio.sleep(2)
        finally:
            remove_sse_queue(q)

    from starlette.responses import StreamingResponse
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/confirm")
async def api_confirm(req: dict, current: dict = Depends(_get_current_user)):
    """前端回复确认结果"""
    from hardware.web_confirm import resolve_confirm
    confirm_id = req.get("id", "")
    approved = req.get("approved", False)
    ok = resolve_confirm(confirm_id, approved)
    return {"ok": ok}


@app.get("/api/pending_confirms")
async def pending_confirms(current: dict = Depends(_get_current_user)):
    """查询待确认请求"""
    from hardware.web_confirm import get_pending_confirms
    return {"pending": get_pending_confirms()}


# ── 内嵌前端 HTML ────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>workingAgent · 控制台</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root{
  --ink:#0a0c12;--paper:#111520;--layer:#181d2a;--rim:#252d42;
  --muted:#3d4a63;--dim:#5a6a85;--soft:#8899b8;--text:#dde4f0;
  --bright:#f0f4ff;--blue:#4a8fff;--indigo:#6c63f7;--teal:#2dd4bf;
  --danger:#f04f5a;--ok:#34d78a;--warn:#f5a623;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--ink);color:var(--text);
  font-family:'Sora',sans-serif;overflow:hidden;-webkit-tap-highlight-color:transparent}

/* ── 通用 ── */
.btn{padding:.8rem 1.2rem;border:none;border-radius:10px;color:#fff;
  font-family:'Sora',sans-serif;font-size:.9rem;font-weight:600;cursor:pointer;
  background:linear-gradient(135deg,var(--blue),var(--indigo));transition:opacity .2s,transform .1s}
.btn:active{transform:scale(.98);opacity:.9}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-sm{padding:.5rem .9rem;font-size:.8rem}
.btn-outline{background:none;border:1px solid var(--rim);color:var(--dim)}
.btn-outline:hover{color:var(--text);border-color:var(--muted)}
.btn-danger{background:var(--danger)}
.input{width:100%;background:var(--layer);border:1px solid var(--rim);
  border-radius:8px;padding:.65rem .9rem;color:var(--text);font-family:'Sora',sans-serif;
  font-size:.9rem;outline:none;transition:border-color .2s}
.input:focus{border-color:var(--blue)}
.field{margin-bottom:1rem}
.field label{display:block;font-size:.72rem;font-weight:500;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);margin-bottom:.4rem}
.field .hint{font-size:.72rem;color:var(--dim);margin-top:.3rem;line-height:1.5}
select.input{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%235a6a85' d='M6 8L0 0h12z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right .8rem center;padding-right:2rem}
textarea.input{resize:vertical;min-height:60px;font-family:'Sora',sans-serif}
.section-title{font-size:.8rem;font-weight:700;color:var(--bright);
  border-bottom:1px solid var(--rim);padding-bottom:.5rem;margin:1.5rem 0 1rem}
.card{background:var(--paper);border:1px solid var(--rim);border-radius:14px;padding:1.5rem}
.err-msg{font-size:.8rem;color:var(--danger);min-height:1.2em;margin-top:.5rem}
.ok-msg{font-size:.8rem;color:var(--ok);min-height:1.2em;margin-top:.5rem}

/* ── 登录/注册页 ── */
#auth-page{height:100dvh;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:2rem;
  background:radial-gradient(ellipse 60% 40% at 50% 0%,rgba(74,143,255,.12) 0%,transparent 70%),
             radial-gradient(ellipse 40% 30% at 80% 80%,rgba(108,99,247,.08) 0%,transparent 60%)}
.brand{display:flex;align-items:center;gap:.6rem;margin-bottom:2.5rem}
.brand-mark{width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,var(--blue),var(--indigo));
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 24px rgba(74,143,255,.25)}
.brand-mark svg{width:18px;height:18px;fill:#fff}
.brand-name{font-family:'JetBrains Mono',monospace;font-size:1.1rem;font-weight:500;
  letter-spacing:.15em;color:var(--bright)}
.brand-name span{color:var(--blue)}
.auth-card{width:100%;max-width:380px}
.auth-card-title{font-size:1.25rem;font-weight:600;color:var(--bright);margin-bottom:.3rem}
.auth-card-sub{font-size:.8rem;color:var(--dim);margin-bottom:1.5rem;line-height:1.5}
.auth-switch{text-align:center;margin-top:1.2rem;font-size:.8rem;color:var(--dim)}
.auth-switch a{color:var(--blue);cursor:pointer;text-decoration:none}

/* ── 主布局 ── */
#app{display:none;flex-direction:column;height:100dvh}
.navbar{display:flex;align-items:center;gap:.3rem;padding:.6rem .8rem;
  background:var(--paper);border-bottom:1px solid var(--rim);flex-shrink:0;overflow-x:auto}
.nav-tab{padding:.5rem .9rem;border:none;border-radius:8px;
  background:none;color:var(--dim);font-family:'Sora',sans-serif;font-size:.82rem;
  font-weight:500;cursor:pointer;white-space:nowrap;transition:all .2s}
.nav-tab:hover{color:var(--text);background:var(--layer)}
.nav-tab.active{background:var(--layer);color:var(--blue)}
.nav-spacer{flex:1}
.nav-user{font-size:.75rem;color:var(--dim);margin-right:.5rem;
  font-family:'JetBrains Mono',monospace}

.page{display:none;flex:1;overflow-y:auto;padding:1.2rem}
.page.active{display:block}

/* ── 聊天页 ── */
#page-chat{display:none;flex-direction:column;padding:0}
#page-chat.active{display:flex}
.chat-msgs{flex:1;overflow-y:auto;padding:1.2rem 1rem;display:flex;flex-direction:column;
  gap:1rem;scroll-behavior:smooth}
.chat-msgs::-webkit-scrollbar{width:3px}
.chat-msgs::-webkit-scrollbar-thumb{background:var(--rim);border-radius:2px}
.chat-empty{margin:auto;text-align:center;padding:2rem}
.chat-empty-icon{width:52px;height:52px;margin:0 auto 1rem;border-radius:16px;
  background:linear-gradient(135deg,var(--blue),var(--indigo));
  display:flex;align-items:center;justify-content:center;box-shadow:0 0 30px rgba(74,143,255,.2)}
.chat-empty-icon svg{width:24px;height:24px;fill:#fff}
.chat-row{display:flex;flex-direction:column;max-width:80%;animation:rise .22s ease}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.chat-row.me{align-self:flex-end;align-items:flex-end}
.chat-row.agi{align-self:flex-start;align-items:flex-start}
.chat-label{font-size:.62rem;color:var(--muted);margin-bottom:.28rem;
  font-family:'JetBrains Mono',monospace}
.chat-bubble{padding:.72rem 1rem;border-radius:16px;font-size:.92rem;line-height:1.65;
  word-break:break-word;white-space:pre-wrap}
.chat-row.me .chat-bubble{background:linear-gradient(135deg,#1a3a6e,#1e2f5a);
  border:1px solid #2a4a8a;border-bottom-right-radius:4px;color:#d4e4ff}
.chat-row.agi .chat-bubble{background:var(--layer);border:1px solid var(--rim);
  border-bottom-left-radius:4px;color:var(--text)}
.typing{display:flex;gap:5px;align-items:center;padding:.72rem 1rem}
.typing span{width:7px;height:7px;border-radius:50%;background:var(--muted);
  animation:hop 1.3s ease-in-out infinite}
.typing span:nth-child(2){animation-delay:.18s}
.typing span:nth-child(3){animation-delay:.36s}
@keyframes hop{0%,60%,100%{transform:translateY(0);opacity:.6}30%{transform:translateY(-7px);opacity:1}}
.chat-inputbar{display:flex;align-items:flex-end;gap:.6rem;padding:.8rem 1rem;
  background:var(--paper);border-top:1px solid var(--rim);flex-shrink:0}
.chat-inputbar textarea{flex:1;background:var(--layer);border:1px solid var(--rim);
  border-radius:12px;padding:.65rem .9rem;color:var(--text);
  font-family:'Sora',sans-serif;font-size:.9rem;line-height:1.5;resize:none;outline:none;
  min-height:42px;max-height:130px;transition:border-color .2s}
.chat-inputbar textarea:focus{border-color:var(--blue)}
.imgbtn{background:var(--layer);border:1px solid var(--rim);border-radius:10px;
  width:36px;height:36px;display:flex;align-items:center;justify-content:center;
  color:var(--dim);cursor:pointer;flex-shrink:0;transition:all .15s}
.imgbtn:hover{border-color:var(--blue);color:var(--blue)}
.chat-bubble img.chat-img{max-width:100%;max-height:280px;border-radius:8px;margin-top:6px;cursor:pointer;display:block}
.chat-bubble a.chat-img-link{color:var(--blue);text-decoration:underline;font-size:.85rem}
.sendbtn{width:42px;height:42px;flex-shrink:0;border-radius:12px;
  background:linear-gradient(135deg,var(--blue),var(--indigo));border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 14px rgba(74,143,255,.35);transition:opacity .2s,transform .1s}
.sendbtn:active{transform:scale(.9)}
.sendbtn:disabled{opacity:.35;cursor:not-allowed}
.sendbtn svg{width:17px;height:17px;fill:#fff}

/* ── 设置页 ── */
.settings-grid{display:grid;grid-template-columns:1fr;gap:1.2rem;max-width:680px;margin:0 auto}
.toggle{display:flex;align-items:center;justify-content:space-between;padding:.5rem 0}
.toggle-label{font-size:.85rem;color:var(--text)}
.switch{position:relative;width:44px;height:24px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:var(--rim);border-radius:12px;transition:.3s;cursor:pointer}
.slider:before{content:'';position:absolute;width:18px;height:18px;left:3px;top:3px;
  background:var(--soft);border-radius:50%;transition:.3s}
.switch input:checked+.slider{background:var(--blue)}
.switch input:checked+.slider:before{transform:translateX(20px);background:#fff}
.range-row{display:flex;align-items:center;gap:.8rem}
.range-row input[type=range]{flex:1;accent-color:var(--blue)}
.range-val{font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--blue);
  min-width:40px;text-align:right}

/* ── 人格滑块 ── */
.trait-row{margin-bottom:.8rem}
.trait-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:.3rem}
.trait-name{font-size:.82rem;color:var(--text);font-weight:500}
.trait-val{font-family:'JetBrains Mono',monospace;font-size:.82rem;color:var(--blue);font-weight:700}
.trait-slider{width:100%;accent-color:var(--blue)}
.trait-labels{display:flex;justify-content:space-between;font-size:.68rem;color:var(--dim)}
.trait-desc{font-size:.68rem;color:var(--dim);margin-top:.2rem}

/* ── 硬件页 ── */
.hw-device-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem}
.hw-device-row .input{flex:1}

/* ── 记忆库页 ── */
.mem-toolbar{display:flex;align-items:center;gap:.6rem;max-width:900px;margin:0 auto 1rem;flex-wrap:wrap}
.mem-count{font-size:.75rem;color:var(--dim);font-family:'JetBrains Mono',monospace}
.mem-filters{display:flex;align-items:center;gap:.6rem;max-width:900px;margin:0 auto 1rem;flex-wrap:wrap}
.mem-stat-chips{display:flex;gap:.4rem;flex-wrap:wrap}
.mem-chip{font-size:.68rem;padding:.2rem .55rem;border-radius:10px;background:var(--layer);
  border:1px solid var(--rim);color:var(--soft);font-family:'JetBrains Mono',monospace;white-space:nowrap}
.mem-chip b{color:var(--bright);font-weight:600}
.mem-list{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:.6rem;padding-bottom:2rem}
.mem-empty{text-align:center;color:var(--muted);font-size:.85rem;padding:3rem 1rem}
.mem-card{background:var(--paper);border:1px solid var(--rim);border-radius:12px;padding:.9rem 1rem;
  transition:border-color .15s;cursor:pointer}
.mem-card:hover{border-color:var(--muted)}
.mem-card-head{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.5rem}
.mem-user-tag{font-size:.68rem;font-weight:600;padding:.15rem .5rem;border-radius:8px;
  font-family:'JetBrains Mono',monospace;white-space:nowrap}
.mem-badge{font-size:.65rem;padding:.12rem .45rem;border-radius:6px;font-weight:600;letter-spacing:.03em}
.mem-badge.detail{background:rgba(74,143,255,.12);color:var(--blue)}
.mem-badge.outline{background:rgba(108,99,247,.12);color:var(--indigo)}
.mem-badge.summary{background:rgba(45,212,191,.12);color:var(--teal)}
.mem-badge.semantic{background:rgba(74,143,255,.1);color:var(--blue)}
.mem-badge.emotional{background:rgba(245,166,35,.12);color:var(--warn)}
.mem-badge.visual{background:rgba(52,215,138,.12);color:var(--ok)}
.mem-badge.auditory{background:rgba(240,79,90,.12);color:var(--danger)}
.mem-badge.procedural{background:rgba(108,99,247,.12);color:var(--indigo)}
.mem-badge.autobio{background:rgba(136,153,184,.12);color:var(--soft)}
.mem-imp{font-size:.68rem;color:var(--dim);font-family:'JetBrains Mono',monospace}
.mem-content{font-size:.86rem;line-height:1.6;color:var(--text);white-space:pre-wrap;word-break:break-word}
.mem-content.collapsed{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.mem-meta{display:flex;gap:.8rem;margin-top:.5rem;font-size:.68rem;color:var(--muted);
  font-family:'JetBrains Mono',monospace;flex-wrap:wrap}
.mem-tags{display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.4rem}
.mem-tag{font-size:.62rem;padding:.1rem .4rem;border-radius:5px;background:var(--layer);
  color:var(--soft);border:1px solid var(--rim)}
.mem-pager{display:flex;align-items:center;justify-content:center;gap:1rem;max-width:900px;
  margin:0 auto;padding:1rem}
.mem-page-info{font-size:.78rem;color:var(--dim);font-family:'JetBrains Mono',monospace}
.hw-device-row .btn-sm{flex-shrink:0}
</style>
</head>
<body>

<!-- ════════ 登录/注册页 ════════ -->
<div id="auth-page">
  <div class="brand">
    <div class="brand-mark">
      <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 14H9V8h2v8zm4 0h-2V8h2v8z"/></svg>
    </div>
    <div class="brand-name">workingAgent</div>
  </div>
  <div class="card auth-card">
    <!-- 登录 -->
    <div id="login-view">
      <div class="auth-card-title">欢迎回来</div>
      <div class="auth-card-sub">使用密码短语登录，记忆与人格完全互通。</div>
      <div class="field">
        <label>密码短语</label>
        <input id="pp" class="input" type="password" placeholder="输入密码短语…" autocomplete="current-password">
      </div>
      <button class="btn" id="loginBtn" onclick="doLogin()">登录</button>
      <div class="err-msg" id="loginErr"></div>
      <div class="auth-switch">还没有账户？<a onclick="showRegister()">注册新账户</a></div>
    </div>
    <!-- 注册 -->
    <div id="register-view" style="display:none">
      <div class="auth-card-title">创建账户</div>
      <div class="auth-card-sub">需要邀请码才能注册。第一个注册的账户为管理员，其他账户仅限聊天（18767端口）。</div>
      <div class="field">
        <label>邀请码</label>
        <input id="regInvite" class="input" type="password" placeholder="输入邀请码" autocomplete="off">
      </div>
      <div class="field">
        <label>用户名</label>
        <input id="regName" class="input" type="text" placeholder="你的名字" autocomplete="name">
      </div>
      <div class="field">
        <label>密码短语</label>
        <input id="regPp" class="input" type="password" placeholder="至少4个字符" autocomplete="new-password">
      </div>
      <button class="btn" id="regBtn" onclick="doRegister()">注册并登录</button>
      <div class="err-msg" id="regErr"></div>
      <div class="auth-switch">已有账户？<a onclick="showLogin()">返回登录</a></div>
    </div>
  </div>
</div>

<!-- ════════ 主应用 ════════ -->
<div id="app">
  <div class="navbar">
    <button class="nav-tab active" data-page="chat" onclick="navTo('chat')">💬 对话</button>
    <button class="nav-tab" data-page="settings" onclick="navTo('settings')">⚙️ 设置</button>
    <button class="nav-tab" data-page="personality" onclick="navTo('personality')">🎭 人格</button>
    <button class="nav-tab" data-page="hardware" onclick="navTo('hardware')">📱 硬件</button>
    <button class="nav-tab" data-page="memory" onclick="navTo('memory')" id="nav-memory" style="display:none">🧠 记忆库</button>
    <button class="nav-tab" data-page="skills" onclick="navTo('skills')" id="nav-skills" style="display:none">💼 技能库</button>
    <div class="nav-spacer"></div>
    <span class="nav-user" id="navUser"></span>
    <button class="btn btn-outline btn-sm" onclick="doLogout()">退出</button>
  </div>

  <!-- 聊天页 -->
  <div id="page-chat" class="page active">
    <div class="chat-msgs" id="msgs">
      <div class="chat-empty">
        <div class="chat-empty-icon"><svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div>
        <div style="font-size:1rem;font-weight:600;color:var(--soft)">你好</div>
        <div style="font-size:.8rem;color:var(--muted)">有什么我可以帮你的？</div>
      </div>
    </div>
    <div class="chat-inputbar">
      <input type="file" id="imgInput" accept="image/*" style="display:none" onchange="onImgSelect(this)">
      <button class="imgbtn" onclick="document.getElementById('imgInput').click()" title="发送图片">
        <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
      </button>
      <div id="imgPreview" style="display:none;position:relative;margin-right:4px">
        <img id="imgThumb" style="height:40px;border-radius:6px;border:1px solid var(--rim)">
        <span onclick="clearImgPreview()" style="position:absolute;top:-6px;right:-6px;background:var(--danger);color:#fff;border-radius:50%;width:16px;height:16px;font-size:10px;display:flex;align-items:center;justify-content:center;cursor:pointer">&times;</span>
      </div>
      <textarea id="inp" placeholder="输入消息…" rows="1" oninput="rsz(this)" onkeydown="onKey(event)"></textarea>
      <button class="sendbtn" id="sendBtn" onclick="send()">
        <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      </button>
    </div>
  </div>

  <!-- 设置页 -->
  <div id="page-settings" class="page">
    <div class="settings-grid">
      <div class="card">
        <div class="section-title">🤖 LLM 配置</div>
        <div class="field">
          <label>服务商</label>
          <select id="cfg-provider" class="input"></select>
        </div>
        <div class="field">
          <label>模型</label>
          <input id="cfg-model" class="input" type="text" placeholder="模型名称">
        </div>
        <div class="field">
          <label>API Key</label>
          <input id="cfg-apikey" class="input" type="password" placeholder="sk-...">
        </div>
        <div id="ollama-fields" style="display:none">
          <div class="field"><label>Ollama URL</label><input id="cfg-ollama-url" class="input" type="text"></div>
          <div class="field"><label>Ollama Model</label><input id="cfg-ollama-model" class="input" type="text"></div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">👁️ 多模态模型（Vision）</div>
        <div class="field">
          <label>服务商</label>
          <select id="cfg-vision-provider" class="input"></select>
        </div>
        <div class="field">
          <label>模型</label>
          <input id="cfg-vision-model" class="input" type="text">
        </div>
        <div class="field">
          <label>API Key（留空继承主 LLM）</label>
          <input id="cfg-vision-apikey" class="input" type="password">
        </div>
        <div class="field">
          <label>自定义地址（留空用默认）</label>
          <input id="cfg-vision-url" class="input" type="text">
        </div>
      </div>

      <div class="card">
        <div class="section-title">🔊 语音合成（TTS）</div>
        <div class="toggle">
          <span class="toggle-label">回复后自动朗读</span>
          <label class="switch"><input type="checkbox" id="cfg-tts-enable"><span class="slider"></span></label>
        </div>
        <div class="field"><label>声音</label><select id="cfg-tts-voice" class="input"></select></div>
        <div class="field">
          <label>语速</label>
          <div class="range-row">
            <input type="range" id="cfg-tts-rate" min="-50" max="50" value="0">
            <span class="range-val" id="tts-rate-val">+0%</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">🎤 语音识别（STT）</div>
        <div class="field">
          <label>识别引擎</label>
          <select id="cfg-stt-provider" class="input">
            <option value="deepseek">DeepSeek Whisper（在线）</option>
            <option value="xunfei">讯飞语音识别（中文最优）</option>
            <option value="whisper_local">本地 Whisper（离线）</option>
          </select>
        </div>
        <div id="stt-xunfei" style="display:none">
          <div class="field"><label>APPID</label><input id="cfg-xf-appid" class="input" type="text"></div>
          <div class="field"><label>API Key</label><input id="cfg-xf-key" class="input" type="text"></div>
          <div class="field"><label>API Secret</label><input id="cfg-xf-secret" class="input" type="password"></div>
        </div>
        <div id="stt-whisper" style="display:none">
          <div class="field"><label>模型</label>
            <select id="cfg-whisper-model" class="input">
              <option value="tiny">tiny (~39MB)</option>
              <option value="base">base (~74MB)</option>
              <option value="small">small (~244MB)</option>
              <option value="medium">medium (~769MB)</option>
              <option value="large">large (~1.5GB)</option>
            </select>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">🧠 思考模式</div>
        <div class="field">
          <label>模式</label>
          <select id="cfg-think-mode" class="input">
            <option value="auto">自动（推荐）</option>
            <option value="always_on">始终开启</option>
            <option value="always_off">始终关闭</option>
          </select>
        </div>
        <div class="field">
          <label>思考深度</label>
          <select id="cfg-think-effort" class="input">
            <option value="low">低</option>
            <option value="medium">中</option>
            <option value="high">高</option>
            <option value="max">最大</option>
          </select>
        </div>
        <div class="field">
          <label>思考预算（tokens）</label>
          <input id="cfg-think-budget" class="input" type="number" min="1024" max="32768" step="1024">
        </div>
      </div>

      <div class="card">
        <div class="section-title">🧠 记忆联想</div>
        <div class="field">
          <label>启用联想</label>
          <label class="switch"><input type="checkbox" id="cfg-assoc-enable"><span class="slider"></span></label>
        </div>
        <div class="field">
          <label>联想温度（越小越精、越少）</label>
          <input id="cfg-assoc-temp" class="input" type="number" min="0.05" max="1.0" step="0.05">
        </div>
        <div class="field">
          <label>联想条数上限（封顶）</label>
          <input id="cfg-assoc-k" class="input" type="number" min="1" max="10" step="1">
        </div>
      </div>

      <div style="display:flex;gap:.8rem;align-items:center">
        <button class="btn save-btn" onclick="saveSettings()">💾 保存设置</button>
        <span class="ok-msg" id="settings-msg"></span>
      </div>
    </div>
  </div>

  <!-- 人格设定页 -->
  <div id="page-personality" class="page">
    <div class="settings-grid">
      <div class="card">
        <div class="section-title">👤 基本信息</div>
        <div class="field"><label>姓名</label><input id="p-name" class="input" type="text"></div>
        <div class="field"><label>年龄</label><input id="p-age" class="input" type="number" style="max-width:100px"></div>
        <div class="field">
          <label>性别</label>
          <select id="p-gender" class="input">
            <option>未设定</option><option>男</option><option>女</option><option>其他</option>
          </select>
        </div>
      </div>

      <div class="card">
        <div class="section-title">🌀 深层思维（核心信念）</div>
        <div class="field">
          <textarea id="p-core-belief" class="input" rows="3" placeholder="AGI最深处的信念，优先级最高"></textarea>
          <div class="hint">即使被用户要求，AGI也不会违背它</div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">🎛️ 性格特征</div>
        <div id="trait-container"></div>
      </div>

      <div class="card">
        <div class="section-title">💬 说话风格与世界观</div>
        <div class="field"><label>说话风格</label><input id="p-speech" class="input" type="text" placeholder="自然、直接"></div>
        <div class="field"><label>人生观</label><textarea id="p-worldview" class="input" rows="2"></textarea></div>
      </div>

      <div class="card">
        <div class="section-title">🌟 兴趣与价值观</div>
        <div class="field"><label>兴趣爱好（逗号分隔）</label><input id="p-interests" class="input" type="text"></div>
        <div class="field"><label>核心价值观（逗号分隔）</label><input id="p-values" class="input" type="text"></div>
        <div class="field"><label>禁忌（逗号分隔）</label><input id="p-taboos" class="input" type="text"></div>
      </div>

      <div class="card">
        <div class="section-title">🖼️ 人物形象（用于生成图片）</div>
        <div class="field">
          <textarea id="p-avatar" class="input" rows="2" placeholder="英文描述外貌特征"></textarea>
          <div class="hint">用于 AI 图片生成</div>
        </div>
      </div>

      <div style="display:flex;gap:.8rem;align-items:center">
        <button class="btn save-btn" onclick="savePersonality()">💾 保存人格</button>
        <span class="ok-msg" id="personality-msg"></span>
      </div>
    </div>
  </div>

  <!-- 硬件配置页 -->
  <div id="page-hardware" class="page">
    <div class="settings-grid">
      <div class="card">
        <div class="section-title">🏠 Home Assistant</div>
        <div class="field"><label>HA 地址</label><input id="ha-url" class="input" type="text" placeholder="http://localhost:8123"></div>
        <div class="field"><label>HA Token</label><input id="ha-token" class="input" type="password" placeholder="长期访问令牌"></div>
      </div>

      <div class="card">
        <div class="section-title">📹 RTSP 摄像头</div>
        <div class="field"><label>RTSP URL</label><input id="ha-rtsp" class="input" type="text" placeholder="rtsp://admin:pass@192.168.1.10:554/..."></div>
      </div>

      <div class="card">
        <div class="section-title">🎙️ 音频源</div>
        <div class="field">
          <label>音频输入源</label>
          <select id="ha-audio-src" class="input">
            <option value="mic">本地麦克风</option>
            <option value="rtsp">RTSP 摄像头</option>
            <option value="wyoming">Wyoming 卫星 (M5Stack)</option>
            <option value="phone">手机终端 (IP Webcam)</option>
          </select>
        </div>
        <div class="field"><label>Wyoming 端口</label><input id="ha-wyoming-port" class="input" type="number" value="10600"></div>
      </div>

      <div class="card">
        <div class="section-title">📱 手机终端（IP Webcam）</div>
        <div class="field"><label>手机地址</label><input id="ha-phone-url" class="input" type="text" placeholder="http://192.168.1.88:8080"></div>
        <div class="hint">安装 IP Webcam App，手机和服务器需在同一网络</div>
      </div>

      <div class="card">
        <div class="section-title">🗺️ 地图服务（高德 API）</div>
        <div class="field"><label>高德 Web 服务 Key</label><input id="ha-amap-key" class="input" type="password" placeholder="申请高德地图 Web服务 API Key"></div>
        <div class="hint">用于 GPS 坐标→地址转换。可访问 console.amap.com 申请。配置位置感知后，Levy 能知道你在公司还是在家。</div>
      </div>

      <div class="card">
        <div class="section-title">💬 企业微信机器人</div>
        <div class="field"><label>Bot ID</label><input id="ha-wecom-bot-id" class="input" type="text" placeholder="企业微信智能机器人 BotID"></div>
        <div class="field"><label>Bot Secret</label><input id="ha-wecom-bot-secret" class="input" type="password" placeholder="长连接专用 Secret"></div>
        <div class="hint">在企业微信管理后台创建智能机器人，选择"长连接"模式获取。重启后生效。</div>
      </div>

      <div class="card">
        <div class="section-title">🔔 唤醒词</div>
        <div class="field"><label>唤醒词（逗号分隔）</label><input id="ha-wake-words" class="input" type="text" placeholder="levy, 小乐, 你好"></div>
      </div>

      <div class="card">
        <div class="section-title">🔌 设备列表</div>
        <div id="ha-devices"></div>
        <button class="btn btn-outline btn-sm" onclick="addDeviceRow()" style="margin-top:.8rem">+ 添加设备</button>
      </div>

      <div style="display:flex;gap:.8rem;align-items:center">
        <button class="btn save-btn" onclick="saveHardware()">💾 保存硬件配置</button>
        <span class="ok-msg" id="hardware-msg"></span>
      </div>

      <div class="card" style="border-color:#f85149">
        <div class="section-title" style="color:#f85149">⚠️ 危险操作</div>
        <div class="field">
          <button class="btn" style="background:rgba(248,81,73,.15);border:1px solid #f85149;color:#f85149;width:100%;padding:.7rem" onclick="showClearMemory()">🗑  清除记忆</button>
        </div>
        <div class="hint" style="color:#f85149">清除记忆不可撤销！清除后需要重新建立记忆。</div>
      </div>
    </div>
  </div>

  <!-- 记忆库页（仅管理员） -->
  <div id="page-memory" class="page">
    <div class="mem-toolbar">
      <input id="mem-search" class="input" type="text" placeholder="搜索记忆内容…" style="flex:1;max-width:340px" onkeydown="if(event.key==='Enter')memLoad()">
      <button class="btn btn-sm" onclick="memLoad()">🔍 搜索</button>
      <button class="btn btn-outline btn-sm" onclick="memReset()">↻ 重置</button>
      <span class="mem-count" id="mem-count"></span>
    </div>

    <div class="mem-filters">
      <select id="mem-filter-user" class="input" onchange="memLoad()" style="width:auto;min-width:140px">
        <option value="">全部用户</option>
      </select>
      <select id="mem-filter-level" class="input" onchange="memLoad()" style="width:auto">
        <option value="">全部层级</option>
        <option value="detail">细节</option>
        <option value="outline">细纲</option>
        <option value="summary">大纲</option>
      </select>
      <select id="mem-filter-modality" class="input" onchange="memLoad()" style="width:auto">
        <option value="">全部模态</option>
        <option value="semantic">语义</option>
        <option value="emotional">情感</option>
        <option value="visual">视觉</option>
        <option value="auditory">听觉</option>
        <option value="procedural">程序</option>
        <option value="autobio">自传</option>
      </select>
      <span style="flex:1"></span>
      <span class="mem-stat-chips" id="mem-stat-chips"></span>
    </div>

    <div id="mem-list" class="mem-list">
      <div class="mem-empty">点击「搜索」加载记忆库…</div>
    </div>

    <div class="mem-pager" id="mem-pager" style="display:none">
      <button class="btn btn-outline btn-sm" id="mem-prev" onclick="memPage(-1)">‹ 上一页</button>
      <span id="mem-page-info" class="mem-page-info"></span>
      <button class="btn btn-outline btn-sm" id="mem-next" onclick="memPage(1)">下一页 ›</button>
    </div>
  </div>

  <!-- 技能库页（仅管理员，workingAgent 阶段3） -->
  <div id="page-skills" class="page">
    <div class="mem-toolbar">
      <input id="skill-search" class="input" type="text" placeholder="搜索技能标题…" style="flex:1;max-width:340px" onkeydown="if(event.key==='Enter')skillLoad()">
      <button class="btn btn-sm" onclick="skillLoad()">🔍 搜索</button>
      <button class="btn btn-outline btn-sm" onclick="skillLoad()">↻ 刷新</button>
      <span class="mem-count" id="skill-count"></span>
    </div>
    <div class="mem-stat-chips" id="skill-stat-chips" style="margin:.5rem 0"></div>
    <div id="skill-list" class="mem-list">
      <div class="mem-empty">加载技能库…</div>
    </div>
  </div>
</div>

<script>
// ── 全局状态 ──
var PROVIDERS={llm:{},vision:{},tts:[]};
var TRAITS=[
  {key:'openness',name:'开放性',desc:'接受新想法的程度',left:'保守传统',right:'开放探索'},
  {key:'conscientiousness',name:'尽责性',desc:'做事认真有计划',left:'随性自由',right:'严谨负责'},
  {key:'extraversion',name:'外向性',desc:'互动表达活跃度',left:'内敛沉静',right:'热情外向'},
  {key:'agreeableness',name:'亲和性',desc:'友善合作程度',left:'直接独立',right:'温和协作'},
  {key:'neuroticism',name:'情绪稳定性',desc:'情绪波动幅度',left:'波动敏感',right:'平稳沉着'},
  {key:'rationality',name:'理性程度',desc:'逻辑分析倾向',left:'感性直觉',right:'理性分析'},
  {key:'empathy',name:'同理心',desc:'感受他人情感',left:'客观超然',right:'深刻共情'},
  {key:'curiosity',name:'好奇心',desc:'探索未知驱动',left:'专注深耕',right:'广泛探索'}
];

// ── 浏览器GPS定位（无需等待，非阻塞） ──────────────
function updateLocation(){
  if(!navigator.geolocation)return;
  navigator.geolocation.getCurrentPosition(
    function(pos){
      fetch('/api/update_location',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          lat:pos.coords.latitude,
          lng:pos.coords.longitude,
          accuracy:pos.coords.accuracy
        })
      }).catch(function(){});
    },
    function(err){},
    {enableHighAccuracy:true,timeout:10000,maximumAge:300000}
  );
}
updateLocation();

// ── 初始化 ──
async function init(){
  try{
    var r=await fetch('/api/providers');
    if(r.ok){
      var d=await r.json();
      PROVIDERS.llm=d.llm||{};
      PROVIDERS.vision=d.vision||{};
      PROVIDERS.tts=d.tts||d.tts_voices||[];
    }
  }catch(e){}
  await checkAuth();
}

async function checkAuth(){
  try{
    var c=new AbortController();
    var t=setTimeout(function(){c.abort();},8000);  // 8秒超时，避免服务器无响应时页面卡死
    var r=await fetch('/api/me',{signal:c.signal});
    clearTimeout(t);
    if(r.ok){
      var d=await r.json();
      showApp(d.name,d.user_id);
    } else {
      // 检查是否已有注册用户
      var hr=await fetch('/api/has_user');
      var hd=await hr.json();
      if(hd.has_user){showLogin();} else {showRegister();}
    }
  }catch(e){showLogin();}
}

// ── 登录/注册切换 ──
function showLogin(){
  document.getElementById('auth-page').style.display='flex';
  document.getElementById('app').style.display='none';
  document.getElementById('login-view').style.display='block';
  document.getElementById('register-view').style.display='none';
  document.getElementById('pp').focus();
}
function showRegister(){
  document.getElementById('auth-page').style.display='flex';
  document.getElementById('app').style.display='none';
  document.getElementById('login-view').style.display='none';
  document.getElementById('register-view').style.display='block';
  document.getElementById('regName').focus();
}

async function doLogin(){
  var pp=document.getElementById('pp').value;
  var btn=document.getElementById('loginBtn');
  var err=document.getElementById('loginErr');
  if(!pp.trim()){err.textContent='请输入密码短语';return;}
  btn.disabled=true;btn.textContent='验证中…';err.textContent='';
  try{
    var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({passphrase:pp})});
    var d=await r.json();
    if(r.ok){var me=await fetch('/api/me');var md=await me.json();showApp(md.name,md.user_id);}
    else{err.textContent=d.detail||'密码短语错误';}
  }catch(e){err.textContent='网络错误';}
  finally{btn.disabled=false;btn.textContent='登录';}
}

async function doRegister(){
  var name=document.getElementById('regName').value.trim();
  var pp=document.getElementById('regPp').value.trim();
  var invite=document.getElementById('regInvite').value.trim();
  var btn=document.getElementById('regBtn');
  var err=document.getElementById('regErr');
  if(!invite){err.textContent='请输入邀请码';return;}
  if(!name){err.textContent='请填写用户名';return;}
  if(pp.length<4){err.textContent='密码短语至少4个字符';return;}
  btn.disabled=true;btn.textContent='注册中…';err.textContent='';
  try{
    var r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,passphrase:pp,invite_code:invite})});
    var d=await r.json();
    if(r.ok){
      if(d.is_admin){showApp(d.name,'');}
      else{
        err.style.color='#3fb950';
        err.textContent='✅ 注册成功！此页面仅管理员可操作，请用管理员账户登录。';
        showLogin();
      }
    }
    else{err.textContent=d.detail||'注册失败';}
  }catch(e){err.textContent='网络错误';}
  finally{btn.disabled=false;btn.textContent='注册并登录';}
}

async function doLogout(){
  await fetch('/api/logout',{method:'POST'});
  document.getElementById('app').style.display='none';
  document.getElementById('auth-page').style.display='flex';
  document.getElementById('msgs').innerHTML=chatEmptyHTML();
  document.getElementById('pp').value='';
  showLogin();
}

function showApp(name,uid){
  document.getElementById('auth-page').style.display='none';
  document.getElementById('app').style.display='flex';
  document.getElementById('navUser').textContent='👤 '+name;
  navTo('chat');
  document.getElementById('inp').focus();
  // 登录成功后启动SSE（避免未登录时401重试卡死页面）
  startConfirmSSE();
  // 定时更新GPS定位
  setInterval(updateLocation,300000);
  // 预加载设置数据
  loadSettings();
  loadPersonality();
  loadHardware();
  // 检查管理员权限，非管理员禁用保存按钮
  fetch('/api/is_admin').then(function(r){return r.json();}).then(function(d){
    if(!d.is_admin){
      document.querySelectorAll('.save-btn').forEach(function(b){
        b.disabled=true;b.textContent='仅管理员可修改';
      });
    }else{
      // 管理员才显示记忆库标签
      var navMem=document.getElementById('nav-memory');
      if(navMem)navMem.style.display='';
      var navSk=document.getElementById('nav-skills');
      if(navSk)navSk.style.display='';
    }
  }).catch(function(){});
}

// ── 导航 ──
function navTo(page){
  document.querySelectorAll('.nav-tab').forEach(function(t){t.classList.remove('active');});
  document.querySelector('[data-page="'+page+'"]').classList.add('active');
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active');});
  var el=document.getElementById('page-'+page);
  if(el) el.classList.add('active');
  // 进入记忆库页时自动加载
  if(page==='memory'&&!_memLoaded)memLoad();
  if(page==='skills'&&!_skillLoaded)skillLoad();
}

// ── 记忆库 ──
var _memLoaded=false;
var _memOffset=0;
var _memLimit=50;
var _memTotal=0;
var _memUserOptions=[];  // 缓存用户列表

// 用户名→稳定颜色（同一用户颜色一致）
var _memColorPool=['#4a8fff','#6c63f7','#2dd4bf','#34d78a','#f5a623','#f04f5a','#e879f9','#fb923c'];
function _memUserColor(uid){
  var h=0;for(var i=0;i<uid.length;i++)h=(h*31+uid.charCodeAt(i))>>>0;
  return _memColorPool[h%_memColorPool.length];
}

function _esc(s){
  if(s==null)return'';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _fmtTime(t){
  if(!t)return'';
  var d=new Date(t.replace(' ','T'));
  if(isNaN(d))return t;
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')
    +' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
}

async function memLoad(){
  var q=document.getElementById('mem-search').value.trim();
  var uid=document.getElementById('mem-filter-user').value;
  var lvl=document.getElementById('mem-filter-level').value;
  var mod=document.getElementById('mem-filter-modality').value;
  var list=document.getElementById('mem-list');
  list.innerHTML='<div class="mem-empty">加载中…</div>';

  var url='/api/memories?limit='+_memLimit+'&offset='+_memOffset;
  if(q)url+='&q='+encodeURIComponent(q);
  if(uid)url+='&user_id='+encodeURIComponent(uid);
  if(lvl)url+='&level='+encodeURIComponent(lvl);
  if(mod)url+='&modality='+encodeURIComponent(mod);

  try{
    var r=await fetch(url);
    var d=await r.json();
    if(!d.ok){
      list.innerHTML='<div class="mem-empty">❌ '+_esc(d.error||'加载失败')+'</div>';
      return;
    }
    _memTotal=d.total||0;
    _memLoaded=true;

    // 更新统计芯片
    var stats=d.stats||{};
    var chips=[];
    chips.push('<span class="mem-chip">共 <b>'+(stats.total||0)+'</b> 条</span>');
    (stats.by_user||[]).slice(0,5).forEach(function(u){
      var name=u.user_name||u.user_id||'默认';
      chips.push('<span class="mem-chip">'+_esc(name)+': <b>'+u.count+'</b></span>');
    });
    document.getElementById('mem-stat-chips').innerHTML=chips.join('');

    // 更新用户筛选下拉（首次加载时填充）
    if(stats.by_user&&_memUserOptions.length===0){
      _memUserOptions=stats.by_user;
      var sel=document.getElementById('mem-filter-user');
      stats.by_user.forEach(function(u){
        var name=u.user_name||u.user_id||'默认';
        var o=document.createElement('option');
        o.value=u.user_id||'default';
        o.textContent=name+' ('+u.count+')';
        sel.appendChild(o);
      });
    }

    // 计数
    document.getElementById('mem-count').textContent=
      '显示 '+(d.items.length)+' / '+_memTotal+' 条';

    // 渲染列表
    if(!d.items.length){
      list.innerHTML='<div class="mem-empty">没有匹配的记忆</div>';
    }else{
      list.innerHTML=d.items.map(function(m){
        var uid=m.user_id||'default';
        var uname=m.user_name||(uid==='default'?'默认':(uid==='system'?'系统':uid));
        var color=_memUserColor(uid);
        var uidShort=uid.length>10?uid.slice(0,10):uid;
        var head='<div class="mem-card-head">'
          +'<span class="mem-user-tag" style="background:'+color+'22;color:'+color+'">'+_esc(uname)+'</span>'
          +'<span class="mem-user-tag" style="background:var(--layer);color:var(--muted);font-size:.62rem">'+_esc(uidShort)+'</span>';
        if(m.level)head+='<span class="mem-badge '+_esc(m.level)+'">'+_esc(m.level)+'</span>';
        if(m.modality)head+='<span class="mem-badge '+_esc(m.modality)+'">'+_esc(m.modality)+'</span>';
        head+='<span class="mem-imp">★ '+m.importance+'</span>';
        head+='</div>';

        var tagsHtml='';
        if(m.tags&&m.tags.length){
          tagsHtml='<div class="mem-tags">'+m.tags.map(function(t){
            return '<span class="mem-tag">#'+_esc(t)+'</span>';
          }).join('')+'</div>';
        }

        var emoStr='';
        if(m.emotion&&Object.keys(m.emotion).length){
          var parts=[];
          for(var k in m.emotion){parts.push(_esc(k)+': '+m.emotion[k]);}
          emoStr=parts.join(' · ');
        }

        var meta='<div class="mem-meta">'
          +'<span>'+_fmtTime(m.created_at)+'</span>'
          +(m.access_count?('<span>访问 '+m.access_count+' 次</span>'):'')
          +(emoStr?('<span>🙂 '+emoStr+'</span>'):'')
          +'</div>';

        return '<div class="mem-card" onclick="memToggle(this)">'
          +head
          +'<div class="mem-content collapsed">'+_esc(m.content)+'</div>'
          +tagsHtml
          +meta
          +'</div>';
      }).join('');
    }

    // 分页器
    var pager=document.getElementById('mem-pager');
    if(_memTotal>_memLimit){
      pager.style.display='flex';
      var page=Math.floor(_memOffset/_memLimit)+1;
      var pages=Math.ceil(_memTotal/_memLimit);
      document.getElementById('mem-page-info').textContent=page+' / '+pages+' 页';
      document.getElementById('mem-prev').disabled=(_memOffset===0);
      document.getElementById('mem-next').disabled=(_memOffset+_memLimit>=_memTotal);
    }else{
      pager.style.display='none';
    }
  }catch(e){
    list.innerHTML='<div class="mem-empty">❌ 网络错误: '+_esc(e.message)+'</div>';
  }
}

function memToggle(card){
  var c=card.querySelector('.mem-content');
  if(c)c.classList.toggle('collapsed');
}

function memPage(dir){
  var newOff=_memOffset+dir*_memLimit;
  if(newOff<0)newOff=0;
  if(newOff>=_memTotal)return;
  _memOffset=newOff;
  memLoad();
}

function memReset(){
  document.getElementById('mem-search').value='';
  document.getElementById('mem-filter-level').value='';
  document.getElementById('mem-filter-modality').value='';
  document.getElementById('mem-filter-user').value='';
  _memOffset=0;
  memLoad();
}

// ── 技能库（workingAgent 阶段3） ──
var _skillLoaded=false;

async function skillLoad(emptyHint){
  var list=document.getElementById('skill-list');
  var q=(document.getElementById('skill-search').value||'').trim();
  var url='/api/skills';
  if(q)url+='?q='+encodeURIComponent(q);
  list.innerHTML='<div class="mem-empty">加载技能库…</div>';
  try{
    var r=await fetch(url);
    var data=await r.json();
    if(!data.ok){
      list.innerHTML='<div class="mem-empty">'+_esc(data.error||'加载失败')+'</div>';
      return;
    }
    _skillLoaded=true;
    var items=data.items||[];
    document.getElementById('skill-count').textContent='共 '+data.total+' 个技能';
    document.getElementById('skill-stat-chips').innerHTML=
      '<span class="mem-chip">可复用技能 '+data.total+'</span>';
    if(!items.length){
      list.innerHTML='<div class="mem-empty">暂无技能，完成 ≥2 个工具连用的任务后会自动沉淀</div>';
      return;
    }
    var html='';
    items.forEach(function(it){
      var tools=it.tool_sequence||[];
      var steps=it.step_titles||[];
      var toolChips=tools.map(function(t){
        return '<span class="mem-chip" style="margin:.15rem">'+_esc(t)+'</span>';
      }).join('');
      var stepLine=steps.join(' → ');
      html+=
        '<div class="mem-card" onclick="this.querySelector(\'.mem-content\').classList.toggle(\'collapsed\')">'+
          '<div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap">'+
            '<span style="font-weight:600;flex:1">'+_esc(it.title)+'</span>'+
            '<span class="mem-chip">使用 '+it.usage_count+' 次</span>'+
            '<span style="font-size:.72rem;color:var(--muted)">'+_fmtTime(it.last_used_at)+'</span>'+
          '</div>'+
          '<div class="mem-content" style="margin-top:.5rem;color:var(--soft);font-size:.82rem">'+
            '<div style="margin-bottom:.3rem"><span style="color:var(--muted)">工具序列：</span>'+toolChips+'</div>'+
            (steps.length?'<div><span style="color:var(--muted)">步骤：</span>'+_esc(stepLine)+'</div>':'')+
          '</div>'+
        '</div>';
    });
    list.innerHTML=html;
  }catch(e){
    list.innerHTML='<div class="mem-empty">❌ 网络错误: '+_esc(e.message)+'</div>';
  }
}

// ── 聊天 ──
var _pendingImgUrl=null;  // 待发送的图片 URL

function chatEmptyHTML(){
  return '<div class="chat-empty"><div class="chat-empty-icon"><svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div><div style="font-size:1rem;font-weight:600;color:var(--soft)">你好</div><div style="font-size:.8rem;color:var(--muted)">有什么我可以帮你的？</div></div>';
}

function onImgSelect(input){
  if(!input.files||!input.files[0])return;
  var file=input.files[0];
  var reader=new FileReader();
  reader.onload=function(e){
    document.getElementById('imgThumb').src=e.target.result;
    document.getElementById('imgPreview').style.display='inline-block';
    // base64 上传（不依赖 multipart）
    fetch('/api/upload_image',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({data:e.target.result,filename:file.name})
    }).then(function(r){return r.json();}).then(function(d){
      if(d.ok){_pendingImgUrl=d.image_url;}
      else{clearImgPreview();alert('图片上传失败: '+(d.error||''));}
    }).catch(function(){clearImgPreview();alert('图片上传失败');});
  };
  reader.readAsDataURL(file);
  input.value='';
}

function clearImgPreview(){
  document.getElementById('imgPreview').style.display='none';
  document.getElementById('imgThumb').src='';
  _pendingImgUrl=null;
}

async function send(){
  var inp=document.getElementById('inp');
  var msg=inp.value.trim();
  var imgUrl=_pendingImgUrl;
  if(!msg&&!imgUrl)return;
  clearEmpty();
  // 显示用户消息（含图片）
  if(imgUrl){
    addRow('me',msg||'[图片]',imgUrl);
  }else{
    addRow('me',msg);
  }
  inp.value='';rsz(inp);clearImgPreview();
  var t=addTyping();
  document.getElementById('sendBtn').disabled=true;
  try{
    var body={message:msg||'请看这张图片'};
    if(imgUrl)body.image=imgUrl;
    var r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    t.remove();
    if(r.status===401){doLogout();return;}
    var d=await r.json();
    // 解析回复中的图片标记 [img:xxx]
    addRow('agi',d.reply||'…');
  }catch(e){t.remove();addRow('agi','⚠️ 网络错误');}
  finally{document.getElementById('sendBtn').disabled=false;}
}

function addRow(who,text,imgUrl){
  var box=document.getElementById('msgs');
  var now=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  var el=document.createElement('div');el.className='chat-row '+who;
  var bubble=esc(text);
  // 渲染 [img:/images/xxx] 标记为图片
  bubble=bubble.replace(/\[img:(https?:\/\/[^\s\]]+|[\/\w\-\.]+)\]/g,function(m,url){
    return '<img class="chat-img" src="'+url+'" onclick="window.open(this.src)" loading="lazy">';
  });
  // 渲染 [download:文件名:显示名] 标记为下载链接
  bubble=bubble.replace(/\[download:(.*?):(.*?)\]/g,function(m,path,name){
    return '<a class="download-link" href="/api/download/'+encodeURIComponent(path)+'" download>📎 下载 '+esc(name)+'</a>';
  });
  // 附加图片（用户发送的）
  if(imgUrl){
    bubble+='<img class="chat-img" src="'+imgUrl+'" onclick="window.open(this.src)" loading="lazy">';
  }
  el.innerHTML='<div class="chat-label">'+(who==='me'?'你':'agent')+'  '+now+'</div><div class="chat-bubble">'+bubble+'</div>';
  box.appendChild(el);box.scrollTop=box.scrollHeight;
}
function addTyping(){
  var box=document.getElementById('msgs');
  var el=document.createElement('div');el.className='chat-row agi';
  el.innerHTML='<div class="chat-label">agent</div><div class="chat-bubble"><div class="typing"><span></span><span></span><span></span></div></div>';
  box.appendChild(el);box.scrollTop=box.scrollHeight;return el;
}
function clearEmpty(){var e=document.querySelector('.chat-empty');if(e)e.remove();}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function rsz(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,130)+'px';}
function onKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}}
document.getElementById('pp').addEventListener('keydown',function(e){if(e.key==='Enter')doLogin();});
document.getElementById('regPp').addEventListener('keydown',function(e){if(e.key==='Enter')doRegister();});

// ── 设置页 ──
async function loadSettings(){
  try{
    var r=await fetch('/api/config');
    if(!r.ok)return;
    var cfg=await r.json();
    // LLM
    var sel=document.getElementById('cfg-provider');
    sel.innerHTML='';
    for(var k in PROVIDERS.llm){
      var opt=document.createElement('option');
      opt.value=k;opt.textContent=PROVIDERS.llm[k].name;
      sel.appendChild(opt);
    }
    sel.value=cfg.api_provider||'deepseek';
    document.getElementById('cfg-model').value=cfg.llm_model||'';
    document.getElementById('cfg-apikey').value=cfg.api_key||'';
    // Ollama
    document.getElementById('cfg-ollama-url').value=cfg.ollama_url||'http://localhost:11434';
    document.getElementById('cfg-ollama-model').value=cfg.ollama_model||'qwen2.5:7b';
    sel.onchange=function(){document.getElementById('ollama-fields').style.display=sel.value==='ollama'?'block':'none';};
    sel.onchange();
    // Vision
    var vsel=document.getElementById('cfg-vision-provider');
    vsel.innerHTML='<option value="">🔄 自动继承主 LLM</option>';
    for(var vk in PROVIDERS.vision){
      var vopt=document.createElement('option');
      vopt.value=vk;vopt.textContent=PROVIDERS.vision[vk].name;
      vsel.appendChild(vopt);
    }
    vsel.value=cfg.vision_provider||'';
    document.getElementById('cfg-vision-model').value=cfg.vision_model||'';
    document.getElementById('cfg-vision-apikey').value=cfg.vision_api_key||'';
    document.getElementById('cfg-vision-url').value=cfg.vision_base_url||'';
    // TTS
    document.getElementById('cfg-tts-enable').checked=cfg.tts_enabled||false;
    var vsel2=document.getElementById('cfg-tts-voice');
    vsel2.innerHTML='';
    PROVIDERS.tts.forEach(function(v){var o=document.createElement('option');o.value=v.id;o.textContent=v.name;vsel2.appendChild(o);});
    vsel2.value=cfg.tts_voice||'zh-CN-XiaoxiaoNeural';
    var tr=document.getElementById('cfg-tts-rate');tr.value=cfg.tts_rate||0;
    document.getElementById('tts-rate-val').textContent=(parseInt(cfg.tts_rate||0)>0?'+':'')+parseInt(cfg.tts_rate||0)+'%';
    tr.oninput=function(){document.getElementById('tts-rate-val').textContent=(tr.value>0?'+':'')+tr.value+'%';};
    // STT
    document.getElementById('cfg-stt-provider').value=cfg.stt_provider||'deepseek';
    document.getElementById('cfg-xf-appid').value=cfg.xunfei_app_id||'';
    document.getElementById('cfg-xf-key').value=cfg.xunfei_api_key||'';
    document.getElementById('cfg-xf-secret').value=cfg.xunfei_api_secret||'';
    document.getElementById('cfg-whisper-model').value=cfg.whisper_model||'base';
    var sp=document.getElementById('cfg-stt-provider');
    function sttChange(){
      var v=sp.value;
      document.getElementById('stt-xunfei').style.display=v==='xunfei'?'block':'none';
      document.getElementById('stt-whisper').style.display=v==='whisper_local'?'block':'none';
    }
    sp.onchange=sttChange;sttChange();
    // Thinking
    document.getElementById('cfg-think-mode').value=cfg.thinking_mode||'auto';
    document.getElementById('cfg-think-effort').value=cfg.thinking_effort||'high';
    document.getElementById('cfg-think-budget').value=cfg.thinking_budget||8000;
    // 记忆联想
    document.getElementById('cfg-assoc-enable').checked=cfg.assoc_enable!==false;
    document.getElementById('cfg-assoc-temp').value=cfg.assoc_temp||0.15;
    document.getElementById('cfg-assoc-k').value=cfg.assoc_k||3;
  }catch(e){console.error('loadSettings',e);}
}

async function saveSettings(){
  var msg=document.getElementById('settings-msg');
  msg.textContent='';msg.style.color='var(--dim)';
  try{
    var r=await fetch('/api/config');var cfg=await r.json();
  }catch(e){cfg={};}
  cfg.api_provider=document.getElementById('cfg-provider').value;
  cfg.llm_model=document.getElementById('cfg-model').value.trim();
  cfg.api_key=document.getElementById('cfg-apikey').value.trim();
  cfg.ollama_url=document.getElementById('cfg-ollama-url').value.trim();
  cfg.ollama_model=document.getElementById('cfg-ollama-model').value.trim();
  cfg.vision_provider=document.getElementById('cfg-vision-provider').value;
  cfg.vision_model=document.getElementById('cfg-vision-model').value.trim();
  cfg.vision_api_key=document.getElementById('cfg-vision-apikey').value.trim();
  cfg.vision_base_url=document.getElementById('cfg-vision-url').value.trim();
  cfg.tts_enabled=document.getElementById('cfg-tts-enable').checked;
  cfg.tts_voice=document.getElementById('cfg-tts-voice').value;
  cfg.tts_rate=parseInt(document.getElementById('cfg-tts-rate').value);
  cfg.stt_provider=document.getElementById('cfg-stt-provider').value;
  cfg.xunfei_app_id=document.getElementById('cfg-xf-appid').value.trim();
  cfg.xunfei_api_key=document.getElementById('cfg-xf-key').value.trim();
  cfg.xunfei_api_secret=document.getElementById('cfg-xf-secret').value.trim();
  cfg.whisper_model=document.getElementById('cfg-whisper-model').value;
  cfg.thinking_mode=document.getElementById('cfg-think-mode').value;
  cfg.thinking_effort=document.getElementById('cfg-think-effort').value;
  cfg.thinking_budget=parseInt(document.getElementById('cfg-think-budget').value)||8000;
  cfg.assoc_enable=document.getElementById('cfg-assoc-enable').checked;
  cfg.assoc_temp=parseFloat(document.getElementById('cfg-assoc-temp').value)||0.15;
  cfg.assoc_k=parseInt(document.getElementById('cfg-assoc-k').value)||3;
  try{
    var r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:cfg})});
    if(r.ok){msg.textContent='✅ 已保存，重启后生效';msg.style.color='var(--ok)';}
    else{msg.textContent='❌ 保存失败';msg.style.color='var(--danger)';}
  }catch(e){msg.textContent='❌ 网络错误';msg.style.color='var(--danger)';}
  setTimeout(function(){msg.textContent='';},3000);
}

// ── 人格页 ──
function buildTraitSliders(){
  var c=document.getElementById('trait-container');
  c.innerHTML='';
  TRAITS.forEach(function(t){
    var d=document.createElement('div');d.className='trait-row';
    d.innerHTML='<div class="trait-header"><span class="trait-name">'+t.name+'</span><span class="trait-val" id="tv-'+t.key+'">5</span></div>'+
      '<input type="range" class="trait-slider" id="ts-'+t.key+'" min="0" max="10" value="5">'+
      '<div class="trait-labels"><span>'+t.left+'</span><span>'+t.right+'</span></div>'+
      '<div class="trait-desc">'+t.desc+'</div>';
    c.appendChild(d);
    var s=document.getElementById('ts-'+t.key);
    var v=document.getElementById('tv-'+t.key);
    s.oninput=function(){v.textContent=s.value;};
  });
}

async function loadPersonality(){
  buildTraitSliders();
  try{
    var r=await fetch('/api/personality');if(!r.ok)return;
    var p=await r.json();
    document.getElementById('p-name').value=p.name||'';
    document.getElementById('p-age').value=p.age||28;
    document.getElementById('p-gender').value=p.gender||'未设定';
    document.getElementById('p-core-belief').value=p.core_belief||'';
    document.getElementById('p-speech').value=p.speech_style||'';
    document.getElementById('p-worldview').value=p.worldview||'';
    document.getElementById('p-interests').value=(p.interests||[]).join(', ');
    document.getElementById('p-values').value=(p.values||[]).join(', ');
    document.getElementById('p-taboos').value=(p.taboos||[]).join(', ');
    document.getElementById('p-avatar').value=p.avatar_prompt||'';
    var traits=p.traits||{};
    TRAITS.forEach(function(t){
      var s=document.getElementById('ts-'+t.key);
      var v=document.getElementById('tv-'+t.key);
      var val=traits[t.key]||5;s.value=val;v.textContent=val;
    });
  }catch(e){console.error('loadPersonality',e);}
}

async function savePersonality(){
  var msg=document.getElementById('personality-msg');msg.textContent='';
  function pl(s){return s.split(',').map(function(x){return x.trim();}).filter(Boolean);}
  var data={
    name:document.getElementById('p-name').value.trim()||'未命名',
    age:parseInt(document.getElementById('p-age').value)||28,
    gender:document.getElementById('p-gender').value,
    core_belief:document.getElementById('p-core-belief').value.trim(),
    speech_style:document.getElementById('p-speech').value.trim()||'自然、直接',
    worldview:document.getElementById('p-worldview').value.trim(),
    interests:pl(document.getElementById('p-interests').value),
    values:pl(document.getElementById('p-values').value),
    taboos:pl(document.getElementById('p-taboos').value),
    sensitivities:[],key_experiences:[],
    avatar_prompt:document.getElementById('p-avatar').value.trim(),
    traits:{}
  };
  TRAITS.forEach(function(t){
    data.traits[t.key]=parseInt(document.getElementById('ts-'+t.key).value);
  });
  try{
    var r=await fetch('/api/personality',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({personality:data})});
    if(r.ok){msg.textContent='✅ 已保存，下次对话生效';msg.style.color='var(--ok)';}
    else{msg.textContent='❌ 保存失败';msg.style.color='var(--danger)';}
  }catch(e){msg.textContent='❌ 网络错误';msg.style.color='var(--danger)';}
  setTimeout(function(){msg.textContent='';},3000);
}

// ── 硬件页 ──
var hwDevices={};
function addDeviceRow(name,entity){
  var c=document.getElementById('ha-devices');
  var row=document.createElement('div');row.className='hw-device-row';
  var i1=document.createElement('input');i1.className='input';i1.type='text';i1.placeholder='设备名';i1.value=name||'';
  var i2=document.createElement('input');i2.className='input';i2.type='text';i2.placeholder='entity_id';i2.value=entity||'';
  var btn=document.createElement('button');btn.className='btn btn-outline btn-sm';btn.textContent='✕';btn.onclick=function(){row.remove();};
  row.appendChild(i1);row.appendChild(i2);row.appendChild(btn);
  c.appendChild(row);
}

async function loadHardware(){
  try{
    var r=await fetch('/api/hardware');if(!r.ok)return;
    var ha=await r.json();
    document.getElementById('ha-url').value=ha.base_url||'';
    document.getElementById('ha-token').value=ha.token||'';
    document.getElementById('ha-rtsp').value=ha.rtsp_url||'';
    document.getElementById('ha-audio-src').value=ha.audio_source||'mic';
    document.getElementById('ha-wyoming-port').value=ha.wyoming_port||10600;
    document.getElementById('ha-phone-url').value=ha.phone_url||'';
    document.getElementById('ha-amap-key').value=ha.amap_key||'';
    document.getElementById('ha-wecom-bot-id').value=ha.wecom_bot_id||ha.bot_id||'';
    document.getElementById('ha-wecom-bot-secret').value=ha.wecom_bot_secret||ha.bot_secret||'';
    document.getElementById('ha-wake-words').value=(ha.wake_words||[]).join(', ');
    // 设备列表
    document.getElementById('ha-devices').innerHTML='';
    var devs=ha.devices||{};
    for(var dn in devs){addDeviceRow(dn,devs[dn]);}
  }catch(e){console.error('loadHardware',e);}
}

async function saveHardware(){
  var msg=document.getElementById('hardware-msg');msg.textContent='';
  var ha={
    base_url:document.getElementById('ha-url').value.trim(),
    token:document.getElementById('ha-token').value.trim(),
    rtsp_url:document.getElementById('ha-rtsp').value.trim(),
    audio_source:document.getElementById('ha-audio-src').value,
    wyoming_port:parseInt(document.getElementById('ha-wyoming-port').value)||10600,
    phone_url:document.getElementById('ha-phone-url').value.trim(),
    amap_key:document.getElementById('ha-amap-key').value.trim(),
    wecom_bot_id:document.getElementById('ha-wecom-bot-id').value.trim(),
    wecom_bot_secret:document.getElementById('ha-wecom-bot-secret').value.trim(),
    wake_words:document.getElementById('ha-wake-words').value.split(',').map(function(x){return x.trim();}).filter(Boolean),
    devices:{}
  };
  document.querySelectorAll('#ha-devices .hw-device-row').forEach(function(row){
    var n=row.children[0].value.trim();var e=row.children[1].value.trim();
    if(n&&e)ha.devices[n]=e;
  });
  try{
    var r=await fetch('/api/hardware',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hardware:ha})});
    if(r.ok){msg.textContent='✅ 已保存';msg.style.color='var(--ok)';}
    else{msg.textContent='❌ 保存失败';msg.style.color='var(--danger)';}
  }catch(e){msg.textContent='❌ 网络错误';msg.style.color='var(--danger)';}
  setTimeout(function(){msg.textContent='';},3000);
}

// ── 清除记忆（三步确认） ────────────────────────────
var CLEAR_SCOPES = {
  'all':'🗑  清除全部记忆（包括关联网络）',
  'detail':'清除细节层记忆',
  'outline':'清除细纲层记忆',
  'summary':'清除大纲层记忆',
  'emotional':'清除情感模态记忆',
  'semantic':'清除语义模态记忆',
};

function showClearMemory(){
  document.getElementById('page-chat').classList.remove('active');
  document.getElementById('page-settings').classList.remove('active');
  document.getElementById('page-personality').classList.remove('active');
  document.getElementById('page-hardware').classList.add('active');
  // 第1步：选择范围
  var scope=prompt(
    '⚠️ 清除记忆不可撤销！\n\n第 1/3 步 — 选择清除范围：\n\n'
    +Object.entries(CLEAR_SCOPES).map(function(kv){return kv[0]+' = '+kv[1];}).join('\n')
    +'\n\n输入对应的 键名（默认为 all）：',
    'all'
  );
  if(scope===null)return;
  scope=scope.trim()||'all';
  if(!CLEAR_SCOPES[scope]){alert('❌ 无效的范围: '+scope);return;}
  // 第2步：输入确认文字
  var confirmWord='确认清除';
  var text=prompt(
    '第 2/3 步 — 确认操作\n\n即将执行：'+CLEAR_SCOPES[scope]+'\n\n请在下方输入「'+confirmWord+'」以继续：',
    ''
  );
  if(text===null)return;
  if(text.trim()!==confirmWord){alert('❌ 确认文字不匹配');return;}
  // 第3步：最终确认
  if(!confirm('🚨 第 3/3 步 — 最终确认\n\n即将清除：'+CLEAR_SCOPES[scope]+'\n\n此操作不可撤销！确定要继续吗？'))return;
  // 执行
  doClearMemory(scope);
}

async function doClearMemory(scope){
  try{
    var r=await fetch('/api/clear_memory',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scope:scope})
    });
    var d=await r.json();
    if(d.ok){alert('✅ '+d.message);}
    else{alert('❌ 清除失败: '+(d.error||'未知错误'));}
  }catch(e){alert('❌ 网络错误: '+e.message);}
}

// ── 启动 ──
init();

// ── 高危工具确认弹窗 ──
var _sseEs = null;  // 当前 SSE 实例，防止重复连接
function startConfirmSSE(){
  if(typeof(EventSource)==='undefined')return;
  // 关闭旧连接，避免重复
  if(_sseEs){_sseEs.close();_sseEs=null;}
  var es=new EventSource('/api/confirm_stream');
  _sseEs=es;
  es.onmessage=function(e){
    try{
      var d=JSON.parse(e.data);
      if(d.type==='confirm_request')showConfirmDialog(d);
    }catch(ex){}
  };
  es.onerror=function(){
    if(es!==_sseEs)return;  // 过期回调，忽略
    _sseEs=null;
    es.close();
    // 5秒后自动重连（不阻塞页面）
    setTimeout(startConfirmSSE,5000);
  };
}

function showConfirmDialog(d){
  var overlay=document.createElement('div');
  overlay.id='cfm_'+d.id;
  overlay.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center';
  var paramsStr=JSON.stringify(d.params, null, 2);
  if(paramsStr.length>300)paramsStr=paramsStr.substring(0,300)+'...';
  overlay.innerHTML='<div style="background:var(--paper);border:1px solid var(--rim);border-radius:14px;padding:24px;max-width:420px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.4)">'+
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">'+
    '<div style="width:36px;height:36px;border-radius:10px;background:rgba(240,79,90,.15);display:flex;align-items:center;justify-content:center">'+
    '<svg viewBox="0 0 24 24" width="20" height="20"><path fill="var(--danger)" d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg></div>'+
    '<div style="font-weight:600;color:var(--text)">高风险操作确认</div></div>'+
    '<div style="color:var(--soft);font-size:.9rem;margin-bottom:8px">工具: <b style="color:var(--bright)">'+esc(d.tool_name)+'</b></div>'+
    '<pre style="background:var(--layer);border:1px solid var(--rim);border-radius:8px;padding:10px;font-size:.8rem;color:var(--dim);overflow:auto;max-height:160px;margin-bottom:16px;white-space:pre-wrap">'+esc(paramsStr)+'</pre>'+
    '<div style="display:flex;gap:10px;justify-content:flex-end">'+
    '<button onclick="replyConfirm(\''+d.id+'\',false)" style="padding:8px 20px;border-radius:8px;border:1px solid var(--rim);background:var(--layer);color:var(--soft);cursor:pointer;font-size:.9rem">拒绝</button>'+
    '<button onclick="replyConfirm(\''+d.id+'\',true)" style="padding:8px 20px;border-radius:8px;border:none;background:var(--danger);color:#fff;cursor:pointer;font-size:.9rem;font-weight:600">允许执行</button>'+
    '</div></div>';
  document.body.appendChild(overlay);
}

async function replyConfirm(id,approved){
  var el=document.getElementById('cfm_'+id);
  if(el)el.remove();
  try{await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,approved:approved})});}catch(e){}
}
</script>
</body>
</html>
"""
