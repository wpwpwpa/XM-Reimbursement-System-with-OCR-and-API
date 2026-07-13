#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对淘宝目录：文件名价格 vs OCR 提取的真实金额。只读，不改任何文件。"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(r"C:\Users\10516\WorkBuddy\XM报销OCR识别搭配大模型")
sys.path.insert(0, str(PROJECT_ROOT))

from rapidocr_onnxruntime import RapidOCR
from m1_poc.parser import extract_amount

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


def name_price(fn: str):
    """淘宝价格紧跟「淘宝」前或后（且非日期）。"""
    m = re.search(r"淘宝\s*(\d+(?:\.\d+)?)", fn)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*淘宝", fn)
    if m:
        return float(m.group(1))
    return None


def all_amounts(text: str):
    """提取所有 ¥金额 与 关键词后金额，作为人工研判参考。"""
    out = []
    for m in re.finditer(r"¥\s*(\d+(?:\.\d+)?)", text):
        out.append(("¥", float(m.group(1))))
    for kw in ("实付款", "实付", "总价", "合计"):
        m = re.search(kw + r"\s*[:：]?\s*¥?\s*(\d+(?:\.\d+)?)", text)
        if m:
            out.append((kw, float(m.group(1))))
    return out


def main():
    folder = Path(r"C:\Users\10516\Desktop\完整的报销\报销截图和excel\淘宝")
    engine = RapidOCR()
    print(f"目录: {folder}\n")
    print(f"{'文件名':<28} {'命名价':>8} {'OCR价':>9} {'结果':>6}  候选金额")
    print("-" * 90)
    mismatch = []
    for fn in sorted(
        [f for f in folder.iterdir() if f.suffix.lower() in IMG_EXTS]
    ):
        np_ = name_price(fn.name)
        try:
            result, _ = engine(str(fn))
        except Exception as e:  # noqa
            print(f"{fn.name:<28} {'?':>8} {'ERR':>9}  OCR异常:{e}")
            continue
        text = "\n".join([ln[1] for ln in result]) if result else ""
        oa = extract_amount(text)
        amts = all_amounts(text)
        ok = np_ is not None and oa is not None and abs(np_ - oa) < 0.01
        flag = "OK" if ok else "不一致"
        cands = " ".join(f"{k}={v}" for k, v in amts[:8])
        print(f"{fn.name:<28} {str(np_):>8} {str(oa):>9} {flag:>6}  {cands}")
        if not ok:
            mismatch.append((fn.name, np_, oa, cands))
    print("\n==== 不一致清单 ====")
    for nm, np_, oa, c in mismatch:
        print(f"{nm}: 命名价={np_} OCR价={oa} | 候选:{c}")


if __name__ == "__main__":
    main()
