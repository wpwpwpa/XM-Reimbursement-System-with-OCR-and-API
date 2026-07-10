"""新功能无头验证：订单字段抽取 + 淘宝Excel按价匹配 + 汇总写出。"""
import sys, tempfile, os
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根，供 m1_poc 导入

from order_fields import extract_order_time, extract_product_name, normalize_date
from order_excel import (load_taobao_excel, match_by_price,
                         build_order_rows, write_order_excel)
from recognizer import recognize_one
from m1_poc.parser import parse

# ---------- 1. 时间归一化 ----------
assert normalize_date("2026-07-08 14:30:22") == "2026-07-08"
assert normalize_date("2026/07/09 12:00") == "2026-07-09"
assert normalize_date("2026年07月08日") == "2026-07-08"
print("[OK] 日期归一化")

# ---------- 2. 拼多多 / 美团 图片抽取 ----------
pd_text = "拼小圈\n下单时间 2026-07-08 14:30:22\n商品名称 云南冰糖苹果5斤装\n实付款 ¥26.52"
mt_text = "美团闪购\n期望时间 2026/07/09 12:00\n商品 美团买菜蔬菜套餐\n实付 ¥14.6"

class FakeBackend:
    def __init__(self, text): self._t = text
    def recognize(self, p): return self._t

r_pd = recognize_one(FakeBackend(pd_text), "x.png", 0.7, llm=None)
assert r_pd["platform"] == "拼多多", r_pd
assert r_pd["amount"] == 26.52, r_pd
assert r_pd["order_time"] == "2026-07-08", r_pd
assert "云南冰糖苹果" in (r_pd["product_name"] or ""), r_pd
print("[OK] 拼多多 时间/商品名:", r_pd["order_time"], r_pd["product_name"])

r_mt = recognize_one(FakeBackend(mt_text), "y.png", 0.7, llm=None)
assert r_mt["platform"] == "美团", r_mt
assert r_mt["order_time"] == "2026-07-09", r_mt
print("[OK] 美团 时间/商品名:", r_mt["order_time"], r_mt["product_name"])

# 淘宝：图片不抽时间/商品（留给 Excel）
tb_text = "天猫超市\n订单号:TB12345\n实付款 ¥99.0\n企业购"
r_tb = recognize_one(FakeBackend(tb_text), "z.png", 0.7, llm=None)
assert r_tb["platform"] == "淘宝", r_tb
assert r_tb["order_time"] is None and r_tb["product_name"] is None, r_tb
print("[OK] 淘宝 图片侧时间/商品名为空（待Excel补）")

# 2b. 商品名由文本大模型识别（OCR+文字模型）：LLM 覆盖正则基线
class FakeLLM:
    def parse(self, text):
        return {"status": "success", "amount": None, "platform": None,
                "confidence": 0.85, "used_llm": True,
                "product_name": "LLM抽到的商品名", "order_time": None}
r_pd_llm = recognize_one(FakeBackend(pd_text), "x.png", 0.7, llm=FakeLLM())
assert r_pd_llm["product_name"] == "LLM抽到的商品名", r_pd_llm
assert r_pd_llm["order_time"] == "2026-07-08", r_pd_llm  # 时间仍来自正则
print("[OK] 商品名=文本大模型识别；时间仍来自图片正则")

# ---------- 3. 淘宝 Excel 读取 + 按价匹配 ----------
from openpyxl import Workbook
tmp = Path(tempfile.gettempdir()) / "tb_sample.xlsx"
wb = Workbook(); ws = wb.active
ws.append(["订单编号", "实付金额", "下单时间", "商品名称"])
ws.append(["TB12345", 99.0, "2026-07-07", "海尔电饭煲4L"])
ws.append(["TB99999", 15.5, "2026-07-06", "抽纸24包"])
wb.save(tmp)

data = load_taobao_excel(str(tmp))
assert len(data["rows"]) == 2, data
# 用图片价格 99.0 匹配
row, ambiguous = match_by_price(data, 99.0)
assert row is not None and row["product"] == "海尔电饭煲4L", row
assert row["order_time"] == "2026-07-07", row
print("[OK] 淘宝按价匹配: 99.0 ->", row["product"], row["order_time"])

# 淘宝识别结果补 Excel 字段
r_tb["order_time"] = row["order_time"]
r_tb["product_name"] = row["product"]

# 2c. 淘宝：商品名优先图片LLM；worker 仅用 Excel 补时间 + 兜底商品名
r_tb_llm = recognize_one(FakeBackend(tb_text), "z.png", 0.7, llm=FakeLLM())
assert r_tb_llm["product_name"] == "LLM抽到的商品名", r_tb_llm   # 来自图片LLM
assert r_tb_llm["order_time"] is None, r_tb_llm                 # 淘宝不取图片时间
# 模拟 worker 淘宝块：时间来自Excel；商品名已有(LLM)则不覆盖
row2, _ = match_by_price(data, 99.0)
r_tb_llm["order_time"] = row2["order_time"]
if not r_tb_llm.get("product_name"):
    r_tb_llm["product_name"] = row2["product"]
assert r_tb_llm["order_time"] == "2026-07-07" and r_tb_llm["product_name"] == "LLM抽到的商品名"
print("[OK] 淘宝：时间=Excel，商品名=图片LLM(不被Excel覆盖)")

# ---------- 4. 汇总写出 ----------
results = [r_pd, r_mt, r_tb]
files = ["pd.png", "mt.png", "tb.png"]
rows = build_order_rows(results, files)
assert len(rows) == 3, rows
assert rows[0]["时间"] == "2026-07-08" and rows[0]["平台"] == "拼多多"
assert rows[2]["商品名称"] == "海尔电饭煲4L" and rows[2]["时间"] == "2026-07-07"

out = Path(tempfile.gettempdir()) / "订单汇总_test.xlsx"
write_order_excel(rows, str(out))
print("[OK] 汇总写出:", out, "行数:", len(rows))

# 读回校验
chk = load_taobao_excel(str(out))  # 复用读取器仅验证行数/首行
# 直接验证文件存在且非空前
assert out.exists() and out.stat().st_size > 0
print("\n=== 新功能无头验证全部通过 ===")
tmp.unlink(missing_ok=True)
out.unlink(missing_ok=True)
