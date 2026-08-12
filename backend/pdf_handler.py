"""
PDF / 图片处理器
策略：
  1. PDF -> 先尝试 PyMuPDF 文本层提取（矢量PDF）
  2. 文本层为空或图片格式 -> 用 RapidOCR 识别文字
  3. 可选 VLM 增强：OCR 粗提取 → GLM-4V 语义分类 → 合并输出
"""
import re
import math
import json
import os
import base64
import tempfile
from pathlib import Path
from typing import List, Dict, Optional
import fitz  # PyMuPDF
from PIL import Image

# OCR 引擎懒加载（首次调用才初始化，避免启动慢）
_ocr_engine = None

# GLM-4V API 配置（运行时从环境变量读取，用户可在前端配置）
def _glm_api_key():
    return os.environ.get("GLM_API_KEY", "")

def _glm_base_url():
    return os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")

def _glm_model():
    return os.environ.get("GLM_MODEL", "glm-4v")


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
    text = text.replace("⊙", "◎").replace("⌀", "Ø")  # ⊙是同轴度符号, ⌀是直径
    # OCR 常把 Ø 读成 @（如 @103±0.10）
    text = re.sub(r'@(?=\s*\d)', 'Ø', text)
    # 修复 @ 被 OCR 放到数字后面（如 08@ -> Ø80, 103@ -> Ø103）
    # 08@ = Ø符号被读成0放开头 + 末位数字0被读成@ -> 恢复为 Ø80
    # 必须放在 0 前缀转 Ø 规则之前，否则 08@ 会被先转成 Ø8@
    text = re.sub(r'^0(\d+)@$', lambda m: 'Ø' + m.group(1) + '0', text)
    text = re.sub(r'^(\d+)@', r'Ø\1', text)
    # 修复 OCR 把 Ø 读成 0 的情况：0后面紧跟数字且整体看起来像直径
    # 例如 "060" -> "Ø60", "0160" -> "Ø160", "010" -> "Ø10"
    # 排除小数（0.05 / 00.05 是公差值，不是 Ø 误读）
    # 00.05 = 符号⌁读成0 + 值0.05，先删一个前导0，再判断直径
    text = re.sub(r'^0(?=0\.\d)', '', text)  # 00.05 -> 0.05；0.05 不受影响
    text = re.sub(r'^0(?!\.)(\d{1,3})\b', r'Ø\1', text)
    # 数量前缀中的 0 也修复: 4×060 -> 4×Ø60
    text = re.sub(r'(\d+\s*[×xX*]\s*)0(\d{1,3})\b', r'\1Ø\2', text)
    # 修复 ± 被读成 + 或其他符号
    text = text.replace("土", "±").replace("干", "±")
    # 修复 @ 被 OCR 放到数字后面（如 08@ -> Ø80, 103@ -> Ø103）
    # 08@ = Ø符号被读成0放开头 + 末位数字0被读成@ -> 恢复为 Ø80
    text = re.sub(r'^0(\d+)@$', lambda m: 'Ø' + m.group(1) + '0', text)
    text = re.sub(r'^(\d+)@', r'Ø\1', text)
    # 修复小写 φ 被 OCR 读成 $ 或其它符号的情况
    text = re.sub(r'\$(\d)', r'φ\1', text)
    # 修复 Φ/φ 被 OCR 读成 O 开头的情况（如 O10 -> Ø10）
    text = re.sub(r'^O(\d{1,3})\b', r'Ø\1', text)
    text = re.sub(r'^o(\d{1,3})\b', r'Ø\1', text)
    # 修复 Φ/φ 前面多了多余字符的情况
    text = re.sub(r'^[^\dØ⌀ΦφRMC°%%]*([Ø⌀Φφ])', r'\1', text)
    # 修复 R 前面多了多余字符的情况（确保半径 R 不被直径修复正则干扰）
    text = re.sub(r'^[^Ø⌀ΦφRMC°%%\d\s]+(R\s*\d)', r'\1', text)
    # 压缩数字中间的空格（OCR 常把 "92. 20" 识别成 "92. 20", "0. 096", "±0. 10"）
    text = re.sub(r'(?<=\d)\s+(?=\d)', '', text)       # 6 30 -> 630
    text = re.sub(r'(?<=\d)\s+\.', '.', text)           # 6. 30 -> 6.30
    text = re.sub(r'\.\s+(?=\d)', '.', text)            # 0. 096 -> 0.096
    text = re.sub(r'(?<=[±+])\s+(?=\d)', '', text)      # ± 0.10 -> ±0.10
    text = re.sub(r'(?<=[×xX])\s+(?=\d)', '', text)     # 4× 60 -> 4×60
    result = {"raw": text}

    # 形位公差（GD&T）：符号 + 公差值 + 基准字母
    # 例: ⌁0.05 A, ⊥0.1 A B, ∥0.02 A, ⌒0.01, ◎0.05 A
    # OCR 常把符号读成: //, ||, 11, ⌒->^, ⊥->L 等
    # 注意: ◎ 是同轴度GD&T符号，不是直径，必须在这里先匹配
    gdt_symbols = r'[⌁⊥∥⌒◎○⌭⌖⏣⌓∠⏥◇◆⌯]'
    gdt_text = text.strip()
    # 标准化 OCR 误读的符号
    gdt_text = re.sub(r'^//+', '∥', gdt_text)   # // -> 平行度
    gdt_text = re.sub(r'^\|\|', '∥', gdt_text)
    gdt_text = re.sub(r'^11(?=\s*\d)', '⌁', gdt_text)  # OCR把⌁读成11
    gdt_text = re.sub(r'^L(?=\s*0\.\d)', '⊥', gdt_text)  # OCR把⊥读成L
    # 注意: 不要在这里把 ^0 当符号替换（会把 0.05 数值吃掉变成 ⌁.05），
    # 符号完全丢失的情况由下方 fallback（值+基准字母）兜底
    # 匹配: 符号 + 公差值 + (可选)1-3个基准字母
    gdt_match = re.match(
        r'^(%s)\s*([<>]?\s*[øØ]?\s*\d+\.?\d*)\s*([A-HJ-Z](\s*[A-HJ-Z]){0,2})?$' % gdt_symbols,
        gdt_text
    )
    if gdt_match:
        result["type"] = "gdt"
        result["symbol"] = gdt_match.group(1)
        result["nominal"] = gdt_match.group(2).replace("Ø", "").replace("ø", "").replace(" ", "").lstrip("<>")
        result["datums"] = (gdt_match.group(3) or "").replace(" ", "")
        result["upper_tol"] = ""
        result["lower_tol"] = ""
        result["quantity"] = 1
        return result

    # OCR 符号丢失的 GD&T 兜底：rapidocr 字符集没有 GD&T 符号（⌁⊥∥⌒◎），
    # 符号被丢弃后只剩 "值 + 基准字母"（如 0.05 A / 0.1 A B），或符号被误读
    # 成 1/I1/0（如 ⊥0.1 -> 10.1, ∥0.02 -> I10.02, ⌁0.05 -> 0.05 A）
    gdt_text2 = gdt_text
    # 误读归一：I1 开头 -> 平行度符号被读成 I1
    gdt_text2 = re.sub(r'^I1(?=\s*\d)', '∥', gdt_text2)
    # ⊥0.1 被读成 10.1（仅当 1 后面是 0.x 小数；12.5 这类真实尺寸不能动）
    gdt_text2 = re.sub(r'^1(?=\s*0\.\d)', '⊥', gdt_text2)
    # 纯符号丢失：^值 + 基准字母 且值为典型公差量级(<=1) -> 位置度(最常见, 标记symbol为空)
    gdt_fallback = re.match(
        r'^(\d+\.?\d*)\s+([A-HJ-Z](\s*[A-HJ-Z]){0,2})$',
        gdt_text2
    )
    if gdt_fallback:
        val = float(gdt_fallback.group(1))
        # 典型 GD&T 公差值 ≤ 1mm；普通线性尺寸极少单独写 0.0X + 基准字母
        if val <= 1.0:
            result["type"] = "gdt"
            result["symbol"] = ""
            result["nominal"] = gdt_fallback.group(1)
            result["datums"] = gdt_fallback.group(2).replace(" ", "")
            result["upper_tol"] = ""
            result["lower_tol"] = ""
            result["quantity"] = 1
            return result
    # 带符号误读的：重新用 GD&T 符号匹配（gdt_text2 已归一化）
    gdt_match2 = re.match(
        r'^(%s)\s*([<>]?\s*[øØ]?\s*\d+\.?\d*)\s*([A-HJ-Z](\s*[A-HJ-Z]){0,2})?$' % gdt_symbols,
        gdt_text2
    )
    if gdt_match2:
        result["type"] = "gdt"
        result["symbol"] = gdt_match2.group(1)
        result["nominal"] = gdt_match2.group(2).replace("Ø", "").replace("ø", "").replace(" ", "").lstrip("<>")
        result["datums"] = (gdt_match2.group(3) or "").replace(" ", "")
        result["upper_tol"] = ""
        result["lower_tol"] = ""
        result["quantity"] = 1
        return result

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

    # 数量前缀（支持 4-R7.50, 4×Ø10, 8-R4, 4-Ø6.30 等）
    # 注意: "-" 后跟字母(R/C/M/Ø)才是数量; "-" 后跟数字是单边公差(如 30-0.1)
    # 用原始 text 匹配（clean 已剥离 Ø/⌀，会漏掉 4-Ø6.30）
    qty_match = re.match(r"(\d+)\s*(?:[×xX*]\s*(.+)|-\s*([RMCØ⌀Φφ])\s*(\d.*))", text)
    if qty_match:
        result["quantity"] = int(qty_match.group(1))
        if qty_match.group(2) is not None:
            clean = qty_match.group(2)
        else:
            clean = qty_match.group(3) + qty_match.group(4)  # 符号+数字（如 Ø6.30）
    # 单边公差: 30-0.1 (nominal=30, lower=-0.1) / 30+0.1 (upper=+0.1)
    # 必须在数量匹配之后、±匹配之前处理
    single_tol = re.match(r"(\d+\.?\d*)\s*([+-])\s*(\d+\.?\d*)$", clean)
    if single_tol:
        result["nominal"] = single_tol.group(1)
        if single_tol.group(2) == "+":
            result["upper_tol"] = f"+{single_tol.group(3)}"
            result["lower_tol"] = ""
        else:
            result["upper_tol"] = ""
            result["lower_tol"] = f"-{single_tol.group(3)}"
        return result

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
    # 排除英文 BOM/说明文字（PLATE, WIDTH, LENGTH, OF, LBS 等）
    # 但保留单字母前缀的尺寸符号（R49/M6/C2/Ø10）和 GD&T
    if re.search(r"[A-Za-z]{3,}", text):
        return False
    # 排除日期（12/17/2024, 2024-12-17 等）
    if re.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$", text):
        return False
    # 排除页码（1 OF 3 / PAGE 2 / SHEET 1）和公差格式说明（X.XX 0.25）
    if re.match(r"^\d+\s+OF\s+\d+$", text):
        return False
    if re.match(r"^X\.?X{1,3}\s+\d", text):
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
    # 排除 OCR 残渣：孤立小数点/公差符号（±0. / -0. / .50 / 0. / ①87.）
    if re.match(r"^[±+\-]\s*0?\.?\d*\.?$", text) and text.count(".") <= 1:
        return False  # 纯公差残渣如 ±0. -0. +0.
    if re.match(r"^\.\d+$", text):
        return False  # .50 无主数字
    if re.match(r"^\d+\.$", text):
        return False  # 0. 87. 结尾孤立小数点
    if re.match(r"^[①-⑳①②…]\d", text):
        return False  # ①87. 圆圈数字残渣
    # 纯公差碎片：±0.05 / -0.10 / +0.02（无主尺寸，是完整标注被拆出的公差部分）
    # 先去空格再判断（OCR 常输出 "±0. 05" 这种带空格形态）
    no_space = text.replace(" ", "")
    if re.match(r"^[±+\-]\d+\.?\d*$", no_space):
        return False
    return True


