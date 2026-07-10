"""生成 3 张合成订单截图，供 M1 PoC 验证（无真实截图时的自包含输入）。
真实截图可放到 test_images/ 直接覆盖，demo 自动读取。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "test_images"
OUT.mkdir(exist_ok=True)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",   # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf", # 黑体
]
FONT = None
for fp in FONT_CANDIDATES:
    if Path(fp).exists():
        FONT = fp
        break
print("字体:", FONT or "默认")


def draw(path: str, lines: list[str], size=28):
    img = Image.new("RGB", (460, 320), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, size) if FONT else ImageFont.load_default()
    y = 30
    for ln in lines:
        d.text((24, y), ln, fill="black", font=font)
        y += 46
    img.save(path, "PNG")
    print("生成:", path)


# 淘宝：天猫(特征词) + 实付款 ¥26.52（同行）
draw(str(OUT / "taobao.png"), [
    "天猫超市",
    "订单号: 123456789",
    "实付款 ¥26.52",
    "企业购 专享价",
])

# 美团：闪购(特征词) + 实付 / ¥14.6（跨行，验证跨行抽取）
draw(str(OUT / "meituan.png"), [
    "美团闪购",
    "订单详情",
    "实付",
    "¥14.6",
    "闪购 专送",
])

# 拼多多：拼小圈(特征词) + 合计 / ¥14.1（跨行）
draw(str(OUT / "pinduoduo.png"), [
    "拼小圈",
    "已拼单 2件",
    "合计",
    "¥14.1",
])
