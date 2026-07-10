"""M1 PoC 主入口：输入示例图 → 输出统一 JSON。

用法：
    python poc.py --input m1_poc/test_images --output m1_poc/output/result.json

只实现模式 C（RapidOCR + 正则解析），满足 M1 交付物：
「OCR 识别 Demo：输入 3 张示例图 → 输出正确 JSON」。
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from ocr_backend import OcrBackend
from parser import parse

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def run(input_dir: str, output_path: str, threshold: float = 0.70) -> list:
    input_dir = Path(input_dir)
    images = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        raise SystemExit(f"未找到图片：{input_dir}（支持 {sorted(IMAGE_EXTS)}）")

    backend = OcrBackend()
    results = []
    for idx, img in enumerate(images, 1):
        print(f"[{idx}/{len(images)}] 识别 {img.name} ...")
        text = backend.recognize(img)
        parsed = parse(text, threshold)
        results.append({
            "original_name": img.name,
            "amount": parsed["amount"],
            "platform": parsed["platform"],
            "confidence": parsed["confidence"],
            "status": parsed["status"],
            # 仅调试用：OCR 原始文本
            "_raw_text": text,
        })

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    ok = sum(1 for r in results if r["status"] == "success")
    print(f"\n完成：{len(results)} 张，成功 {ok}，失败 {len(results) - ok}")
    print(f"JSON 已写入：{output_path}")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M1 OCR PoC (模式C)")
    ap.add_argument("--input", default="m1_poc/test_images")
    ap.add_argument("--output", default="m1_poc/output/result.json")
    ap.add_argument("--threshold", type=float, default=0.70)
    args = ap.parse_args()
    run(args.input, args.output, args.threshold)
