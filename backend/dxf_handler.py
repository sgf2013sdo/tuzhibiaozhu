"""
DXF 文件处理器 - 使用 ezdxf 解析工程图纸中的尺寸标注
提取：线性尺寸、直径、半径、角度、公差等
"""
import math
import re
from pathlib import Path
from typing import Optional
import ezdxf


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_dimension_text(text: str) -> dict:
    """解析尺寸文字，提取名义值、上下偏差、类型符号"""
    if not text:
        return {}
    text = text.strip()
    result = {"raw": text}

    # 直径 Ø / ⌀ / Φ / φ / %%C
    if any(c in text for c in ["Ø", "⌀", "Φ", "φ", "%%C", "%%c"]):
        result["type"] = "diameter"
        result["symbol"] = "Ø"
    # 半径 R（支持数量前缀如 4×R25）
    elif re.match(r'(\d+\s*[×xX*]\s*)?R\s*\d', text):
        result["type"] = "radius"
        result["symbol"] = "R"
    # 角度
    elif "°" in text or "%%D" in text:
        result["type"] = "angle"
        result["symbol"] = "°"
    # 螺纹 M8, M10×1.5（支持数量前缀）
    elif re.match(r'(\d+\s*[×xX*]\s*)?M\s*\d', text):
        result["type"] = "thread"
        result["symbol"] = "M"
    # 倒角 C（支持数量前缀）
    elif re.match(r'(\d+\s*[×xX*]\s*)?C\s*\d', text):
        result["type"] = "chamfer"
        result["symbol"] = "C"
    else:
        result["type"] = "linear"
        result["symbol"] = ""

    # 提取名义尺寸数值
    clean = text
    for sym in ["Ø", "⌀", "%%C", "%%c", "Φ", "φ", "°", "%%D"]:
        clean = clean.replace(sym, "")
    if result["type"] == "radius":
        clean = re.sub(r"^R\s*", "", clean)
    if result["type"] == "chamfer":
        clean = re.sub(r"^C\s*", "", clean)
    if result["type"] == "thread":
        clean = re.sub(r"^M\s*", "", clean)

    # 数量前缀 如 3×26 / 4×Ø8 / 4×R25
    qty_match = re.match(r"(\d+)\s*[×xX*]\s*(.+)", clean)
    if qty_match:
        result["quantity"] = int(qty_match.group(1))
        clean = qty_match.group(2)

    # 上下偏差 如 50±0.02 或 50 +0.02/-0.01 或 50(+0.02/-0.01)
    # 先处理 ± 符号
    tol_match = re.search(r"([+-]?\d+\.?\d*)\s*±\s*(\d+\.?\d*)", clean)
    if tol_match:
        result["nominal"] = tol_match.group(1).lstrip("+")
        result["upper_tol"] = f"+{tol_match.group(2)}"
        result["lower_tol"] = f"-{tol_match.group(2)}"
    else:
        # 上下偏差独立 如 50(+0.02/-0.01) 或分行
        tol_match2 = re.search(r"([+-]?\d+\.?\d*)\s*\(?\s*\+(\d+\.?\d*)\s*/\s*-(\d+\.?\d*)\s*\)?", clean)
        if tol_match2:
            result["nominal"] = tol_match2.group(1).lstrip("+")
            result["upper_tol"] = f"+{tol_match2.group(2)}"
            result["lower_tol"] = f"-{tol_match2.group(3)}"
        else:
            # 没有公差
            num_match = re.search(r"([+-]?\d+\.?\d*)", clean)
            if num_match:
                result["nominal"] = num_match.group(1).lstrip("+")
                result["upper_tol"] = ""
                result["lower_tol"] = ""

    return result


def _get_text_location(entity, msp) -> tuple:
    """获取文字/标注的大致坐标"""
    try:
        if hasattr(entity, "dxf") and hasattr(entity.dxf, "insert"):
            insert = entity.dxf.insert
            return (_safe_float(insert.x), _safe_float(insert.y))
    except Exception:
        pass
    try:
        if hasattr(entity, "dxf") and hasattr(entity.dxf, "midpoint"):
            mp = entity.dxf.midpoint
            return (_safe_float(mp.x), _safe_float(mp.y))
    except Exception:
        pass
    return (0.0, 0.0)


