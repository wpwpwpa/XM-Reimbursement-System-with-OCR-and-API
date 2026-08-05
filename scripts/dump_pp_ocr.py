#!/usr/bin/env python3
"""Dump PaddleOCR PP-StructureV3 的原始文本（与 dump_ocr.py 同结构，便于对比）。

PP 侧通过 subprocess 调独立 paddle venv 跑 PPStructureV3，walk 出所有 text 字段，
同时保留版面结构(pp_raw_json，含 layout/坐标)，让用户看清「结构化 OCR」原文长啥样。

用法:
  # 焦点图(前台快跑，约 N 分钟)：指定若干文件名
  python scripts/dump_pp_ocr.py DIR --include Ct7XwxND99ek.jpg,13.72美团.jpg
  # 全量(后台慢跑，41 张约 30-40 分钟)
  python scripts/dump_pp_ocr.py "C:/Users/10516/Desktop/完整的报销/报销截图和excel"
"""
import sys, json, re, csv, subprocess
from pathlib import Path
from datetime import datetime

GT_CSV_NAME = "_rename_mapping.csv"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PADDLE_PYTHON = str(ROOT / ".paddle_venv" / "Scripts" / "python.exe")
PADDLE_PYTHON = DEFAULT_PADDLE_PYTHON

PLATFORMS = ["美团", "淘宝", "拼多多", "京东"]
_GT_RE = re.compile(r"([\d]+(?:\.[\d]+)?)\s*(" + "|".join(PLATFORMS) + r")")


def parse_gt(name: str):
    m = _GT_RE.search(name)
    return {"amount": float(m.group(1)), "platform": m.group(2)} if m else None


def load_gt_csv(csv_path: str) -> dict:
    m = {}
    p = Path(csv_path)
    if not p.exists():
        return m
    with open(p, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            old, new = row.get("old_path") or "", row.get("new_path") or ""
            on, nn = Path(old).name, Path(new).name
            g = parse_gt(on)
            if g and nn:
                m[nn] = g
    return m


def get_gt(name: str, gt_map: dict):
    return parse_gt(name) or gt_map.get(name)


# 与 ocr_verify.py 同款 PP 抽取逻辑（walk 出所有 text）
_PP_CODE = r'''
import os
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_INFERENCE_USE_MKLDNN"] = "0"
os.environ["FLAGS_enable_pir_in_static_run"] = "0"
os.environ["PADDLE_PIR_EXECUTOR"] = "0"
import sys, json
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
import paddle
try:
    paddle.set_flags({"FLAGS_use_mkldnn": False, "FLAGS_enable_pir_in_static_run": False})
except Exception:
    pass
from paddleocr import PPStructureV3
pipe = PPStructureV3()
out = {}
for path in sys.argv[1:]:
    try:
        lines, blocks = [], []
        for o in pipe.predict(input=path):
            # 全局 OCR 纯文本行（等同 RapidOCR「按行拼文本」）
            ocr = o.get("overall_ocr_res")
            if isinstance(ocr, dict):
                for t in (ocr.get("rec_texts") or []):
                    lines.append(str(t))
            # 结构化版面区块（文本块/标题/表格…），带 block 类型+坐标（PP 差异化价值）
            for b in (o.get("parsing_res_list") or []):
                bt = getattr(b, "content", None)
                lbl = getattr(b, "label", None)
                if bt and str(bt) != "None":
                    blocks.append({"label": str(lbl) if lbl else "?",
                                   "content": str(bt),
                                   "bbox": getattr(b, "bbox", None)})
        out[Path(path).name] = {
            "text": "\n".join(lines),
            "blocks": blocks,
        }
    except Exception as e:
        out[Path(path).name] = {"text": "__ERR__" + repr(e), "blocks": []}
print(json.dumps(out, ensure_ascii=False))
'''


def run_paddle_batch(imgs) -> (dict, str):
    if not Path(PADDLE_PYTHON).exists():
        return None, f"paddle python 未找到: {PADDLE_PYTHON}"
    try:
        r = subprocess.run(
            [PADDLE_PYTHON, "-c", _PP_CODE, *[str(i) for i in imgs]],
            capture_output=True, encoding="utf-8", errors="replace", timeout=3600,
        )
    except subprocess.TimeoutExpired:
        return None, "PP 批量推理超时(>3600s)"
    if r.returncode != 0:
        return None, f"PP 推理失败: {r.stderr[-1500:]}"
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, f"解析 PP 输出失败: {e}"


def main():
    global PADDLE_PYTHON
    images_dir = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "m1_poc" / "test_images")
    includes = []
    for a in sys.argv[2:]:
        if a.startswith("--include="):
            includes = [x.strip() for x in a[len("--include="):].split(",") if x.strip()]
        elif a.startswith("--paddle-python="):
            PADDLE_PYTHON = a[len("--paddle-python="):]

    imgs = sorted(
        p for p in Path(images_dir).rglob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp")
    )
    if includes:
        want = set(includes)
        imgs = [p for p in imgs if p.name in want]
        if not imgs:
            print(f"未找到指定的焦点图: {includes}")
            return

    gt_map = load_gt_csv(str(Path(images_dir) / GT_CSV_NAME))
    print(f"[PP] 推理 {len(imgs)} 张 (模型仅加载一次)...")
    pp_map, err = run_paddle_batch(imgs)
    if err:
        print(f"[PP] 失败: {err}")
        return

    rows = []
    for img in imgs:
        d = pp_map.get(img.name, {})
        text = d.get("text", "")
        blocks = d.get("blocks", [])
        rows.append({
            "image": img.name,
            "gt": get_gt(img.name, gt_map),
            "raw_ocr_text": text,           # 与 dump_ocr.py 同字段，便于并排对比
            "layout_blocks": blocks,        # PP 特有的结构化区块（含 block 类型）
        })
        print(f"  {img.name} (GT={get_gt(img.name, gt_map)}, 区块数={len(blocks)}):")
        snippet = text[:200].replace("\n", " ⏎ ")
        print(f"    {snippet}")

    out_dir = ROOT / "logs" / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"pp_ocr_dump_{stamp}.json"
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  明细已存: {out_path}")


if __name__ == "__main__":
    main()
