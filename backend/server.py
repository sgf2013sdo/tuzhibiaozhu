"""
FastAPI 后端服务
接口：
  POST /api/upload        - 上传文件，自动识别格式并提取尺寸
  GET  /api/image/{id}    - 获取图纸图片
  POST /api/update        - 更新尺寸标注数据
  POST /api/export/excel  - 导出 Excel 质检表
  POST /api/export/image  - 导出带气泡标注的图纸
  GET  /api/health        - 健康检查
"""
import os
import sys
import json
import uuid
import asyncio
import traceback
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# 确保能 import 同级模块
sys.path.insert(0, str(Path(__file__).parent))

from dxf_handler import extract_dimensions_from_dxf
from pdf_handler import extract_from_pdf, extract_from_image
from exporter import export_excel, export_ballooned_image

app = FastAPI(title="BaboLocal API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 存储目录
UPLOAD_DIR = Path("/opt/data/babo-local/uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# 项目状态（内存中，重启后丢失）
PROJECTS = {}

# 线程池：用于在后台运行 CPU 密集的 OCR/解析任务，不阻塞事件循环
_executor = ThreadPoolExecutor(max_workers=4)

# OCR 超时时间（秒）
OCR_TIMEOUT = 90


async def _run_ocr_with_timeout(func, *args, timeout=None):
    """在线程池中运行 OCR/导出任务，带超时保护"""
    loop = asyncio.get_event_loop()
    actual_timeout = timeout or OCR_TIMEOUT
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, func, *args),
            timeout=actual_timeout,
        )
        return result
    except asyncio.TimeoutError:
        print(f"[WARN] 任务超时 ({actual_timeout}s): {getattr(func, '__name__', str(func))}")
        # 对于 OCR 函数，超时后返回空结果让用户手动操作
        func_name = getattr(func, "__name__", "")
        if "extract_from_pdf" in func_name:
            import fitz as _fitz
            import tempfile
            from pathlib import Path as _Path
            filepath = args[0]
            doc = _fitz.open(filepath)
            page = doc[0]
            mat = _fitz.Matrix(200/72, 200/72)
            pix = page.get_pixmap(matrix=mat)
            tmpdir = tempfile.mkdtemp(prefix="babo_")
            png_path = str(_Path(tmpdir) / "drawing.png")
            pix.save(png_path)
            doc.close()
            return {"image_path": png_path, "dimensions": [], "width": pix.width, "height": pix.height, "source_format": "pdf"}
        elif "extract_from_image" in func_name:
            from PIL import Image as _Image
            import tempfile
            from pathlib import Path as _Path
            filepath = args[0]
            img = _Image.open(filepath)
            if img.mode != "RGB":
                img = img.convert("RGB")
            tmpdir = tempfile.mkdtemp(prefix="babo_")
            png_path = str(_Path(tmpdir) / "drawing.png")
            img.save(png_path, "PNG")
            return {"image_path": png_path, "dimensions": [], "width": img.width, "height": img.height, "source_format": "image"}
        else:
            # 其他函数超时（如导出），抛异常
            raise HTTPException(500, f"处理超时，请减少标注数量或缩小图片后重试")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "BaboLocal"}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文件，自动识别格式并提取尺寸标注"""
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")

    ext = Path(file.filename).suffix.lower()
    project_id = str(uuid.uuid4())[:8]

    # 分块写入文件，避免大文件 OOM
    save_path = UPLOAD_DIR / f"{project_id}_{file.filename}"
    total_size = 0
    try:
        with open(save_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB 分块
                if not chunk:
                    break
                f.write(chunk)
                total_size += len(chunk)
        print(f"[INFO] 文件保存完成: {file.filename}, {total_size / 1024 / 1024:.1f} MB")
    except Exception as e:
        if save_path.exists():
            save_path.unlink()
        raise HTTPException(500, f"文件保存失败: {str(e)}")
    finally:
        await file.close()

    # 在线程池中运行解析（带超时保护，避免卡死线程池）
    try:
        if ext == ".dxf":
            result = await _run_ocr_with_timeout(extract_dimensions_from_dxf, str(save_path))
        elif ext == ".pdf":
            result = await _run_ocr_with_timeout(extract_from_pdf, str(save_path))
        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
            result = await _run_ocr_with_timeout(extract_from_image, str(save_path))
        else:
            if save_path.exists():
                save_path.unlink()
            raise HTTPException(400, f"不支持的文件格式: {ext}")
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"文件解析失败: {str(e)}")

    # 存储项目状态
    PROJECTS[project_id] = {
        "id": project_id,
        "filename": file.filename,
        "image_path": result["image_path"],
        "dimensions": result["dimensions"],
        "width": result["width"],
        "height": result["height"],
        "source_format": result["source_format"],
    }

    return {
        "project_id": project_id,
        "filename": file.filename,
        "width": result["width"],
        "height": result["height"],
        "dimensions": result["dimensions"],
        "source_format": result["source_format"],
        "count": len(result["dimensions"]),
    }


@app.get("/api/image/{project_id}")
async def get_image(project_id: str):
    """获取图纸图片"""
    proj = PROJECTS.get(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    return FileResponse(proj["image_path"], media_type="image/png")


class UpdateDimRequest(BaseModel):
    project_id: str
    dimensions: list


@app.post("/api/update")
async def update_dimensions(req: UpdateDimRequest):
    """更新尺寸标注数据"""
    proj = PROJECTS.get(req.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    proj["dimensions"] = req.dimensions
    return {"status": "ok", "count": len(req.dimensions)}


class ExportExcelRequest(BaseModel):
    project_id: str
    project_name: str = "质检报告"


@app.post("/api/export/excel")
async def export_excel_api(req: ExportExcelRequest):
    """导出 Excel 质检表"""
    proj = PROJECTS.get(req.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    data = export_excel(proj["dimensions"], req.project_name)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=inspection_report.xlsx"},
    )


class ExportImageRequest(BaseModel):
    project_id: str
    bubble_scale: float = 1.0
    bubble_shape: str = "circle"
    colors: Optional[dict] = None


@app.post("/api/export/image")
async def export_image_api(req: ExportImageRequest):
    """导出带气泡标注的图纸"""
    proj = PROJECTS.get(req.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    # 也在线程池中运行，避免大图阻塞
    data = await _run_ocr_with_timeout(
        export_ballooned_image,
        proj["image_path"],
        proj["dimensions"],
        req.bubble_scale,
        req.bubble_shape,
        req.colors,
    )
    return Response(
        content=data,
        media_type="image/png",
        headers={"Content-Disposition": "attachment; filename=ballooned_drawing.png"},
    )


# 静态文件服务前端
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9527,
                timeout=300, timeout_graceful_shutdown=10)