def _render_dxf_with_pillow(msp, png_path: str, width: int, height: int) -> str:
    """用 Pillow 手绘 DXF 基本图元（LINE/LWPOLYLINE/CIRCLE/ARC/TEXT）作为回退方案"""
    from PIL import Image, ImageDraw, ImageFont

    # 计算实际绘图范围
    all_points = []
    for entity in msp:
        try:
            if entity.dxftype() == "LINE":
                all_points.append((entity.dxf.start.x, entity.dxf.start.y))
                all_points.append((entity.dxf.end.x, entity.dxf.end.y))
            elif entity.dxftype() == "LWPOLYLINE":
                for pt in entity.get_points():
                    all_points.append((pt[0], pt[1]))
            elif entity.dxftype() in ("CIRCLE", "ARC"):
                cx, cy = entity.dxf.center.x, entity.dxf.center.y
                r = entity.dxf.radius
                all_points.append((cx - r, cy - r))
                all_points.append((cx + r, cy + r))
            elif entity.dxftype() == "TEXT":
                ins = entity.dxf.insert
                all_points.append((ins.x, ins.y))
                all_points.append((ins.x + 50, ins.y + 10))
        except Exception:
            continue

    if not all_points:
        all_points = [(0, 0), (width, height)]

    min_x = min(p[0] for p in all_points) - 20
    max_x = max(p[0] for p in all_points) + 20
    min_y = min(p[1] for p in all_points) - 20
    max_y = max(p[1] for p in all_points) + 20

    draw_w = max_x - min_x
    draw_h = max_y - min_y

    # 缩放到合理画布大小
    scale = min(1600 / draw_w, 1200 / draw_h, 5.0)
    img_w = int(draw_w * scale)
    img_h = int(draw_h * scale)

    img = Image.new("RGB", (img_w, img_h), "white")
    draw = ImageDraw.Draw(img)

    # 加载字体
    font_size = max(10, int(12 * scale / 3))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    def to_px(wx, wy):
        px = int((wx - min_x) * scale)
        py = img_h - int((wy - min_y) * scale)  # Y 翻转
        return (px, py)

    for entity in msp:
        try:
            etype = entity.dxftype()
            if etype == "LINE":
                draw.line([to_px(entity.dxf.start.x, entity.dxf.start.y),
                           to_px(entity.dxf.end.x, entity.dxf.end.y)],
                          fill="black", width=max(1, int(scale / 5)))
            elif etype == "LWPOLYLINE":
                pts = [to_px(p[0], p[1]) for p in entity.get_points()]
                if len(pts) >= 2:
                    draw.line(pts + [pts[0]], fill="black", width=max(1, int(scale / 5)))
            elif etype == "CIRCLE":
                cx, cy = to_px(entity.dxf.center.x, entity.dxf.center.y)
                r = int(entity.dxf.radius * scale)
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=max(1, int(scale / 5)))
            elif etype == "ARC":
                import math as _math
                cx, cy = to_px(entity.dxf.center.x, entity.dxf.center.y)
                r = int(entity.dxf.radius * scale)
                a1 = _math.radians(entity.dxf.start_angle)
                a2 = _math.radians(entity.dxf.end_angle)
                # 用多段线近似
                steps = 30
                arc_pts = []
                for i in range(steps + 1):
                    a = a1 + (a2 - a1) * i / steps
                    px = cx + int(r * _math.cos(a))
                    py = cy - int(r * _math.sin(a))
                    arc_pts.append((px, py))
                if len(arc_pts) >= 2:
                    draw.line(arc_pts, fill="black", width=max(1, int(scale / 5)))
            elif etype == "TEXT":
                px, py = to_px(entity.dxf.insert.x, entity.dxf.insert.y)
                text = entity.dxf.text or ""
                draw.text((px, py - font_size), text, fill="black", font=font)
        except Exception:
            continue

    img.save(png_path, "PNG")
    print(f"[INFO] Pillow 回退渲染完成: {img_w}x{img_h}")
    # 返回 PNG 路径 + 世界坐标边界，供后续标注坐标变换使用
    return png_path, (min_x, max_x, min_y, max_y, img_w, img_h)