def _filter_and_parse_ocr_results(ocr_results: list, scale_factor: float = 1.0, img_w: int = 0, img_h: int = 0) -> list:
    """
    从 OCR 结果中筛选尺寸标注
    ocr_results: [[bbox, text, confidence], ...]
    img_w/img_h: 图片原始尺寸（用于过滤边缘位置标记框 + 右下角图签区）
    返回: [dimension_dict, ...]
    """
    dimensions = []
    dim_id = 0
    seen_texts = []  # 存 (text, cx, cy)，按空间位置去重

    # 边缘位置标记过滤：图纸外圈的位置标记框数字（1,2,3... A,B,C...）
    # 边缘宽度 = 图片宽高的 6%（位置标记通常在边框外侧的窄条内）
    edge_w = img_w * 0.06 if img_w > 0 else 0
    edge_h = img_h * 0.06 if img_h > 0 else 0

    # 底部图签区（标题栏）过滤：工程图纸标题栏通常在底部整条横带
    # （材料/名称/设计/审核/日期/图号/比例等），不做尺寸标注
    # 图签区 ≈ 底部 100% 宽 × 25% 高的横带（标准 A4/A3 标题栏比例）
    tb_h = img_h * 0.25 if img_h > 0 else 0
    tb_y0 = img_h - tb_h if img_h > 0 else 0

    for item in ocr_results:
        if not item or len(item) < 3:
            continue
        bbox, text, conf = item[0], item[1], item[2]
        if not text or not isinstance(text, str):
            continue

        text = text.strip()
        if not _is_dimension_like(text):
            continue

        parsed = _parse_dim_text(text)
        if not parsed.get("nominal") and not parsed.get("raw"):
            continue

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

        # 边缘位置标记过滤：图纸最外圈的独立短文本（位置标记框 1,2,3... A,B,C...）
        if edge_w > 0 and edge_h > 0:
            on_edge = (cx < edge_w or cx > img_w - edge_w or
                       cy < edge_h or cy > img_h - edge_h)
            if on_edge and len(text) <= 3 and re.match(r'^[\dA-Za-z]+$', text):
                continue  # 位置标记，跳过

        # 底部图签区过滤：标题栏内的文字（材料/名称/设计/审核/日期/图号/比例）
        # 尺寸标注不会出现在标题栏内部，直接整区跳过
        if tb_h > 0:
            if cy >= tb_y0:
                continue  # 图签区，跳过

        # 空间去重：相同文本且位置相近(50px内)才算重复，不同位置保留
        # 工程图纸同一数值可能出现在多处（对称孔、多处同尺寸）
        is_dup = False
        for (s_text, s_x, s_y) in seen_texts:
            if s_text == text:
                dist = ((s_x - cx) ** 2 + (s_y - cy) ** 2) ** 0.5
                if dist < 50:
                    is_dup = True
                    break
        if is_dup:
            continue

        seen_texts.append((text, cx, cy))

        dim_id += 1
        dimensions.append({
            "id": dim_id,
            "raw_text": text,
            "type": parsed.get("type", "text"),
            "symbol": parsed.get("symbol", ""),
            "nominal": parsed.get("nominal", ""),
            "datums": parsed.get("datums", ""),
            "upper_tol": parsed.get("upper_tol", ""),
            "lower_tol": parsed.get("lower_tol", ""),
            "quantity": parsed.get("quantity", 1),
            "x": cx,
            "y": cy,
            "note": "",
        })

    return dimensions


