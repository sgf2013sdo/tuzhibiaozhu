@echo off
REM ============================================
REM BaboLocal Windows 打包脚本
REM 在 Windows 上运行此脚本生成 .exe
REM ============================================

echo ========================================
echo  BaboLocal 桌面应用打包工具
echo ========================================

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 创建虚拟环境（如果不存在）
if not exist venv (
    echo [INFO] 创建虚拟环境...
    python -m venv venv
)

REM 激活虚拟环境
call venv\Scripts\activate.bat

REM 安装依赖
echo [INFO] 安装依赖...
pip install -r requirements.txt
pip install pyinstaller pywebview

REM 清理旧构建
echo [INFO] 清理旧构建...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del BaboLocal.spec 2>nul
del /q __pycache__\*.pyc 2>nul
del /q backend\__pycache__\*.pyc 2>nul

REM 打包
echo [INFO] 开始打包...
pyinstaller --name BaboLocal ^
    --onefile ^
    --windowed ^
    --noconfirm ^
    --add-data "frontend;frontend" ^
    --add-data "backend;backend" ^
    --add-data "desktop;desktop" ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.websockets ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.lifespan.on ^
    --hidden-import server ^
    --hidden-import dxf_handler ^
    --hidden-import pdf_handler ^
    --hidden-import exporter ^
    --collect-all rapidocr_onnxruntime ^
    --collect-all onnxruntime ^
    desktop\main.py

if errorlevel 1 (
    echo [ERROR] 打包失败！
    pause
    exit /b 1
)

echo.
echo ========================================
echo  打包成功！
echo  exe 文件位于: dist\BaboLocal.exe
echo ========================================
echo.
echo  使用方法：
echo  1. 将 dist\BaboLocal.exe 复制到任意目录
echo  2. 双击运行即可
echo  3. 首次启动可能较慢（解压依赖）
echo.
pause
