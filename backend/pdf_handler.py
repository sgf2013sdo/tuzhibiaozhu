"""
PDF / 图片处理器
策略：
  1. PDF -> 先尝试 PyMuPDF 文本层提取（矢量PDF）
  2. 文本层为空或图片格式 -> 用 RapidOCR 识别文字
  3. 正则匹配尺寸标注模式
"""
import re
import math
import tempfile
from pathlib import Path
from typing import List, Dict
import fitz  # PyMuPDF
from PIL import Image

# OCR 引擎懒加载（首次调用才初始化，避免启动慢）
_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _parse_dim_text(text: str) -> dict:
    """解析尺寸文字，提取名义值、上下偏差、类型符号"""
    if not text:
        return {}
    text = text.strip()
    # 清理 OCR 常见误读
    # Ø 被读成 0 开头的数字 -> 如 060 可能是 Ø60, 0160 可能是 Ø160
    # 如果数字以 0 开头且后面有2+位数字，可能是 Ø 被读成了 0
    text = text.replace("⊙", "Ø").replace("◎", "Ø")
    # 修复 OCR 把 Ø 读成 0 的情况：0后面紧跟数字且整体看起来像直径
    # 例如 "060" -> "Ø60", "0160" -> "Ø160", "010" -> "Ø10"
    text = re.sub(r'^0(\d{1,3})\b', r'Ø\1', text)
    # 数量前缀中的 0 也修复: 4×060 -> 4×Ø60
    text = re.sub(r'(\d+\s*[×xX*]\s*)0(\d{1,3})\b', r'\1Ø\2', text)
    # 修复 ± 被读成 + 或其他符号
    text = text.replace("土", "±").replace("干", "±")
    # 修复小写 φ 被 OCR 读成 $ 或其它符号的情况
    text = re.sub(r'\$(\d)', r'φ\1', text)
    # 修复 Φ/φ 被 OCR 读成 O 开头的情况（如 O10 -> Ø10）
    text = re.sub(r'^O(\d{1,3})\b', r'Ø\1', text)
    text = re.sub(r'^o(\d{1,3})\b', r'Ø\1', text)
    # 修复 Φ/φ 前面多了多余字符的情况
    text = re.sub(r'^[^\dØ⌀ΦφRMC°%%]*([Ø⌀Φφ])', r'\1', text)
    # 修复 R 前面多了多余字符的情况（确保半径 R 不被直径修复正则干扰）
    text = re.sub(r'^[^Ø⌀ΦφRMC°%%\d\s]+(R\s*\d)', r'\1', text)
    result = {"raw": text}

    # 直径
    if any(c in text for c in ["Ø", "⌀", "Φ", "φ", "%%C", "%%c"]):
        result["type"] = "diameter"
        result["symbol"] = "Ø"
    elif re.match(r'(\d+\s*[×xX*]\s*)?R\s*\d', text):
        result["type"] = "radius"
        result["symbol"] = "R"
    elif "°" in text or "%%D" in text:
        result["type"] = "angle"
        result["symbol"] = "°"
    elif re.match(r'(\d+\s*[×xX*]\s*)?M\s*\d', text):
        result["type"] = "thread"
        result["symbol"] = "M"
    elif re.match(r'(\d+\s*[×xX*]\s*)?C\s*\d', text):
        result["type"] = "chamfer"
        result["symbol"] = "C"
    else:
        result["type"] = "linear"
        result["symbol"] = ""

    clean = text
    for sym in ["Ø", "⌀", "%%C", "%%c", "Φ", "φ", "°", "%%D"]:
        clean = clean.replace(sym, "")
    if result["type"] == "radius":
        clean = re.sub(r"^R\s*", "", clean)
    if result["type"] == "chamfer":
        clean = re.sub(r"^C\s*", "", clean)
    if result["type"] == "thread":
        clean = re.sub(r"^M\s*", "", clean)

    # 数量前缀
    qty_match = re.match(r"(\d+)\s*[×xX*]\s*(.+)", clean)
    if qty_match:
        result["quantity"] = int(qty_match.group(1))
        clean = qty_match.group(2)

    # 公差 ±
    tol_match = re.search(r"([+-]?\d+\.?\d*)\s*±\s*(\d+\.?\d*)", clean)
    if tol_match:
        result["nominal"] = tol_match.group(1).lstrip("+")
        result["upper_tol"] = f"+{tol_match.group(2)}"
        result["lower_tol"] = f"-{tol_match.group(2)}"
    else:
        # 上下偏差 +0.02/-0.01
        tol_match2 = re.search(
            r"(\d+\.?\d*)\s*\(?\s*\+?\s*(\d+\.?\d*)\s*/\s*-?\s*(\d+\.?\d*)\s*\)?", clean
        )
        if tol_match2:
            result["nominal"] = tol_match2.group(1)
            result["upper_tol"] = f"+{tol_match2.group(2)}"
            result["lower_tol"] = f"-{tol_match2.group(3)}"
        else:
            num_match = re.search(r"(\d+\.?\d*)", clean)
            if num_match:
                result["nominal"] = num_match.group(1)
                result["upper_tol"] = ""
                result["lower_tol"] = ""

    return result


