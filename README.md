# BaboLocal - 气泡标注工具

工程图纸尺寸标注自动识别与气泡编号工具，支持 PDF / 图片 / DXF 文件。

## 功能

- 上传图纸自动 OCR 识别尺寸标注
- 气泡编号显示（可拖拽、可调大小）
- 尺寸标注列表（搜索、筛选、排序）
- 编辑弹窗（含 GB/T 1804-2000 公差表选择器）
- Delete 键快速删除选中气泡
- 导出 Excel 质检报告
- 导出带气泡标注的图纸图片

## 目录结构

```
babo-local/
├── desktop/main.py       # 桌面应用启动器（pywebview + 内嵌 FastAPI）
├── backend/
│   ├── server.py         # FastAPI 服务（子进程模型，超时可 kill）
│   ├── pdf_handler.py    # PDF/图片 OCR 处理
│   ├── dxf_handler.py    # DXF 解析 + 坐标变换
│   └── exporter.py       # Excel/图片导出
├── frontend/index.html   # 前端单页应用
├── build.bat             # Windows 打包脚本
├── build.sh              # Linux 打包脚本
├── requirements.txt      # Python 依赖
└── .github/workflows/    # GitHub Actions CI 自动打包
```

## 开发模式运行

```bash
# 1. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate    # Linux
venv\Scripts\activate       # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3a. Web 模式（浏览器访问）
python -m uvicorn backend.server:app --host 0.0.0.0 --port 9527 --timeout-keep-alive 300
# 浏览器打开 http://localhost:9527

# 3b. 桌面模式（原生窗口）
python desktop/main.py
```

## 打包为 EXE

### 方式一：本地打包（Windows）

1. 将项目复制到 Windows 机器
2. 双击运行 `build.bat`
3. 生成的 `dist/BaboLocal.exe` 即可分发

### 方式二：本地打包（Linux）

```bash
chmod +x build.sh
./build.sh
# 生成 dist/BaboLocal
```

### 方式三：GitHub Actions 自动打包

1. 将项目推送到 GitHub
2. 打 tag 触发构建：`git tag v1.0 && git push origin v1.0`
3. CI 自动在 Windows 环境打包，生成 Release 下载链接
4. 或在 GitHub 仓库 Actions 页面手动触发 `workflow_dispatch`

## 技术架构

### 为什么用子进程模型？

旧版使用 `ThreadPoolExecutor` 运行 OCR，Python 线程无法强制终止，OCR 卡死时线程池被耗尽导致 502。

新版每次上传在独立子进程中运行 OCR，超时后 `proc.kill()` 强制终止，主进程不受影响。超时/出错时自动降级为"仅显示图纸、无标注"，用户可手动添加。

### 为什么用 pywebview？

浏览器通过局域网访问服务器时，OCR 处理时间较长（20-180秒），浏览器或网络中间件可能超时断连返回 502。

pywebview 创建原生窗口，FastAPI 绑定 `127.0.0.1` 本地端口，全程无网络层，彻底消除超时问题。打包为 exe 后用户双击即可使用，无需安装 Python 环境。

### 坐标系说明

| 文件类型 | 图片尺寸 | 标注坐标 | 匹配 |
|---------|---------|---------|------|
| PDF | 渲染像素 | 文本层坐标 × DPI缩放 | ✅ |
| 图片 | 原始像素 | OCR 像素坐标 | ✅ |
| DXF | 实际PNG像素 | 世界坐标 → 像素变换（含Y翻转）| ✅ |
