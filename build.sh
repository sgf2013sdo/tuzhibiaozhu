#!/bin/bash
# ============================================
# BaboLocal Linux 打包脚本
# ============================================
set -e

echo "========================================"
echo "  BaboLocal 桌面应用打包工具 (Linux)"
echo "========================================"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "[INFO] 创建虚拟环境..."
    python3 -m venv venv
fi

source venv/bin/activate

# 安装依赖
echo "[INFO] 安装依赖..."
pip install -r requirements.txt
pip install pyinstaller pywebview

# 清理旧构建
echo "[INFO] 清理旧构建..."
rm -rf build dist BaboLocal.spec
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true

# 打包
echo "[INFO] 开始打包..."
pyinstaller --name BaboLocal \
    --onefile \
    --windowed \
    --noconfirm \
    --add-data "frontend:frontend" \
    --add-data "backend:backend" \
    --add-data "desktop:desktop" \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import server \
    --hidden-import dxf_handler \
    --hidden-import pdf_handler \
    --hidden-import exporter \
    --collect-all rapidocr_onnxruntime \
    --collect-all onnxruntime \
    desktop/main.py

echo ""
echo "========================================"
echo "  打包成功！"
echo "  可执行文件位于: dist/BaboLocal"
echo "========================================"
echo ""
echo "  使用方法："
echo "  1. 将 dist/BaboLocal 复制到任意目录"
echo "  2. 双击或 ./BaboLocal 运行"
echo "  3. 首次启动可能较慢（解压依赖）"
echo ""
