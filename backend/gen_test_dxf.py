"""生成一个包含尺寸标注的测试 DXF 文件"""
import ezdxf

doc = ezdxf.new("R2010", setup=True)
msp = doc.modelspace()

# 画一个矩形
from ezdxf.math import Vec3
points = [(0, 0), (100, 0), (100, 60), (0, 60)]
msp.add_lwpolyline(points + [points[0]], close=True)

# 添加一些 TEXT 标注
texts = [
    (50, -5, "100"),
    (-5, 30, "60"),
    (30, 30, "Ø25±0.02"),
    (70, 30, "R15"),
    (50, 65, "45°"),
    (10, 50, "C2"),
    (80, 10, "M8"),
    (50, 15, "50±0.05"),
    (20, 10, "4×Ø10"),
    (90, 50, "30+0.05/-0.02"),
]
for x, y, text in texts:
    msp.add_text(text, dxfattribs={"height": 5, "insert": (x, y)})

# 添加标注实体
msp.add_linear_dim(base=(50, -15), p1=(0, 0), p2=(100, 0), angle=0)
msp.add_linear_dim(base=(-15, 30), p1=(0, 0), p2=(0, 60), angle=90)

doc.saveas("/opt/data/babo-local/uploads/test_drawing.dxf")
print("Test DXF created successfully")