def _merge_dimension_sets(text_dims: list, ocr_dims: list) -> list:
    """
    合并文本层提取和 OCR 提取的标注结果。
    - 按 (raw_text 规范化, 位置距离) 去重
    - 位置相近(<60px)且文本相似视为同一标注，保留文本层的（坐标更准）
    """
    merged = list(text_dims)

    def norm(t):
        # 规范化文本用于比对：去空格、统一符号
        t = t.replace(" ", "").replace("@", "Ø").replace("0", "Ø") if False else t.replace(" ", "")
        return t

    for ocr_d in ocr_dims:
        ocr_text = ocr_d.get("raw_text", "")
        ocr_x, ocr_y = ocr_d.get("x", 0), ocr_d.get("y", 0)
        ocr_norm = norm(ocr_text)

        dup = False
        replace_idx = -1  # 若新标注更完整，记录要替换的碎片下标
        for idx, td in enumerate(merged):
            td_text = td.get("raw_text", "")
            td_x, td_y = td.get("x", 0), td.get("y", 0)
            # 文本完全相同 且 位置相近(<60px) 才算重复；不同位置的相同尺寸要保留
            # （图纸上对称孔/多处同尺寸会在不同位置出现相同文本）
            dist = ((td_x - ocr_x) ** 2 + (td_y - ocr_y) ** 2) ** 0.5
            if td_text == ocr_text and dist < 60:
                dup = True
                break
            if dist < 60 and td.get("nominal") == ocr_d.get("nominal") and td.get("nominal"):
                dup = True
                break
            # 旋转OCR碎片过滤：如 "±0.10" 是 "@103±0.10" 的碎片，
            # 位置相近(<150px)且一个是另一个的子串/公差碎片 -> 丢弃碎片
            if dist < 150:
                td_n = norm(td_text)
                ocr_n = norm(ocr_text)
                # 纯公差碎片（±x / +x / -x）并入完整标注
                if re.match(r'^[±+\-]\d', ocr_n) and (td_n in ocr_n or ocr_n in td_n or td.get("nominal") == ocr_d.get("nominal", "")):
                    dup = True
                    break
                if re.match(r'^[±+\-]\d', td_n) and (ocr_n in td_n or td_n in ocr_n or td.get("nominal") == ocr_d.get("nominal", "")):
                    dup = True
                    break
                # 完整标注包含碎片文本：保留更长的完整标注，替换碎片
                if len(ocr_n) > len(td_n) and td_n in ocr_n and len(td_n) >= 3:
                    replace_idx = idx
                    break
                if len(td_n) > len(ocr_n) and ocr_n in td_n and len(ocr_n) >= 3:
                    dup = True
                    break
        if replace_idx >= 0:
            merged[replace_idx] = ocr_d  # 用完整标注替换碎片
        elif not dup:
            merged.append(ocr_d)

    # 重新编号
    for i, d in enumerate(merged):
        d["id"] = i + 1
    return merged


