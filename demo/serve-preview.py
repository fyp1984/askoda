#!/usr/bin/env python3
"""本地预览服务：禁用缓存，确保 HTML 更新后刷新即加载最新版本。
用法：在项目根目录运行 python3 serve-preview.py [端口]（默认 8123）"""
import http.server
import socketserver
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏


socketserver.TCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), NoCacheHandler) as httpd:
    httpd.daemon_threads = True
    print(f"预览服务已启动: http://127.0.0.1:{PORT}/ （已禁用缓存，多线程，刷新即最新）")
    httpd.serve_forever()
