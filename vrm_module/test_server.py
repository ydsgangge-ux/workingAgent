"""
VRM 模型测试服务
运行后在浏览器打开 http://localhost:8899 查看效果
"""
import http.server
import threading
import webbrowser
import os
import sys

PORT = 8899
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if not os.path.isdir(STATIC_DIR):
    print(f"[VRM] static 目录不存在: {STATIC_DIR}")
    sys.exit(1)

os.chdir(STATIC_DIR)

handler = http.server.SimpleHTTPRequestHandler
httpd = http.server.HTTPServer(("127.0.0.1", PORT), handler)
print(f"[VRM] 测试服务已启动: http://localhost:{PORT}")
print(f"[VRM] 按 Ctrl+C 停止")
print()

# 自动打开浏览器
url = f"http://localhost:{PORT}/vrm_viewer.html"
webbrowser.open(url)

try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print("\n[VRM] 服务已停止")
    httpd.server_close()
