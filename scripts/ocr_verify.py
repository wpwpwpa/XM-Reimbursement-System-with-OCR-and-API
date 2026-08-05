#!/usr/bin/env python3
"""OCR 改造离线验证脚本（阶段1，增强版：含文件名 GT 自动评判）。

对比「现有 RapidOCR + 正则 (recognizer.recognize_image_gated)」与
「PaddleOCR PP-StructureV3 结构化」对真实截图的字段提取命中率。

GT 来源：图片文件名本身已带金额+平台（如 `13.72美团.jpg`），直接解析做 ground truth。

PP 侧：单次 subprocess 批量推理（模型只加载一次），输出 {filename: text}，
      主进程再用现有 order_fields 提取同字段，公平对比（同正则、不同文本来源）。

用法:
  python scripts/ocr_verify.py --images DIR            # 两侧都跑
  python scripts/ocr_verify.py --images DIR --no-paddle  # 仅现有侧
  python scripts/ocr_verify.py --images DIR --limit 10   # 只处理前10张(控时)
  python scripts/ocr_verify.py --images DIR --paddle-python X
"""
import sys, json, argparse, subprocess, re, csv
from pathlib import Path
from datetime import datetime

GT_CSV_NAME = "_rename_mapping.csv"


def load_gt_csv(csv_path: str) -> dict:
    """从重命名映射 csv 建 {乱码名: {amount,platform}}。
    old_path=规范名(含金额平台), new_path=实际乱码名。"""
    m = {}
    p = Path(csv_path)
    if not p.exists():
        return m
    with open(p, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            old = row.get("old_path") or ""
            new = row.get("new_path") or ""
            on, nn = Path(old).name, Path(new).name
            g = parse_gt(on)
            if g and nn:
                m[nn] = g
    return m


def get_gt(img_name: str, gt_csv_map: dict):
    g = parse_gt(img_name)
    if g:
        return g
    return gt_csv_map.get(img_name)

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
for p in (str(ROOT), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

CURRENT_FIELDS = ["amount", "platform", "order_time", "product_name"]
DEFAULT_PADDLE_PYTHON = str(ROOT / ".paddle_venv" / "Scripts" / "python.exe")
PADDLE_PYTHON = DEFAULT_PADDLE_PYTHON

PLATFORMS = ["美团", "淘宝", "拼多多", "京东"]
_GT_RE = re.compile(r"([\d]+(?:\.[\d]+)?)\s*(" + "|".join(PLATFORMS) + r")")


def parse_gt(name: str):
    """从文件名解析 ground truth：金额(数值)+平台。"""
    m = _GT_RE.search(name)
    if not m:
        return None
    return {"amount": float(m.group(1)), "platform": m.group(2)}


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def hit(cur_val, gt_val):
    """cur_val vs GT 是否命中。gt_val 为 None 时返回 None(该图无 GT，跳过统计)。"""
    if gt_val is None:
        return None
    if cur_val is None:
        return False
    if isinstance(gt_val, (int, float)):
        cn = _num(cur_val)
        return cn is not None and abs(cn - gt_val) < 1e-6
    return str(cur_val).strip() == str(gt_val).strip()


# ---------------- 现有侧 ----------------
def run_current(backend, img: Path) -> dict:
    from recognizer import recognize_image_gated
    res = recognize_image_gated(backend, str(img), model_fn=None)
    return {k: res.get(k) for k in CURRENT_FIELDS} | {"status": res.get("status")}


# ---------------- PP 侧（单次批量 subprocess 调独立 paddle venv）----------------
_PP_CODE = r'''
import os
# 绕开 Paddle 3.3 oneDNN/PIR 未实现 op 报错（已降级 3.2，仍保留防御）
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
        lines = []
        for o in pipe.predict(input=path):
            f = getattr(o, "json", None)
            if callable(f):
                js = f()
                def walk(x):
                    if isinstance(x, dict):
                        t = x.get("text")
                        if isinstance(t, str):
                            lines.append(t)
                        for v in x.values():
                            walk(v)
                    elif isinstance(x, list):
                        for v in x:
                            walk(v)
                if isinstance(js, dict):
                    walk(js)
            else:
                lines.append(str(o))
        out[Path(path).name] = "\n".join(lines)
    except Exception as e:
        out[Path(path).name] = "__ERR__" + repr(e)
print(json.dumps(out, ensure_ascii=False))
'''


def run_paddle_batch(imgs) -> (dict, str):
    """单次 subprocess 批量推理，返回 ({filename: text}, error_or_None)。"""
    if not Path(PADDLE_PYTHON).exists():
        return None, f"paddle python 未找到: {PADDLE_PYTHON}"
    try:
        r = subprocess.run(
            [PADDLE_PYTHON, "-c", _PP_CODE, *[str(i) for i in imgs]],
            capture_output=True, encoding="utf-8", errors="replace", timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return None, "PP 批量推理超时(>1800s，可能样本过多/机器慢)"
    if r.returncode != 0:
        return None, f"PP 推理失败: {r.stderr[-1500:]}"
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, f"解析 PP 输出失败: {e}"


def extract_fields_from_text(full: str, is_paddle_text: bool) -> dict:
    """用现有 order_fields + m1_poc.parser 从文本提 amount/platform。"""
    from order_fields import extract_order_time, extract_product_name
    from m1_poc.parser import parse
    pr = parse(full, 0.70)
    platform = pr.get("platform")
    return {
        "amount": pr.get("amount"),
        "platform": platform,
        "order_time": (extract_order_time(full, platform)
                       if platform in ("拼多多", "美团", "京东") else None),
        "product_name": extract_product_name(full, platform),
    }


# ---------------- 比对 + 汇总 ----------------
def main():
    global PADDLE_PYTHON
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=str(ROOT / "m1_poc" / "test_images"))
    ap.add_argument("--no-paddle", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张(控时)")
    ap.add_argument("--gt-csv", default="", help="GT 映射 csv(默认在 --images 目录下找 _rename_mapping.csv)")
    ap.add_argument("--paddle-python", default=DEFAULT_PADDLE_PYTHON)
    args = ap.parse_args()
    PADDLE_PYTHON = args.paddle_python

    imgs = sorted(
        p for p in Path(args.images).rglob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp")
    )
    if args.limit:
        imgs = imgs[: args.limit]
    if not imgs:
        print(f"未在 {args.images} 找到图片")
        return

    # GT：优先文件名，否则查重命名映射 csv
    csv_path = args.gt_csv or str(Path(args.images) / GT_CSV_NAME)
    gt_map = load_gt_csv(csv_path)
    print(f"[GT] 从 {csv_path} 载入 {len(gt_map)} 条乱码名映射")

    from m1_poc.ocr_backend import OcrBackend
    backend = OcrBackend(use_gpu=False)

    # PP 批量（一次加载模型）
    pp_map = None
    pp_err = None
    if not args.no_paddle:
        print(f"[PP] 批量推理 {len(imgs)} 张(模型仅加载一次)...")
        pp_map, pp_err = run_paddle_batch(imgs)
        if pp_err:
            print(f"[PP] 不可用: {pp_err}")

    rows = []
    for img in imgs:
        gt = get_gt(img.name, gt_map)
        cur = run_current(backend, img)
        cur_am_hit = hit(cur.get("amount"), gt.get("amount") if gt else None)
        cur_pl_hit = hit(cur.get("platform"), gt.get("platform") if gt else None)

        pad = {"available": False, "error": "skipped"}
        pad_am_hit = None
        pad_pl_hit = None
        if pp_map is not None:
            full = pp_map.get(img.name)
            if full and not full.startswith("__ERR__"):
                try:
                    pf = extract_fields_from_text(full, True)
                    pad = {"available": True, **pf, "pp_text": full[:300]}
                    pad_am_hit = hit(pf.get("amount"), gt.get("amount") if gt else None)
                    pad_pl_hit = hit(pf.get("platform"), gt.get("platform") if gt else None)
                except Exception as e:
                    pad = {"available": True, "error": f"字段提取失败: {e}"}
            elif full:
                pad = {"available": True, "error": full[:200]}

        rows.append({
            "image": img.name, "gt": gt,
            "current": cur, "cur_amount_hit": cur_am_hit, "cur_platform_hit": cur_pl_hit,
            "paddle": {k: v for k, v in pad.items() if k != "pp_text"},
            "pad_amount_hit": pad_am_hit, "pad_platform_hit": pad_pl_hit,
        })
        print(f"  {img.name}: GT={gt} | 现 amount={cur.get('amount')}({cur_am_hit}) "
              f"platform={cur.get('platform')}({cur_pl_hit})"
              + (f" | PP amount={pad.get('amount')}({pad_am_hit}) platform={pad.get('platform')}({pad_pl_hit})"
                 if pad.get('available') else f" | PP={pad.get('error')}"))

    # 汇总命中率（仅统计有 GT 的图）
    gt_rows = [r for r in rows if r["gt"]]
    n = len(gt_rows)
    print(f"\n=== 汇总 (有 GT 的图片共 {n}/{len(rows)} 张) ===")
    if n:
        def rate(key):
            vals = [r[key] for r in gt_rows if r[key] is not None]
            ok = sum(1 for v in vals if v)
            return ok, len(vals)
        c_am = rate("cur_amount_hit"); c_pl = rate("cur_platform_hit")
        print(f"  现有侧 amount 命中: {c_am[0]}/{c_am[1]}  | platform 命中: {c_pl[0]}/{c_pl[1]}")
        if not args.no_paddle:
            p_am = rate("pad_amount_hit"); p_pl = rate("pad_platform_hit")
            print(f"  PP侧   amount 命中: {p_am[0]}/{p_am[1]}  | platform 命中: {p_pl[0]}/{p_pl[1]}")
    else:
        print("  (无 GT 图，仅输出明细)")

    out_dir = ROOT / "logs" / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"ocr_verify_{stamp}.json"
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  明细已存: {out_path}")


if __name__ == "__main__":
    main()
