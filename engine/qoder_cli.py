"""
Qoder CLI 接入
==============
把仓库级编码任务外包给 Qoder CLI 的 headless 模式，适合：
  - 跨文件修改、重构、迁移
  - 需要跑编译 / 测试、多轮自我纠错
  - 预计耗时几分钟以上

只负责"跑任务 + 收集证据"，是否合并由 B 层/用户决定：
  - 默认 permission_mode=accept_edits（自动批准工作区文件编辑，命令仍需权限）
  - 不执行 git commit / push
  - 运行前后对比 git status，返回真实改动文件清单

配置键（config.json）：
  qoder_cli_path         默认 "qoder"，可填绝对路径
  qoder_token            Qoder PAT；留空则复用本机 qoder login 的登录态
  qoder_default_repo     默认仓库目录；留空则用当前项目根目录
  qoder_permission_mode  default / accept_edits / auto / bypass_permissions
  qoder_max_turns        单次最大轮数，默认 20
  qoder_timeout          超时秒数，默认 900
"""

import os
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_CLI = "qoder"
DEFAULT_PERMISSION = "accept_edits"
DEFAULT_MAX_TURNS = 20
DEFAULT_TIMEOUT = 900
_PROGRESS_KEEP = 8000       # 最终回复最多保留的字符数
_RAW_LOG_KEEP = 20000      # 原始输出最多解析的字符数

INSTALL_HINT = (
    "未找到 qoder 命令。请先安装并登录：\n"
    "  Windows PowerShell: irm https://qoder.com/install.ps1 | iex\n"
    "  macOS/Linux:        curl -fsSL https://qoder.com/install | bash\n"
    "安装后运行 qoder login，或在 config.json 配置 qoder_token（PAT）。"
)


def _load_cfg() -> Dict:
    try:
        from desktop.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_cli(cfg: Optional[Dict] = None) -> str:
    """返回可执行的 qoder 路径，找不到返回空字符串"""
    cfg = cfg if cfg is not None else _load_cfg()
    raw = str(cfg.get("qoder_cli_path") or DEFAULT_CLI).strip()
    if not raw:
        raw = DEFAULT_CLI
    # 绝对/相对路径且存在，直接用
    p = Path(raw).expanduser()
    if p.exists():
        return str(p)
    return shutil.which(raw) or shutil.which(DEFAULT_CLI) or ""


def _resolve_repo(repo: str, cfg: Dict) -> str:
    candidate = (repo or cfg.get("qoder_default_repo") or "").strip()
    if not candidate:
        return str(_project_root())
    return str(Path(candidate).expanduser().resolve())


def _git_lines(repo: str, args: List[str]) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", repo] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, shell=False,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _git_snapshot(repo: str) -> set:
    out = _git_lines(repo, ["status", "--porcelain"])
    return {line for line in out.splitlines() if line.strip()}


def _kill_tree(proc: subprocess.Popen):
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15, shell=False,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _handle_event(obj: Dict, state: Dict):
    """解析 stream-json 的一条事件"""
    t = obj.get("type")

    if t == "system":
        sid = obj.get("session_id") or obj.get("sessionId")
        if sid:
            state["session_id"] = sid

    elif t == "assistant":
        msg = obj.get("message") or {}
        for block in (msg.get("content") or []):
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text") or ""
                if txt.strip():
                    state["texts"].append(txt.strip())
            elif btype == "tool_use":
                name = block.get("name") or ""
                if name:
                    state["tools"].append(name)

    elif t == "result":
        if obj.get("result"):
            state["result_text"] = str(obj["result"])
        state["subtype"] = obj.get("subtype") or ""
        state["is_error"] = bool(obj.get("is_error"))
        state["cost_usd"] = obj.get("total_cost_usd")
        state["duration_ms"] = obj.get("duration_ms")


