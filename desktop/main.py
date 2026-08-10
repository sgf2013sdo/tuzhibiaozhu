"""
BaboLocal 桌面应用启动器
- 后台线程启动 FastAPI 服务（绑定 localhost 随机端口）
- pywebview 创建原生窗口加载前端页面
- 关闭窗口时自动退出服务
"""
import os
import sys
import time
import threading
import socket
import http.client
from pathlib import Path


def get_app_root():
    """获取应用根目录（兼容 PyInstaller --onefile 打包）"""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    # desktop/main.py -> 项目根目录是 parent.parent
    return Path(__file__).parent.parent.resolve()


def get_uploads_dir():
    """获取上传目录 - 打包后使用用户目录，开发时用项目目录"""
    if hasattr(sys, '_MEIPASS'):
        # 打包模式：放在用户文档目录下
        return Path.home() / "BaboLocal" / "uploads"
    return get_app_root() / "uploads"


def find_free_port():
    """找一个可用的随机端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for_server(port, timeout=30):
    """等待服务器就绪"""
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
        time.sleep(0.3)
    return False


def start_server(port, stop_event):
    """在后台线程中启动 FastAPI 服务"""
    app_root = get_app_root()

    # 把 backend 目录加入 sys.path
    backend_dir = str(app_root / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    # 设置工作目录到项目根目录（server.py 中的相对路径依赖这个）
    os.chdir(str(app_root))

    # 设置上传目录（打包后用用户目录）
    uploads_dir = get_uploads_dir()
    uploads_dir.mkdir(parents=True, exist_ok=True)
    os.environ['BABO_UPLOAD_DIR'] = str(uploads_dir)

    # 打包后子进程用 sys.executable；开发时用 venv python
    if hasattr(sys, '_MEIPASS'):
        os.environ['BABO_PYTHON'] = sys.executable
    else:
        os.environ['BABO_PYTHON'] = str(app_root / "venv" / "bin" / "python")

    # GLM-4V API 凭证（VLM 增强识别模式用）
    # 打包到 exe 时固定 API key；本地开发时可从外部环境变量覆盖
    os.environ.setdefault('GLM_API_KEY', '428d0fc749c44f5bacdd04db164ca026.9ySXXn37Xwt3Vrx6')
    os.environ.setdefault('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')

    # 环境变量确保 OCR 库能找到模型文件
    os.environ.setdefault('ORT_LOGGING_LEVEL', '3')  # 减少日志输出

    try:
        import uvicorn
        # 直接 import app 对象，不走 uvicorn.run（避免重复配置）
        import importlib
        server_mod = importlib.import_module('server')
        app = server_mod.app

        config = uvicorn.Config(
            app,
            host='127.0.0.1',
            port=port,
            log_level='warning',
            timeout_keep_alive=300,
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception as e:
        print(f"[ERROR] 服务器启动失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()


def main():
    port = find_free_port()
    print(f"[INFO] 启动服务，端口: {port}")

    stop_event = threading.Event()
    server_thread = threading.Thread(target=start_server, args=(port, stop_event), daemon=True)
    server_thread.start()

    # 等待服务器就绪
    if not wait_for_server(port, timeout=30):
        print("[ERROR] 服务器启动超时", file=sys.stderr)
        # 回退：尝试用系统浏览器打开
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}/')
        return

    url = f'http://127.0.0.1:{port}/'
    print(f"[INFO] 服务器就绪: {url}")

    # 启动 pywebview 窗口
    gui_available = True
    try:
        import webview
    except ImportError:
        gui_available = False
        print("[WARN] pywebview 未安装")

    if gui_available:
        try:
            window = webview.create_window(
                title='BaboLocal - 气泡标注工具',
                url=url,
                width=1400,
                height=900,
                min_size=(1000, 700),
                text_select=False,
            )
            webview.start(debug=False)
            print("[INFO] 窗口已关闭，正在停止服务...")
        except Exception as e:
            print(f"[WARN] pywebview GUI 不可用 ({e})，回退到系统浏览器")
            gui_available = False

    if not gui_available:
        import webbrowser
        webbrowser.open(url)
        print("[INFO] 已在系统浏览器中打开，按 Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[INFO] 正在停止服务...")

    # 清理
    stop_event.set()


if __name__ == '__main__':
    main()