# ===== 尺寸标注正则模式 =====
# 匹配工程图纸中常见的尺寸标注文字
DIM_PATTERNS = [
    # 直径+公差: Ø25±0.02, Φ50, ⌀10
    re.compile(r"[Ø⌀Φφ⊙◎O]\s*\d+\.?\d*\s*(?:±\s*\d+\.?\d*)?"),
    # 半径: R25, R15±0.1
    re.compile(r"R\s*\d+\.?\d*\s*(?:±\s*\d+\.?\d*)?"),
    # 角度: 45°, 30.5°
    re.compile(r"\d+\.?\d*\s*°"),
    # 倒角: C2, C1.5
    re.compile(r"C\s*\d+\.?\d*"),
    # 螺纹: M8, M10×1.5
    re.compile(r"M\s*\d+\.?\d*(?:\s*[×xX]\s*\d+\.?\d*)?"),
    # 数量前缀: 4×Ø10, 3X26
    re.compile(r"\d+\s*[×xX*]\s*[Ø⌀RMC]?\s*\d+\.?\d*"),
    # 线性尺寸+公差: 50±0.05
    re.compile(r"\d+\.?\d*\s*±\s*\d+\.?\d*"),
    # 上下偏差: 30(+0.05/-0.02)
    re.compile(r"\d+\.?\d*\s*\(\s*\+\d+\.?\d*\s*/\s*-\d+\.?\d*\s*\)"),
    # 纯数字尺寸（1-5位整数或小数，排除太小或太大的数）
    re.compile(r"\b\d{1,5}(?:\.\d{1,3})?\b"),
]


def _is_dimension_like(text: str) -> bool:
    """判断文本是否像尺寸标注"""
    text = text.strip()
    if not text or len(text) > 60:
        return False
    if not re.search(r"\d", text):
        return False
    # 排除日期/时间
    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", text):
        return False
    # 排除比例（如 1:2, 1:100）
    if re.match(r"^\d+\s*:\s*\d+$", text):
        return False
    # 排除包含冒号的非尺寸文字（如 "00: 1:2", "Q235"）
    if ":" in text and "°" not in text:
        return False
    # 排除包含中文的非尺寸文字（如 "材料: Q235", "零件名称: 测试零件"）
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    # 排除包含 "Q" 开头的材料标注（如 Q235）
    if re.match(r"^Q\d", text):
        return False
    # 排除纯 0
    if text == "0" or text == "00" or text == "000":
        return False
    # 排除全是相同数字的（如 0000）
    if len(text) >= 3 and len(set(text.replace(".", ""))) == 1:
        return False
    return True


def _filter_and_parse_ocr_results(ocr_results: list, scale_factor: float = 1.0) -> list:
    """
    从 OCR 结果中筛选尺寸标注
    ocr_results: [[bbox, text, confidence], ...]
    返回: [dimension_dict, ...]
    """
    dimensions = []
    dim_id = 0
    seen_texts = set()

    for item in ocr_results:
        if not item or len(item) < 3:
            continue
        bbox, text, conf = item[0], item[1], item[2]
        if not text or not isinstance(text, str):
            continue

        text = text.strip()
        if not _is_dimension_like(text):
            continue

        # 去重
        if text in seen_texts:
            continue

        parsed = _parse_dim_text(text)
        if not parsed.get("nominal") and not parsed.get("raw"):
            continue

        seen_texts.add(text)

        # 计算中心坐标
        cx, cy = 0, 0
        try:
            if bbox and len(bbox) >= 4:
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                cx = int((min(xs) + max(xs)) / 2 * scale_factor)
                cy = int((min(ys) + max(ys)) / 2 * scale_factor)
        except Exception:
            cx, cy = 0, 0

        dim_id += 1
        dimensions.append({
            "id": dim_id,
            "raw_text": text,
            "type": parsed.get("type", "text"),
            "symbol": parsed.get("symbol", ""),
            "nominal": parsed.get("nominal", ""),
            "upper_tol": parsed.get("upper_tol", ""),
            "lower_tol": parsed.get("lower_tol", ""),
            "quantity": parsed.get("quantity", 1),
            "x": cx,
            "y": cy,
            "note": "",
        })

    return dimensions


