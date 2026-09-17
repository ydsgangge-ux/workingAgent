"""
定时任务调度器 — 让 Levy 能在指定时间主动说话或执行工具
====================================================

核心能力：
1. 创建定时任务（一次性 / 重复）
2. 到期触发两种动作：speak（主动对用户说话）/ tool（调用工具）
3. 持久化到 JSON，重启后恢复未执行的任务
4. 与现有 add_schedule 互补：add_schedule 只记日程，这里负责执行

设计要点：
- 无任务时线程休眠，不空转
- 有任务时按最近触发时间动态计算等待，精确到秒
- 开机时扫描过期任务，补执行（不超过2小时的才补）
- 系统可自主创建任务，不依赖用户主动要求
"""
import json
import uuid
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, List, Dict


TASKS_FILE = Path(__file__).resolve().parent.parent / "data" / "tasks.json"
CATCHUP_MAX_HOURS = 2


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self, on_trigger: Optional[Callable[[Dict], None]] = None):
        self._tasks: List[Dict] = []
        self._on_trigger = on_trigger
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wakeup = threading.Event()
        self._load()

    def _load(self):
        if TASKS_FILE.exists():
            try:
                with open(TASKS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._tasks = data.get("tasks", [])
                self._log(f"已加载 {len(self._tasks)} 个任务")
            except Exception as e:
                self._log(f"加载任务失败: {e}")
                self._tasks = []
        else:
            self._tasks = []

    def _save(self):
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump({"tasks": self._tasks}, f, ensure_ascii=False, indent=2)

    def _log(self, msg: str):
        print(f"[TaskScheduler] {msg}")

    def _notify_wakeup(self):
        self._wakeup.set()

    def _next_pending_time(self) -> Optional[datetime]:
        earliest = None
        for t in self._tasks:
            if t.get("status") != "pending":
                continue
            try:
                dt = datetime.fromisoformat(t["trigger_time"])
                if earliest is None or dt < earliest:
                    earliest = dt
            except (ValueError, KeyError):
                continue
        return earliest

    def create_task(
        self,
        content: str,
        trigger_time: str,
        action: str = "speak",
        action_params: Optional[Dict] = None,
        repeat: Optional[str] = None,
        source: str = "system",
    ) -> Dict:
        trigger_dt = self._parse_trigger_time(trigger_time)
        if not trigger_dt:
            return {"ok": False, "error": f"无法解析触发时间: {trigger_time}"}

        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        task = {
            "id": task_id,
            "content": content,
            "trigger_time": trigger_dt.isoformat(),
            "action": action,
            "action_params": action_params or {},
            "repeat": repeat,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "source": source,
        }

        self._tasks.append(task)
        self._save()
        self._log(f"创建任务: {content} @ {trigger_dt.strftime('%m月%d日 %H:%M')}")
        self._notify_wakeup()
        return {"ok": True, "task_id": task_id, "message": f"已设定: {content}（{trigger_dt.strftime('%m月%d日 %H:%M')}）"}

    def cancel_task(self, task_id: str) -> Dict:
        for t in self._tasks:
            if t["id"] == task_id and t["status"] == "pending":
                t["status"] = "cancelled"
                self._save()
                self._log(f"取消任务: {t['content']}")
                return {"ok": True, "message": f"已取消: {t['content']}"}
        return {"ok": False, "error": f"未找到待执行任务: {task_id}"}

    def cancel_by_content(self, keyword: str) -> Dict:
        cancelled = []
        for t in self._tasks:
            if t["status"] == "pending" and keyword in t.get("content", ""):
                t["status"] = "cancelled"
                cancelled.append(t["content"])
        if cancelled:
            self._save()
            self._notify_wakeup()
            return {"ok": True, "message": f"已取消 {len(cancelled)} 个任务", "cancelled": cancelled}
        return {"ok": False, "error": f"未找到包含「{keyword}」的待执行任务"}

    def list_tasks(self, status: str = "pending") -> List[Dict]:
        return [t for t in self._tasks if t.get("status") == status]

    def _parse_trigger_time(self, trigger_time: str) -> Optional[datetime]:
        now = datetime.now()
        if trigger_time.startswith("+"):
            return self._parse_relative(trigger_time, now)
        try:
            dt = datetime.fromisoformat(trigger_time)
            if dt < now:
                return None
            return dt
        except ValueError:
            pass
        try:
            parts = trigger_time.strip().split(":")
            if len(parts) == 2:
                h, m = int(parts[0]), int(parts[1])
                dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if dt <= now:
                    dt += timedelta(days=1)
                return dt
        except (ValueError, IndexError):
            pass
        return None

    def _parse_relative(self, expr: str, now: datetime) -> Optional[datetime]:
        expr = expr[1:]
        try:
            if expr.endswith("m"):
                return now + timedelta(minutes=int(expr[:-1]))
            elif expr.endswith("h"):
                return now + timedelta(hours=int(expr[:-1]))
            elif expr.endswith("d"):
                return now + timedelta(days=int(expr[:-1]))
            elif expr.endswith("min"):
                return now + timedelta(minutes=int(expr[:-3]))
        except (ValueError, IndexError):
            pass
        return None

    def _fire_task(self, task: Dict):
        task["status"] = "done"
        self._log(f"触发任务: {task['content']} [{task['action']}]")

        if self._on_trigger:
            try:
                self._on_trigger(task)
            except Exception as e:
                self._log(f"回调执行失败: {e}")

        if task.get("repeat"):
            next_dt = self._next_occurrence(task)
            if next_dt:
                task["trigger_time"] = next_dt.isoformat()
                task["status"] = "pending"

    def _check_and_fire(self):
        now = datetime.now()
        fired = []
        for task in self._tasks:
            if task["status"] != "pending":
                continue
            try:
                trigger_dt = datetime.fromisoformat(task["trigger_time"])
            except (ValueError, KeyError):
                continue
            if now >= trigger_dt:
                fired.append(task)

        for task in fired:
            self._fire_task(task)

        if fired:
            self._save()

    def _next_occurrence(self, task: Dict) -> Optional[datetime]:
        repeat = task.get("repeat", "")
        try:
            last = datetime.fromisoformat(task["trigger_time"])
        except (ValueError, KeyError):
            return None
        if repeat == "daily":
            return last + timedelta(days=1)
        elif repeat == "weekly":
            return last + timedelta(weeks=1)
        elif repeat.startswith("interval:"):
            minutes = int(repeat.split(":")[1])
            return last + timedelta(minutes=minutes)
        return None

    def catchup_overdue(self):
        """开机补执行：扫描过期未执行的任务，2小时内的补执行，超时的标记过期"""
        now = datetime.now()
        overdue = []
        expired = []

        for task in self._tasks:
            if task["status"] != "pending":
                continue
            try:
                trigger_dt = datetime.fromisoformat(task["trigger_time"])
            except (ValueError, KeyError):
                continue

            if trigger_dt > now:
                continue

            delay = (now - trigger_dt).total_seconds() / 3600

            if delay <= CATCHUP_MAX_HOURS:
                overdue.append(task)
            else:
                expired.append(task)

        for task in overdue:
            self._log(f"补执行过期任务: {task['content']}（迟到 {(now - datetime.fromisoformat(task['trigger_time'])).seconds // 60} 分钟）")
            task["action_params"] = task.get("action_params", {})
            msg = task["action_params"].get("message", task["content"])
            task["action_params"]["message"] = f"（迟到的提醒）{msg}"
            self._fire_task(task)

        for task in expired:
            task["status"] = "expired"
            self._log(f"标记过期任务: {task['content']}（超过 {CATCHUP_MAX_HOURS} 小时）")

        if overdue or expired:
            self._save()

        return {
            "catchup": len(overdue),
            "expired": len(expired),
        }

    def start(self):
        """启动后台调度线程 — 智能等待，无任务时休眠"""
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                try:
                    self._check_and_fire()
                except Exception as e:
                    self._log(f"检查出错: {e}")

                next_time = self._next_pending_time()
                if next_time is None:
                    self._wakeup.wait(timeout=300)
                    self._wakeup.clear()
                else:
                    wait_sec = (next_time - datetime.now()).total_seconds()
                    wait_sec = max(1, min(wait_sec, 300))
                    self._wakeup.wait(timeout=wait_sec)
                    self._wakeup.clear()

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        self._log("调度器已启动（智能等待模式）")

    def stop(self):
        self._running = False
        self._notify_wakeup()
        self._log("调度器已停止")


_scheduler_instance: Optional[TaskScheduler] = None


def get_scheduler() -> TaskScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = TaskScheduler()
    return _scheduler_instance


def init_scheduler(on_trigger: Callable[[Dict], None]) -> TaskScheduler:
    global _scheduler_instance
    _scheduler_instance = TaskScheduler(on_trigger=on_trigger)
    return _scheduler_instance