def _vlm_classify_dimensions(png_path: str, ocr_texts: list, image_w: int, image_h: int) -> list:
    """
    VLM 增强：将图纸图片发给 GLM-4V，识别哪些文字是尺寸标注，返回结构化数据。
    返回: [{text, type, nominal, upper_tol, lower_tol, quantity, symbol}, ...]
    """
    if not _glm_api_key():
        print("[INFO] GLM_API_KEY 未配置，跳过 VLM 增强")
        return []

    try:
        # 缩放图片到 VLM 可接受的范围（最长边 1024px）
        img = Image.open(png_path)
        max_side = 1024
        if img.width > max_side or img.height > max_side:
            scale = max_side / max(img.width, img.height)
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # 转 base64
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        # 构建 prompt —— 要求返回紧凑 JSON
        # OCR 已提取的文字列表给 VLM 做参考
        ocr_hint = "\n".join(f"- {t}" for t in ocr_texts[:30]) if ocr_texts else "(无)"
        prompt = f"""你是工程图纸审图专家。请分析这张图纸上的所有尺寸标注，返回 JSON 数组。

OCR 已识别到以下文字（仅供参考，可能含非尺寸内容）:
{ocr_hint}

返回规则：
1. 只保留真正的尺寸标注（线性尺寸/直径/半径/角度/倒角/螺纹）
2. 必须排除以下内容：
   - 图纸外圈的位置序号（单独的 1,2,3... A,B,C...，通常在图纸边框外侧）
   - 右下角标题栏信息（日期如 20240406、材料牌号如 ADC12/A356.2、文档编号、图号、重量、公司名、表面处理说明）
   - 形位公差标注框（如"// 0.05 A"等）
   - 粗糙度符号（如 Ra3.2）
   - 图纸比例（如 1/1, 1:1）
3. 每个元素格式: {{"t":"原始文字","n":"名义值","u":"上偏差","l":"下偏差","tp":"类型","q":"数量"}}
4. tp 取值: linear/diameter/radius/angle/chamfer/thread
5. 偏差为空时写 ""，数量为空时写 "1"
6. 例: {{"t":"50±0.05","n":"50","u":"+0.05","l":"-0.05","tp":"linear","q":"1"}}
7. 例: {{"t":"4×Ø10","n":"10","u":"","l":"","tp":"diameter","q":"4"}}
8. 只返回 JSON 数组，不要其他文字"""

        resp = _call_glm4v(img_b64, prompt)
        if not resp:
            return []

        # 解析 JSON 响应
        result = _parse_vlm_json(resp)
        print(f"[INFO] VLM 识别到 {len(result)} 个标注")
        return result

    except Exception as e:
        print(f"[WARN] VLM 增强失败: {e}")
        return []