def run_task(task: str, repo: str = "", timeout: int = 0,
             permission_mode: str = "", max_turns: int = 0) -> Dict:
    """调用 Qoder CLI 执行一个编码任务。

    返回：
      ok / result / session_id / tools_used / changed_files / diff_stat /
      exit_code / elapsed / timed_out / error
    """
    task = (task or "").strip()
    if not task:
        return {"ok": False, "error": "任务描述为空"}

    cfg = _load_cfg()
    cli = resolve_cli(cfg)
    if not cli:
        return {"ok": False, "installed": False, "error": INSTALL_HINT}

    repo_path = _resolve_repo(repo, cfg)
    if not Path(repo_path).is_dir():
        return {"ok": False, "error": f"仓库目录不存在：{repo_path}"}

    mode = (permission_mode or cfg.get("qoder_permission_mode")
            or DEFAULT_PERMISSION).strip()
    turns = int(max_turns or cfg.get("qoder_max_turns") or DEFAULT_MAX_TURNS)
    limit = int(timeout or cfg.get("qoder_timeout") or DEFAULT_TIMEOUT)

    cmd = [
        cli, "-p", task,
        "-o", "stream-json",
        "--permission-mode", mode,
        "--max-turns", str(turns),
        "--cwd", repo_path,
    ]

    env = os.environ.copy()
    token = str(cfg.get("qoder_token") or "").strip()
    if token:
        env["QODER_PERSONAL_ACCESS_TOKEN"] = token
    env.setdefault("NO_COLOR", "1")

    before = _git_snapshot(repo_path)
    state: Dict = {
        "texts": [], "tools": [], "session_id": "",
        "subtype": "", "is_error": False, "result_text": "",
        "cost_usd": None, "duration_ms": None,
    }
    stderr_buf: List[str] = []
    raw_buf: List[str] = []
    raw_len = 0
    lock = threading.Lock()

    def _read_stdout(pipe):
        nonlocal raw_len
        try:
            for line in pipe:
                line = (line or "").rstrip("\r\n")
                if not line:
                    continue
                with lock:
                    if raw_len < _RAW_LOG_KEEP:
                        raw_buf.append(line)
                        raw_len += len(line)
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    with lock:
                        _handle_event(obj, state)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _read_stderr(pipe):
        try:
            for line in pipe:
                if line and sum(len(x) for x in stderr_buf) < 8000:
                    stderr_buf.append(line)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    started = time.time()
    timed_out = False
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd,
            cwd=repo_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return {"ok": False, "installed": False, "error": INSTALL_HINT}
    except Exception as e:
        return {"ok": False, "error": f"启动 Qoder CLI 失败：{e}"}

    t_out = threading.Thread(target=_read_stdout, args=(proc.stdout,), daemon=True)
    t_err = threading.Thread(target=_read_stderr, args=(proc.stderr,), daemon=True)
    t_out.start()
    t_err.start()

    try:
        exit_code = proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        exit_code = -1

    t_out.join(timeout=10)
    t_err.join(timeout=5)

    after = _git_snapshot(repo_path)
    changed = sorted(after - before)
    diff_stat = _git_lines(repo_path, ["diff", "--stat"])

    with lock:
        texts = list(state["texts"])
        tools_used = []
        for name in state["tools"]:
            if name not in tools_used:
                tools_used.append(name)
        joined = "\n\n".join(texts)
        result_text = state["result_text"] or joined
        if joined and len(joined) > len(result_text):
            result_text = joined

    elapsed = round(time.time() - started, 1)
    stderr_text = "".join(stderr_buf).strip()

    ok = (not timed_out) and exit_code == 0 and not state["is_error"]
    out: Dict = {
        "ok": ok,
        "repo": repo_path,
        "result": result_text[:_PROGRESS_KEEP],
        "session_id": state["session_id"],
        "tools_used": tools_used,
        "changed_files": changed[:100],
        "diff_stat": diff_stat[-3000:],
        "exit_code": exit_code,
        "elapsed": elapsed,
        "permission_mode": mode,
        "max_turns": turns,
    }
    if state["cost_usd"] is not None:
        out["cost_usd"] = state["cost_usd"]
    if timed_out:
        out["timed_out"] = True
        out["error"] = f"Qoder 任务超时（{limit} 秒），已终止进程"
    elif not ok:
        out["error"] = (stderr_text or state["subtype"]
                        or "Qoder 执行失败（退出码非 0）")
    if not ok and not out["result"]:
        # 附带最后一段原始输出，方便排查登录/权限问题
        out["raw_tail"] = "\n".join(raw_buf[-20:])[:3000]
    return out


def check_qoder(cfg: Optional[Dict] = None) -> Dict:
    """检查 Qoder CLI 是否安装、版本、配置的仓库与认证方式"""
    cfg = cfg if cfg is not None else _load_cfg()
    cli = resolve_cli(cfg)
    if not cli:
        return {"ok": False, "installed": False, "error": INSTALL_HINT}

    version = ""
    try:
        r = subprocess.run(
            [cli, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, shell=False,
        )
        version = (r.stdout or r.stderr or "").strip()[:200]
    except Exception as e:
        return {"ok": False, "installed": True, "path": cli,
                "error": f"无法执行 qoder --version：{e}"}

    repo = _resolve_repo("", cfg)
    return {
        "ok": True,
        "installed": True,
        "path": cli,
        "version": version,
        "repo": repo,
        "repo_exists": Path(repo).is_dir(),
        "has_token": bool(str(cfg.get("qoder_token") or "").strip()),
        "token_env": bool(os.environ.get("QODER_PERSONAL_ACCESS_TOKEN")),
        "permission_mode": cfg.get("qoder_permission_mode") or DEFAULT_PERMISSION,
        "max_turns": cfg.get("qoder_max_turns") or DEFAULT_MAX_TURNS,
        "timeout": cfg.get("qoder_timeout") or DEFAULT_TIMEOUT,
        "tip": "首次使用需 qoder login，或配置 qoder_token（PAT）",
    }
