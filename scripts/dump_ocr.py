#!/usr/bin/env python3
"""Dump RapidOCR 原始文本（正则实际吃的输入），用于核对正则是否匹配当前 OCR 输出。

用法:
  python scripts/dump_ocr.py <images_dir>
  python scripts/dump_ocr.py <images_dir> --limit 10
"""
import sys, json, re, argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

PLATFORMS = ["美团", "淘宝", "拼多多", "京东"]
_GT_RE = re.compile(r"([\d]+(?:\.[\d]+)?)\s*(" + "|".join(PLATFORMS) + r")")


def parse_gt(name):
    m = _GT_RE.search  # placeholder
    return None


def parse_gt_real(name):
    m = re.search(r"([\d]+(?:\.[\d]+)?)\s*(" + "|".join(PLATFORMS) + r")", name)
    return {"amount": float(m.group(1)), "platform": m.group(2)} if m else None


def load_gt_csv(csv_path):
    import csv
    m = {}
    if csv_path and Path(csv_path).exists():
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                old = row.get("old_path") or ""
                new = row.get("new_path") or ""
                g = parse_gt_real(Path(old).name)
                if g:
                    m[Path(new).name] = g
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from m1_poc.ocr_backend import OcrBackend
    backend = OcrBackend(use_gpu=False)

    imgs = sorted(
        p for p in Path(args.images).rglob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp")
    )
    if args.limit:
        imgs = imgs[: args.limit]
    gt_map = load_gt_csv(str(Path(args.images) / "_rename_mapping.csv"))

    rows = []
    for img in imgs:
        raw = backend.recognize(str(img))
        gt = parse_gt_real(img.name) or gt_map.get(img.name)
        rows.append({"image": img.name, "gt": gt, "raw_ocr_text": raw})

    # stdout 完整打印焦点图：京东误判图 + 各平台代表
    focus_names = {"Ct7XwxND99ek.jpg", "13.72美团.jpg", "25.8淘宝.jpg", "22.0拼多多.jpg"}
    focus = [r for r in rows if r["image"] in focus_names]
    for r in focus:
        print("=" * 64)
        print("IMAGE:", r["image"], "  GT:", r["gt"])
        print("--- RAW OCR TEXT (RapidOCR, 正则实际输入) ---")
        print(r["raw_ocr_text"])
        print()

    out = ROOT / "logs" / "verify"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out / f"ocr_dump_{stamp}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"全部 {len(rows)} 张 RapidOCR 原文已存: {path}")


if __name__ == "__main__":
    main()
