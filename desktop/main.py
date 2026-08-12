"""
BaboLocal 桌面应用启动器
- 后台线程启动 FastAPI 服务（绑定 localhost 随机端口）
- pywebview 创建原生窗口加载前端页面
- 提供文件保存 API（pywebview 不支持浏览器 <a download> 下载）
- 启动失败时在浏览器显示错误信息
"""
import datetime
import base64
_STARTUP_LOG = str(__import__('pathlib').Path.home() / 'BaboLocal' / 'startup.log')
try:
    __import__('pathlib').Path(_STARTUP_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(_STARTUP_LOG, 'a', encoding='utf-8') as _log:
        _log.write(f'[{datetime.datetime.now()}] EXE started, cwd={__import__("os").getcwd()}\n')
except:
    pass

import os
import sys
import time
import threading
import socket
import http.client
from pathlib import Path


def get_app_root():
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent.resolve()


def get_uploads_dir():
    if hasattr(sys, '_MEIPASS'):
        return Path.home() / "BaboLocal" / "uploads"
    return get_app_root() / "uploads"


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for_server(port, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            conn.request('GET', '/api/health')
            resp = conn.getresponse()
            conn.close()
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_server(port, stop_event):
    app_root = get_app_root()
    backend_dir = str(app_root / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    os.chdir(str(app_root))

    uploads_dir = get_uploads_dir()
    uploads_dir.mkdir(parents=True, exist_ok=True)
    os.environ['BABO_UPLOAD_DIR'] = str(uploads_dir)

    if hasattr(sys, '_MEIPASS'):
        os.environ['BABO_PYTHON'] = sys.executable
    else:
        os.environ['BABO_PYTHON'] = str(app_root / "venv" / "bin" / "python")

    # 完全本地化：不设置 GLM API key，禁用任何云端调用
    # （pdf_handler 检测到无 key 会自动跳过 VLM，只用本地 RapidOCR）
    os.environ.pop('GLM_API_KEY', None)
    os.environ.setdefault('ORT_LOGGING_LEVEL', '3')

    try:
        import uvicorn, importlib
        server_mod = importlib.import_module('server')
        app = server_mod.app
        config = uvicorn.Config(app, host='127.0.0.1', port=port,
                                 log_level='warning', timeout_keep_alive=300, access_log=False)
        srv = uvicorn.Server(config)
        srv.run()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"[ERROR] 服务器启动失败:\n{err}", file=sys.stderr)
        err_path = str(get_uploads_dir()) + '/babo_error.log'
        try:
            with open(err_path, 'w', encoding='utf-8') as f:
                f.write(err)
        except:
            pass


def serve_error_page(port, err_msg):
    import re
    safe = re.sub(r'[<>]', ' ', err_msg[:3000])
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = f"""<!DOCTYPE html><html><head><meta charset=utf-8><title>BaboLocal</title></head>
<body style="font-family:monospace;padding:20px;background:#1a1a2e;color:#e0e0e0">
<h2 style="color:#f44336">❌ 启动失败</h2><p>请截图发给开发者：</p>
<pre style="background:#0d0d1a;padding:16px;border-radius:8px;color:#4CAF50;max-height:400px;overflow:auto">{safe}</pre>
</body></html>"""
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode())

    try:
        httpd = HTTPServer(('127.0.0.1', port), Handler)
        httpd.serve_forever()
    except:
        pass


class BaboApi:
    """pywebview JS 桥接 API：EXE 模式下浏览器 <a download> 不可用，改走这里保存文件"""

    def save_file(self, base64_data: str, filename: str) -> str:
        """保存文件到用户主目录 BaboLocal/exports/ 下，返回保存路径或错误信息"""
        try:
            from tkinter import Tk, filedialog
            root = Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            # 默认保存目录
            default_dir = str(Path.home() / "BaboLocal" / "exports")
            Path(default_dir).mkdir(parents=True, exist_ok=True)
            path = filedialog.asksaveasfilename(
                title='保存文件', initialdir=default_dir, initialfile=filename,
                defaultextension=Path(filename).suffix or '.xlsx',
            )
            root.destroy()
            if not path:
                return '{"success": false, "message": "已取消保存"}'
            data = base64.b64decode(base64_data.split(',')[-1])
            with open(path, 'wb') as f:
                f.write(data)
            return '{"success": true, "message": "' + path.replace('\\', '/') + '"}'
        except Exception as e:
            return '{"success": false, "message": "' + str(e).replace('"', "'") + '"}'

    def open_exports(self) -> str:
        """打开导出目录"""
        try:
            import subprocess
            exports_dir = str(Path.home() / "BaboLocal" / "exports")
            Path(exports_dir).mkdir(parents=True, exist_ok=True)
            subprocess.Popen(['explorer', exports_dir])
            return '{"success": true}'
        except Exception as e:
            return '{"success": false, "message": "' + str(e).replace('"', "'") + '"}'


def main():
    err_file = str(get_uploads_dir()) + '/babo_error.log'
    try:
        os.remove(err_file)
    except:
        pass

    port = find_free_port()
    stop_event = threading.Event()
    server_thread = threading.Thread(target=start_server, args=(port, stop_event), daemon=True)
    server_thread.start()

    if not wait_for_server(port, timeout=12):
        time.sleep(2)
        if os.path.exists(err_file):
            with open(err_file, 'r', encoding='utf-8') as f:
                err = f.read()
            serve_error_page(port, err)
            return
        else:
            print("[ERROR] 启动超时", file=sys.stderr)

    url = f'http://127.0.0.1:{port}/'

    gui_available = True
    try:
        import webview
    except ImportError:
        gui_available = False

    if gui_available:
        try:
            webview.create_window(title='BaboLocal', url=url, width=1400, height=900,
                                   min_size=(1000, 700), text_select=False,
                                   js_api=BaboApi())
            webview.start(debug=False)
        except Exception as e:
            print(f"[WARN] 窗口不可用: {e}")
            gui_available = False

    if not gui_available:
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    stop_event.set()


if __name__ == '__main__':
    main()