def extract_dimensions_from_dxf(filepath: str) -> dict:
    """
    从 DXF 文件中提取所有尺寸标注和文字信息
    返回: {"image_path": str, "dimensions": [...], "width": int, "height": int}
    """
    filepath = str(filepath)
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()

    # 渲染 DXF 为 PNG 图片（用于前端显示）
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="babo_")
    png_path = str(Path(tmpdir) / "drawing.png")

    # 获取图纸边界
    try:
        extmin = doc.header.get("$EXTMIN")
        extmax = doc.header.get("$EXTMAX")
        if extmin and extmax and len(extmin) >= 2 and len(extmax) >= 2:
            w = int(_safe_float(extmax[0]) - _safe_float(extmin[0]))
            h = int(_safe_float(extmax[1]) - _safe_float(extmin[1]))
            width = w if w > 0 else 1000
            height = h if h > 0 else 800
        else:
            width = 1000
            height = 800
    except Exception:
        width = 1000
        height = 800

    # 优先用 matplotlib 渲染，失败则回退到 Pillow 手绘
    # 统一收集 bounds: (min_x, max_x, min_y, max_y, png_w, png_h)
    render_bounds = None
    rendered = False
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import RenderContext, Frontend
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

        fig = plt.figure(figsize=(16, 12), dpi=150)
        ax = fig.add_subplot(111)
        ax.set_axis_off()  # 不显示坐标轴和刻度，让绘图区域占满
        ctx = RenderContext(doc)
        out = MatplotlibBackend(ax)
        Frontend(ctx, out).draw_layout(msp, finalize=True)

        # 保存前获取 axes 的世界坐标范围（数据坐标系）
        ax_xmin, ax_xmax = ax.get_xlim()
        ax_ymin, ax_ymax = ax.get_ylim()

        # 让 axes 填满整个 figure，不用 tight bbox
        # 这样数据范围 (ax_xmin..ax_xmax, ax_ymin..ax_ymax) 精确映射到 PNG 像素范围
        ax.set_position([0, 0, 1, 1])  # axes 占满整个 figure
        fig.savefig(png_path, dpi=150, pad_inches=0)
        plt.close(fig)
        rendered = True

        # 测量实际 PNG 像素尺寸
        from PIL import Image as _PILImage
        _rendered_img = _PILImage.open(png_path)
        png_w, png_h = _rendered_img.size
        # axes 占满整个 figure，所以数据范围直接映射到 PNG 像素范围
        render_bounds = (ax_xmin, ax_xmax, ax_ymin, ax_ymax, png_w, png_h)
    except Exception as e:
        print(f"[WARN] matplotlib 渲染失败，回退到 Pillow: {e}")

    if not rendered:
        png_path, render_bounds = _render_dxf_with_pillow(msp, png_path, width, height)

    # ===== 统一坐标变换：DXF 世界坐标 -> PNG 像素坐标 =====
    # render_bounds = (min_x, max_x, min_y, max_y, png_w, png_h)
    bx_min, bx_max, by_min, by_max, png_w, png_h = render_bounds
    range_x = bx_max - bx_min if (bx_max - bx_min) != 0 else 1
    range_y = by_max - by_min if (by_max - by_min) != 0 else 1

    def world_to_pixel(wx, wy):
        """DXF 世界坐标 -> PNG 像素坐标（含 Y 翻转）"""
        px = (wx - bx_min) / range_x * png_w
        py = (1 - (wy - by_min) / range_y) * png_h
        return (int(px), int(py))

    dimensions = []
    dim_id = 0

    # 1. 提取 DIMENSION 实体
    for dim in msp.query("DIMENSION"):
        try:
            dim_type = dim.dimtype
            text_content = ""
            if hasattr(dim.dxf, "text"):
                text_content = dim.dxf.text or ""

            # 如果 text 是空或 "<>"，尝试从关联的 TEXT/MTEXT 获取
            if not text_content or text_content == "<>":
                text_content = getattr(dim, "get_text", lambda: "")() or ""

            if not text_content:
                continue

            parsed = _parse_dimension_text(text_content)
            if not parsed.get("nominal"):
                continue

            location = _get_text_location(dim, msp)
            px, py = world_to_pixel(location[0], location[1])

            dim_id += 1
            dimensions.append({
                "id": dim_id,
                "raw_text": text_content,
                "type": parsed.get("type", "linear"),
                "symbol": parsed.get("symbol", ""),
                "nominal": parsed.get("nominal", ""),
                "upper_tol": parsed.get("upper_tol", ""),
                "lower_tol": parsed.get("lower_tol", ""),
                "quantity": parsed.get("quantity", 1),
                "x": px,
                "y": py,
                "note": "",
            })
        except Exception as e:
            print(f"[WARN] DIMENSION parse error: {e}")
            continue

    # 2. 提取 TEXT / MTEXT 实体（补充可能遗漏的标注）
    for entity in msp.query("TEXT MTEXT"):
        try:
            text_content = ""
            if entity.dxftype() == "TEXT":
                text_content = entity.dxf.text or ""
            else:  # MTEXT
                text_content = entity.text or ""
                # 去除格式控制码
                text_content = re.sub(r"\\[A-Za-z][^;]*;", "", text_content)
                text_content = re.sub(r"[{}]", "", text_content)

            text_content = text_content.strip()
            if not text_content:
                continue

            parsed = _parse_dimension_text(text_content)

            # 只保留看起来像尺寸标注的文字（包含数字）
            if not parsed.get("nominal") and not re.search(r"\d", text_content):
                continue

            # 避免与 DIMENSION 重复
            if any(d["raw_text"] == text_content for d in dimensions):
                continue

            location = _get_text_location(entity, msp)
            px, py = world_to_pixel(location[0], location[1])

            dim_id += 1
            dimensions.append({
                "id": dim_id,
                "raw_text": text_content,
                "type": parsed.get("type", "text"),
                "symbol": parsed.get("symbol", ""),
                "nominal": parsed.get("nominal", ""),
                "upper_tol": parsed.get("upper_tol", ""),
                "lower_tol": parsed.get("lower_tol", ""),
                "quantity": parsed.get("quantity", 1),
                "x": px,
                "y": py,
                "note": "",
            })
        except Exception as e:
            print(f"[WARN] TEXT/MTEXT parse error: {e}")
            continue

    return {
        "image_path": png_path,
        "dimensions": dimensions,
        "width": png_w,
        "height": png_h,
        "source_format": "dxf",
    }