def _ocr_image(img_path: str, render_scale: float = 1.0) -> list:
    """对图片执行 OCR，返回 dimensions 列表"""
    ocr = _get_ocr()
    # 对大图做缩限，避免 OCR 太慢导致超时
    img = Image.open(img_path)
    max_dim = 2000  # 最大边长 2000px
    orig_w, orig_h = img.width, img.height
    resize_scale = 1.0  # 缩放到 OCR 图的坐标映射因子
    if orig_w > max_dim or orig_h > max_dim:
        resize_scale = max_dim / max(orig_w, orig_h)
        new_w = int(orig_w * resize_scale)
        new_h = int(orig_h * resize_scale)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        tmp_path = img_path.replace(".png", "_resized.png")
        img_resized.save(tmp_path, "PNG")
        print(f"[INFO] 图片缩放: {orig_w}x{orig_h} -> {new_w}x{new_h}")
        result, _ = ocr(str(tmp_path))
        # OCR 坐标基于缩放后图片，需乘以放大因子恢复到原始坐标
        # 再乘以 render_scale（PDF 渲染时的 DPI 缩放）
        scale_factor = (1.0 / resize_scale) * render_scale
    else:
        result, _ = ocr(str(img_path))
        scale_factor = render_scale

    if not result:
        return []

    return _filter_and_parse_ocr_results(result, scale_factor=scale_factor)


def extract_from_pdf(filepath: str, page_num: int = 0) -> dict:
    """
    从 PDF 文件提取尺寸标注
    策略：先文本层，后 OCR
    """
    filepath = str(filepath)
    doc = fitz.open(filepath)
    page = doc[page_num] if page_num < len(doc) else doc[0]

    # 渲染为图片（控制 DPI 避免超大图 OCR 超时）
    render_dpi = 200  # 200 DPI 够 OCR 用，300 太大容易超时
    mat = fitz.Matrix(render_dpi / 72, render_dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    tmpdir = tempfile.mkdtemp(prefix="babo_")
    png_path = str(Path(tmpdir) / "drawing.png")
    pix.save(png_path)

    img_width = pix.width
    img_height = pix.height
    render_scale = render_dpi / 72  # 文本坐标 -> 图片像素的缩放比

    dimensions = []

    # ===== 第一步：尝试 PyMuPDF 文本层提取（矢量PDF） =====
    try:
        blocks = page.get_text("dict")["blocks"]
        dim_id = 0
        for block in blocks:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                line_text = ""
                line_x = line_y = 0
                for span in line["spans"]:
                    line_text += span["text"]
                    bbox = span["bbox"]
                    line_x = (bbox[0] + bbox[2]) / 2
                    line_y = (bbox[1] + bbox[3]) / 2

                line_text = line_text.strip()
                if not _is_dimension_like(line_text):
                    continue

                parsed = _parse_dim_text(line_text)
                if not parsed.get("nominal"):
                    continue

                dim_id += 1
                dimensions.append({
                    "id": dim_id,
                    "raw_text": line_text,
                    "type": parsed.get("type", "text"),
                    "symbol": parsed.get("symbol", ""),
                    "nominal": parsed.get("nominal", ""),
                    "upper_tol": parsed.get("upper_tol", ""),
                    "lower_tol": parsed.get("lower_tol", ""),
                    "quantity": parsed.get("quantity", 1),
                    "x": line_x * render_scale,
                    "y": line_y * render_scale,
                    "note": "",
                })
    except Exception as e:
        print(f"[WARN] PDF文本层提取失败: {e}")

    # ===== 第二步：如果文本层没提取到，用 OCR =====
    if not dimensions:
        print("[INFO] PDF文本层无尺寸标注，启用 OCR 识别...")
        try:
            dimensions = _ocr_image(png_path, render_scale=1.0)
        except Exception as e:
            print(f"[ERROR] OCR 失败: {e}")

    doc.close()

    return {
        "image_path": png_path,
        "dimensions": dimensions,
        "width": img_width,
        "height": img_height,
        "source_format": "pdf",
    }


def extract_from_image(filepath: str) -> dict:
    """
    从图片文件提取尺寸标注
    使用 RapidOCR 识别文字
    """
    filepath = str(filepath)
    img = Image.open(filepath)
    if img.mode != "RGB":
        img = img.convert("RGB")

    tmpdir = tempfile.mkdtemp(prefix="babo_")
    png_path = str(Path(tmpdir) / "drawing.png")
    img.save(png_path, "PNG")

    dimensions = []
    try:
        print("[INFO] 启用 OCR 识别图片尺寸标注...")
        dimensions = _ocr_image(png_path, render_scale=1.0)
    except Exception as e:
        print(f"[ERROR] OCR 失败: {e}")

    return {
        "image_path": png_path,
        "dimensions": dimensions,
        "width": img.width,
        "height": img.height,
        "source_format": "image",
    }
