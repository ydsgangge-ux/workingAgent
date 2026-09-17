"""
查看启动时恢复的对话上下文
直接查询 interactions 表，显示最近 N 条记录

用法：
  python check_context.py            显示最近10条
  python check_context.py 5          显示最近5条
"""
import sqlite3
from pathlib import Path
import sys, os

# 数据库路径
DB = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "workingAgent" / "memory.db"

limit = 10
if len(sys.argv) > 1:
    try:
        limit = int(sys.argv[1])
    except ValueError:
        DB = Path(sys.argv[1])

if not DB.exists():
    print(f"[-] 数据库不存在: {DB}")
    print(f"[-] 如果路径不对，请手动指定: python check_context.py <数据库路径>")
    sys.exit(1)

try:
    conn = sqlite3.connect(str(DB), timeout=10)
except Exception as e:
    print(f"[-] 连接数据库失败: {e}")
    print("[-] 可能原因：服务正在运行中，请先关闭服务再试")
    sys.exit(1)

# 检查 interactions 表
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
if "interactions" not in tables:
    print("[-] interactions 表不存在，还没有对话记录")
    conn.close()
    sys.exit(0)

# 统计总数
total = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
print(f"[数据库] {DB}")
print(f"[总数]   {total} 条对话记录\n")

# 取最近记录（时间倒序）
rows = conn.execute(
    "SELECT timestamp, user_input, response FROM interactions "
    "ORDER BY timestamp DESC LIMIT ?", (limit,)
).fetchall()

print(f"最近 {min(limit, len(rows))} 条记录（时间倒序，最新的在最前）：")
print("=" * 70)
for i, (ts, inp, rsp) in enumerate(rows):
    print(f"\n── #{i+1}  [{ts[:19]}] ──")
    print(f"  [用户]: {inp[:200]}")
    print(f"  [回应]: {rsp[:300]}")

conn.close()
print()