def _call_glm4v(img_b64: str, prompt: str) -> str:
    """调用视觉模型 API（OpenAI 兼容格式，可指向任意用户配置的服务）"""
    try:
        import urllib.request
        import urllib.error

        api_key = _glm_api_key()
        base_url = _glm_base_url().rstrip("/")
        model = _glm_model()

        data = json.dumps({
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }],
            "max_tokens": 1024,
            "temperature": 0.1,
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
            return body.get("choices", [{}])[0].get("message", {}).get("content", "")

    except urllib.error.HTTPError as e:
        print(f"[ERROR] 视觉API错误: {e.code} {e.reason}")
        return ""
    except Exception as e:
        print(f"[ERROR] 视觉API调用失败: {e}")
        return ""


def _parse_vlm_json(text: str) -> list:
    """从 VLM 响应中提取 JSON 数组"""
    if not text:
        return []
    # 去掉 markdown 代码块标记
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # 找第一个 [ ... ] 段
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            raw = json.loads(match.group())
            return raw
        except json.JSONDecodeError:
            pass
    print(f"[WARN] VLM 返回格式无法解析: {text[:200]}")
    return []


def _merge_ocr_vlm(ocr_dims: list, vlm_dims: list) -> list:
    """
    合并 OCR 定位结果和 VLM 语义结果。
    OCR 提供像素坐标 (x, y)，VLM 提供准确的语义分类。
    匹配规则：按 raw_text / nominal 模糊匹配。
    """
    if not vlm_dims:
        return ocr_dims  # VLM 失败时回退到纯 OCR

    # 预处理 VLM 结果
    vlm_map = {}
    for v in vlm_dims:
        key = v.get("t", v.get("text", ""))
        if not key:
            continue
        vlm_map[key] = v

    # 合并：OCR 为准保留坐标，语义用 VLM 覆盖
    merged = []
    matched_keys = set()
    for d in ocr_dims:
        raw = d.get("raw_text", "")
        # 精确匹配
        if raw in vlm_map:
            v = vlm_map[raw]
            matched_keys.add(raw)
            d2 = dict(d)
            d2["type"] = v.get("tp", d.get("type", "linear"))
            d2["nominal"] = v.get("n", d.get("nominal", ""))
            d2["upper_tol"] = v.get("u", d.get("upper_tol", ""))
            d2["lower_tol"] = v.get("l", d.get("lower_tol", ""))
            d2["quantity"] = int(v.get("q", d.get("quantity", 1)) or 1)
            d2["symbol"] = d2.get("symbol", "") or _derive_symbol(d2["type"])
            merged.append(d2)
        else:
            # 模糊匹配：尝试按 nominal 值和位置匹配
            ocr_nom = d.get("nominal", "")
            best = None
            for k, v in vlm_map.items():
                if k in matched_keys:
                    continue
                v_nom = v.get("n", "")
                if ocr_nom and v_nom and ocr_nom in v_nom or v_nom in ocr_nom:
                    if not best or len(v_nom) > len(best[1].get("n", "")):
                        best = (k, v)
            if best:
                k, v = best
                matched_keys.add(k)
                d2 = dict(d)
                d2["type"] = v.get("tp", d.get("type", "linear"))
                d2["nominal"] = v.get("n", d.get("nominal", ""))
                d2["upper_tol"] = v.get("u", d.get("upper_tol", ""))
                d2["lower_tol"] = v.get("l", d.get("lower_tol", ""))
                d2["quantity"] = int(v.get("q", d.get("quantity", 1)) or 1)
                d2["symbol"] = d2.get("symbol", "") or _derive_symbol(d2["type"])
                merged.append(d2)
            else:
                # VLM 没匹配到 = 不是尺寸标注，直接过滤掉
                pass

    print(f"[INFO] VLM 合并: OCR {len(ocr_dims)} → 合并后 {len(merged)}")
    return merged


