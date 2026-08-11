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
import subprocess
import traceback
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# PyInstaller 打包后的资源路径
def _app_path(relative):
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / relative
    return (Path(__file__).parent.parent / relative).resolve()

# 确保能 import 同级模块
sys.path.insert(0, str(Path(__file__).parent))

from exporter import export_excel, export_ballooned_image

app = FastAPI(title="BaboLocal API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 存储目录（打包后使用环境变量指定的用户目录）
UPLOAD_DIR = Path(os.environ.get("BABO_UPLOAD_DIR", "/opt/data/babo-local/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 项目状态（内存中，重启后丢失）
PROJECTS = {}

# OCR 超时时间（秒）
OCR_TIMEOUT = 180


async def _run_extraction_subprocess(script_path: str, filepath: str, timeout: int = None, recog_mode: str = "ocr"):
    """
    运行 OCR/解析任务。
    - 开发模式（Linux/venv）: 独立子进程，超时可 kill
    - EXE 打包模式: 直接进程内调用（EXE 不是 Python 解释器，无法用 -c 子进程）
    recog_mode: "ocr" 或 "vlm"，控制识别精度
    """
    actual_timeout = timeout or OCR_TIMEOUT

    # 设置识别模式环境变量（pdf_handler 内读取）
    old_mode = os.environ.get("BABO_RECOGNITION_MODE")
    os.environ["BABO_RECOGNITION_MODE"] = recog_mode

    # EXE 模式：直接调用（无法 spawn 子进程，因为 EXE 不是 python 解释器）
    if hasattr(sys, '_MEIPASS'):
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            if script_path == 'pdf_handler':
                from pdf_handler import extract_from_pdf
                func = lambda: extract_from_pdf(filepath)
            elif script_path == 'pdf_handler_img':
                from pdf_handler import extract_from_image
                func = lambda: extract_from_image(filepath)
            else:
                from dxf_handler import extract_dimensions_from_dxf
                func = lambda: extract_dimensions_from_dxf(filepath)

            try:
                result = await _asyncio.wait_for(
                    _asyncio.to_thread(func), timeout=actual_timeout)
                return result
            except _asyncio.TimeoutError:
                print(f"[WARN] EXE 内 OCR 超时 ({actual_timeout}s)")
                return _generate_fallback_result(filepath, script_path)
        except HTTPException:
            raise
        except Exception as e:
            print(f"[ERROR] EXE 内 OCR 失败: {e}")
            import traceback; traceback.print_exc()
            return _generate_fallback_result(filepath, script_path)
        finally:
            if old_mode is None:
                os.environ.pop("BABO_RECOGNITION_MODE", None)
            else:
                os.environ["BABO_RECOGNITION_MODE"] = old_mode

    # ===== 开发模式：子进程 =====
    venv_python = os.environ.get("BABO_PYTHON", str(Path(__file__).parent.parent / "venv" / "bin" / "python"))

    sub_env = {**os.environ,
               "BABO_RECOGNITION_MODE": recog_mode,
               }

    try:
        proc = await asyncio.create_subprocess_exec(
            venv_python, "-c",
            f"""
import sys, json, traceback
sys.path.insert(0, {str(Path(__file__).parent)!r})
try:
    from {script_path} import {'extract_from_pdf' if 'pdf' in script_path else 'extract_from_image' if 'image' in script_path else 'extract_dimensions_from_dxf'}
    # 实际调用由下面的动态代码完成
except Exception:
    pass

# 动态执行
import importlib
mod = None
try:
    if {script_path!r} == 'pdf_handler':
        from pdf_handler import extract_from_pdf
        result = extract_from_pdf({filepath!r})
    elif {script_path!r} == 'pdf_handler_img':
        from pdf_handler import extract_from_image
        result = extract_from_image({filepath!r})
    elif {script_path!r} == 'dxf_handler':
        from dxf_handler import extract_dimensions_from_dxf
        result = extract_dimensions_from_dxf({filepath!r})
    print(json.dumps(result, ensure_ascii=False, default=str))
except Exception as e:
    traceback.print_exc()
    print(json.dumps({{"error": str(e)}}))
""",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent.parent),
            env=sub_env,
        )
    except Exception as e:
        raise HTTPException(500, f"启动处理进程失败: {str(e)}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=actual_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"[WARN] 子进程超时 ({actual_timeout}s)，已强制终止")

        # 超时后生成无标注的结果
        return _generate_fallback_result(filepath, script_path)

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace")[-500:] if stderr else "未知错误"
        print(f"[ERROR] 子进程退出码 {proc.returncode}: {err_msg}")
        # 可能是 OOM kill
        return _generate_fallback_result(filepath, script_path)

    output = stdout.decode("utf-8", errors="replace").strip()
    if not output:
        print(f"[ERROR] 子进程无输出，stderr: {stderr.decode('utf-8', errors='replace')[-500:]}")
        return _generate_fallback_result(filepath, script_path)

    try:
        result = json.loads(output)
        if "error" in result:
            print(f"[ERROR] 提取失败: {result['error']}")
            return _generate_fallback_result(filepath, script_path)
        return result
    except json.JSONDecodeError:
        # 输出可能包含多行，取最后一行 JSON
        for line in reversed(output.split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except:
                    pass
        return _generate_fallback_result(filepath, script_path)


def _generate_fallback_result(filepath: str, script_type: str) -> dict:
    """超时/出错时生成无标注的降级结果，让用户能手动操作"""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="babo_")
    png_path = str(Path(tmpdir) / "drawing.png")

    try:
        if "pdf" in script_type:
            import fitz
            doc = fitz.open(filepath)
            page = doc[0]
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat)
            pix.save(png_path)
            w, h = pix.width, pix.height
            doc.close()
            return {"image_path": png_path, "dimensions": [], "width": w, "height": h, "source_format": "pdf"}
        elif "image" in script_type or "img" in script_type:
            from PIL import Image
            img = Image.open(filepath)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(png_path, "PNG")
            return {"image_path": png_path, "dimensions": [], "width": img.width, "height": img.height, "source_format": "image"}
        else:
            # DXF fallback - 生成空白图
            from PIL import Image as PILImage
            img = PILImage.new("RGB", (1000, 800), "white")
            img.save(png_path, "PNG")
            return {"image_path": png_path, "dimensions": [], "width": 1000, "height": 800, "source_format": "dxf"}
    except Exception as e:
        print(f"[ERROR] 降级结果生成失败: {e}")
        return {"image_path": png_path, "dimensions": [], "width": 1000, "height": 800, "source_format": "unknown"}


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "BaboLocal"}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), mode: str = "ocr"):
    """上传文件，自动识别格式并提取尺寸标注。
    mode: "ocr" (快速本地) 或 "vlm" (AI 精准)"""
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

    # 在独立子进程中运行解析（带超时保护，超时可真正 kill 进程）
    try:
        if ext == ".dxf":
            result = await _run_extraction_subprocess("dxf_handler", str(save_path), recog_mode=mode)
        elif ext == ".pdf":
            result = await _run_extraction_subprocess("pdf_handler", str(save_path), recog_mode=mode)
        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
            result = await _run_extraction_subprocess("pdf_handler_img", str(save_path), recog_mode=mode)
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
    # 导出图片：EXE 模式直接调用，开发模式用子进程
    import tempfile, json as _json
    from exporter import export_ballooned_image as _export_fn

    if hasattr(sys, '_MEIPASS'):
        # EXE 模式：直接进程内调用
        try:
            import asyncio as _asyncio
            data = await _asyncio.wait_for(
                _asyncio.to_thread(
                    _export_fn,
                    proj["image_path"], proj["dimensions"],
                    req.bubble_scale, req.bubble_shape, req.colors,
                ),
                timeout=120,
            )
        except _asyncio.TimeoutError:
            raise HTTPException(500, "导出图片超时，请减少标注数量后重试")
        except Exception as e:
            raise HTTPException(500, f"导出图片失败: {str(e)}")
        return Response(
            content=data,
            media_type="image/png",
            headers={"Content-Disposition": "attachment; filename=ballooned_drawing.png"},
        )

    venv_python = str(Path(__file__).parent.parent / "venv" / "bin" / "python")
    tmpdir = tempfile.mkdtemp(prefix="babo_export_")
    output_png = str(Path(tmpdir) / "output.png")

    script = f"""
import sys, json, traceback
sys.path.insert(0, {str(Path(__file__).parent)!r})
try:
    from exporter import export_ballooned_image
    data = export_ballooned_image(
        {proj["image_path"]!r},
        {proj["dimensions"]!r},
        {req.bubble_scale!r},
        {req.bubble_shape!r},
        {req.colors!r},
    )
    with open({output_png!r}, "wb") as f:
        f.write(data)
    print("OK")
except Exception as e:
    traceback.print_exc()
    print("ERROR:" + str(e))
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            venv_python, "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent.parent),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        out = stdout.decode("utf-8", errors="replace").strip()
        if "OK" not in out or not Path(output_png).exists():
            err = stderr.decode("utf-8", errors="replace")[-500:]
            print(f"[ERROR] 导出图片失败: {out} | stderr: {err}")
            raise HTTPException(500, f"导出图片失败")
        with open(output_png, "rb") as f:
            data = f.read()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(500, "导出图片超时，请减少标注数量后重试")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"导出图片失败: {str(e)}")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Content-Disposition": "attachment; filename=ballooned_drawing.png"},
    )


# 静态文件服务前端
frontend_dir = _app_path("frontend")
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9527,
                timeout_keep_alive=300)
