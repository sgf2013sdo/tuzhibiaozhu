"""生成测试图片 - 模拟工程图纸，用 Pillow 画一个带尺寸标注的简单零件图"""
from PIL import Image, ImageDraw, ImageFont
import math

W, H = 1200, 900
img = Image.new("RGB", (W, H), "white")
draw = ImageDraw.Draw(img)

try:
    font_l = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    font_m = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
except Exception:
    font_l = font_s = font_m = ImageFont.load_default()

# 画一个零件轮廓（矩形+圆孔）
# 外轮廓
draw.rectangle([200, 200, 900, 650], outline="black", width=3)
# 4个圆孔
for cx, cy in [(300, 300), (800, 300), (300, 550), (800, 550)]:
    draw.ellipse([cx-30, cy-30, cx+30, cy+30], outline="black", width=2)
# 中心大圆
draw.ellipse([500-80, 400-80, 500+80, 400+80], outline="black", width=3)
# 中心线
draw.line([500, 150, 500, 700], fill="gray", width=1)
draw.line([100, 400, 1000, 400], fill="gray", width=1)

# 尺寸标注
# 水平总宽 700
draw.line([200, 720, 900, 720], fill="blue", width=1)
draw.line([200, 710, 200, 730], fill="blue", width=1)
draw.line([900, 710, 900, 730], fill="blue", width=1)
draw.text((520, 725), "700", fill="blue", font=font_l)

# 垂直总高 450
draw.line([950, 200, 950, 650], fill="blue", width=1)
draw.line([940, 200, 960, 200], fill="blue", width=1)
draw.line([940, 650, 960, 650], fill="blue", width=1)
draw.text((960, 400), "450", fill="blue", font=font_l)

# 圆孔直径 Ø60
draw.text((260, 300), "Ø60", fill="red", font=font_l)
draw.text((760, 300), "Ø60", fill="red", font=font_l)

# 中心圆 Ø160
draw.text((460, 380), "Ø160", fill="red", font=font_m)

# 半径标注 R30
draw.text((310, 330), "R30", fill="green", font=font_s)
draw.text((810, 330), "R30", fill="green", font=font_s)

# 角度标注 45°
draw.line([200, 200, 300, 100], fill="blue", width=1)
draw.text((220, 120), "45°", fill="blue", font=font_l)

# 带公差的尺寸
draw.text((150, 260), "50±0.02", fill="purple", font=font_s)
draw.text((150, 560), "50±0.02", fill="purple", font=font_s)

# 孔距标注
draw.line([300, 270, 800, 270], fill="blue", width=1)
draw.text((520, 275), "500", fill="blue", font=font_s)

# 数量前缀
draw.text((350, 150), "4×Ø60", fill="red", font=font_m)

# 螺纹标注
draw.text((650, 680), "M8", fill="darkgreen", font=font_l)

# 倒角
draw.text((180, 180), "C2", fill="brown", font=font_s)

# 标题栏
draw.rectangle([200, 750, 700, 850], outline="black", width=2)
draw.text((220, 770), "零件名称: 测试零件", fill="black", font=font_s)
draw.text((220, 800), "材料: Q235", fill="black", font=font_s)
draw.text((220, 825), "比例: 1:2", fill="black", font=font_s)

img.save("/opt/data/babo-local/uploads/test_engineering.png")
print("测试图片已生成: test_engineering.png")