def _derive_symbol(dim_type: str) -> str:
    """从类型推导符号"""
    symbols = {"diameter": "Ø", "radius": "R", "angle": "°", "chamfer": "C", "thread": "M"}
    return symbols.get(dim_type, "")


def _ocr_image(img_path: str, render_scale: float = 1.0) -> list:
    """对图片执行 OCR，返回 dimensions 列表
    支持竖向尺寸标注：旋转 +90°/-90° 各识别一次，坐标映射回原图后合并
    """
    ocr = _get_ocr()
    # 对大图做缩限，避免 OCR 太慢导致超时
    # 注意: 缩放会丢失小字号文字（如 12.50±0.1），阈值取 2500 且最低缩放比 0.75
    img = Image.open(img_path)
    max_dim = 2500  # 最大边长 2500px（原 2000 太小，缩放后小字识别丢失）
    orig_w, orig_h = img.width, img.height
    resize_scale = 1.0  # 缩放到 OCR 图的坐标映射因子
    if orig_w > max_dim or orig_h > max_dim:
        resize_scale = max_dim / max(orig_w, orig_h)
        if resize_scale < 0.75:
            resize_scale = 0.75  # 最低 0.75，避免过度缩小丢字
        new_w = int(orig_w * resize_scale)
        new_h = int(orig_h * resize_scale)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        tmp_path = img_path.replace(".png", "_resized.png")
        img_resized.save(tmp_path, "PNG")
        print(f"[INFO] 图片缩放: {orig_w}x{orig_h} -> {new_w}x{new_h}")
        result, _ = ocr(str(tmp_path))
        scale_factor = (1.0 / resize_scale) * render_scale
    else:
        result, _ = ocr(str(img_path))
        scale_factor = render_scale

    all_results = list(result) if result else []

    # 竖向文字识别：旋转 ±90° 再 OCR，bbox 映射回原图坐标
    # RapidOCR 对旋转 90° 的竖排文本识别率极低，旋转后文字变水平即可识别
    # 关键：旋转结果与 0° 结果 bbox 重叠的丢弃（那是横排文字，0° 已识别，
    # 旋转会重复识别并产生碎片如 ±0. / ①87. / 4-RO.）

    def _bbox_overlap_ratio(pts_a, pts_b):
        """计算两个 bbox 的重叠比例（基于面积交并比）"""
        try:
            ax0 = min(p[0] for p in pts_a); ax1 = max(p[0] for p in pts_a)
            ay0 = min(p[1] for p in pts_a); ay1 = max(p[1] for p in pts_a)
            bx0 = min(p[0] for p in pts_b); bx1 = max(p[0] for p in pts_b)
            by0 = min(p[1] for p in pts_b); by1 = max(p[1] for p in pts_b)
            ix0 = max(ax0, bx0); ix1 = min(ax1, bx1)
            iy0 = max(ay0, by0); iy1 = min(ay1, by1)
            if ix1 <= ix0 or iy1 <= iy0:
                return 0.0
            inter = (ix1 - ix0) * (iy1 - iy0)
            area_a = max(1, (ax1 - ax0) * (ay1 - ay0))
            area_b = max(1, (bx1 - bx0) * (by1 - by0))
            return inter / min(area_a, area_b)  # 用较小框作分母，更容易判重叠
        except Exception:
            return 0.0

    base_results = list(result) if result else []
    try:
        work_w = int(orig_w * resize_scale)
        work_h = int(orig_h * resize_scale)
        base = img_resized if resize_scale < 1.0 else img
        for angle in (90, -90):
            rot = base.rotate(angle, expand=True)
            rot_path = img_path.replace(".png", f"_rot{angle}.png")
            rot.save(rot_path, "PNG")
            rot_result, _ = ocr(str(rot_path))
            if not rot_result:
                continue
            rw, rh = rot.width, rot.height
            for item in rot_result:
                bbox, text, conf = item[0], item[1], item[2]
                # 把旋转图的 bbox 映射回原图（缩放前）坐标系
                # PIL rotate(正角度)=逆时针; rotate(-90)=顺时针
                new_pts = []
                for (px, py) in bbox:
                    if angle == 90:
                        # 逆时针90°: 原(x,y) -> 旋(x'=y, y'=H-1-x)，rot=(H,W)
                        # 逆映射: 原(x=rh-1-y', y=x')   （rh=原W）
                        nx = rh - 1 - py
                        ny = px
                    else:
                        # 顺时针90°: 原(x,y) -> 旋(x'=H-1-y, y'=x)，rot=(H,W)
                        # 逆映射: 原(x=y', y=rw-1-x')   （rw=原H）
                        nx = py
                        ny = rw - 1 - px
                    new_pts.append([nx, ny])
                # 与 0° 结果重叠过滤：重叠率高说明是横排文字重复识别，跳过
                is_dup_rot = False
                for base_item in base_results:
                    base_bbox = base_item[0]
                    if _bbox_overlap_ratio(new_pts, base_bbox) > 0.5:
                        is_dup_rot = True
                        break
                if is_dup_rot:
                    continue
                all_results.append([new_pts, text, conf])
    except Exception as e:
        print(f"[WARN] 旋转OCR失败(可忽略): {e}")

    if not all_results:
        return []

    return _filter_and_parse_ocr_results(all_results, scale_factor=scale_factor,
                                          img_w=orig_w, img_h=orig_h)


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

    # ===== 第二步：始终用 OCR 补充（文本层经常残缺，OCR 能识别更多） =====
    try:
        print("[INFO] 启用 OCR 补充识别...")
        ocr_dims = _ocr_image(png_path, render_scale=1.0)
        # 合并：OCR 结果与文本层结果去重合并（按文本+位置）
        if ocr_dims:
            dimensions = _merge_dimension_sets(dimensions, ocr_dims)
            print(f"[INFO] 合并后共 {len(dimensions)} 个标注 (文本层 {len(dimensions) - len(ocr_dims) + len([d for d in ocr_dims if d not in dimensions])} + OCR)")
    except Exception as e:
        print(f"[ERROR] OCR 补充识别失败: {e}")

    doc.close()

    # ===== 第三步：VLM 增强（可选） =====
    # OCR 已提供坐标，VLM 提供更准确的语义分类
    if os.environ.get("BABO_RECOGNITION_MODE") == "vlm":
        print("[INFO] 启用 VLM 增强识别...")
        ocr_texts = [d["raw_text"] for d in dimensions] if dimensions else []
        vlm_dims = _vlm_classify_dimensions(png_path, ocr_texts, img_width, img_height)
        if vlm_dims:
            dimensions = _merge_ocr_vlm(dimensions, vlm_dims)
        else:
            print("[WARN] VLM 无结果，保留 OCR 输出")

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

    # VLM 增强（可选）
    if os.environ.get("BABO_RECOGNITION_MODE") == "vlm":
        img_w, img_h = img.width, img.height
        print("[INFO] 启用 VLM 增强识别...")
        ocr_texts = [d["raw_text"] for d in dimensions] if dimensions else []
        vlm_dims = _vlm_classify_dimensions(png_path, ocr_texts, img_w, img_h)
        if vlm_dims:
            dimensions = _merge_ocr_vlm(dimensions, vlm_dims)
        else:
            print("[WARN] VLM 无结果，保留 OCR 输出")

    return {
        "image_path": png_path,
        "dimensions": dimensions,
        "width": img.width,
        "height": img.height,
        "source_format": "image",
    }
