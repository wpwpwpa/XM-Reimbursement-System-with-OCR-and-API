"""M3 识别核心：复用 m1_poc 模式 C 管线（RapidOCR + 正则解析）。

纯后端模块，不依赖 PyQt，供 GUI 后台线程与无头验证共同调用。

识别链路：
  RapidOCR 取文本 → 正则 parse（平台 + 金额 + 时间）
  → 拼多多/美团（配模型）直接调模型判商品名（方案A：不对比不二次仲裁）
  → 金额/平台/时间以正则为主，模型补缺空缺字段
"""
import sys
from pathlib import Path

# 开发模式：把项目根（含 m1_poc 包）加入 path。
# PyInstaller frozen 时 m1_poc 已打进包，跳过 path 注入以免干扰。
if not getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_THRESHOLD = 0.70

from order_fields import (extract_order_time, extract_product_name,
                          normalize_date, _is_bad_product)


def render_template(tpl: str, amount, platform, ext: str) -> str:
    """按 PRD 6.4 模板生成新文件名。缺金额或平台返回空串。"""
    if amount is None or platform is None:
        return ""
    return (
        tpl.replace("{price}", str(amount))
        .replace("{platform}", platform)
        .replace("{ext}", ext)
    )


def _normalize_llm(raw: dict) -> dict:
    """把 LLM 后端返回的字典规范化成与 parse() 同构。"""
    return {
        "status": raw.get("status", "failed"),
        "amount": raw.get("amount"),
        "platform": raw.get("platform"),
        "confidence": raw.get("confidence", 0.0),
        "reason": raw.get("reason", ""),
        "used_llm": True,
    }


def recognize_one(backend, image_path, threshold: float = DEFAULT_THRESHOLD,
                  llm=None) -> dict:
    """单图识别：RapidOCR → 正则 parse（金额+平台）；商品名由大模型识别。

    设计（方案A：简化门控，直接让模型判商品名，不对比不二次仲裁）：
    - 金额 / 平台 / 时间：正则抽取为主。
    - 商品名称：直接由大模型识别（文本模型→llm.parse）。仅一次 LLM 调用取其
      product_name。模型结果经 _is_bad_product 质检：有效则用模型值；模型返回
      垃圾/空则回退正则抽到的商品名（免费，不再二次调模型）。
    - 淘宝商品名留空待 Excel 按价格匹配补全。
    backend 由调用方创建并复用（RapidOCR 模型加载较重）。
    llm: 可选的 LLMBackend 实例（None = 纯正则，不调任何模型 → 商品名留空）。
    """
    from m1_poc.parser import parse

    text = backend.recognize(image_path)
    result = parse(text, threshold)        # 正则：金额 + 平台
    platform = result.get("platform")

    # 图片侧时间：拼多多/美团 取关键词；淘宝留空待 Excel
    time_img = extract_order_time(text, platform) if platform in ("拼多多", "美团") else None
    # 商品名基线：正则抽取（仅作兜底，模型档下优先用模型结果）
    product = extract_product_name(text, platform) if platform in ("拼多多", "美团") else None
    if product and _is_bad_product(product):   # 质检：垃圾值视为空
        product = None

    # 大模型判断商品名（方案A：一次调用，直接采用，不对比不仲裁）
    llm_raw = None
    if llm is not None:
        try:
            llm_raw = llm.parse(text)
        except Exception:  # noqa: BLE001
            llm_raw = None

    if llm_raw is not None:
        lp = llm_raw.get("product_name") or None
        if lp and not _is_bad_product(lp):
            product = lp                      # 模型有效 → 直接采用
        # 模型坏/空 → 保留正则兜底 product（不再二次调模型）
        lt = normalize_date(llm_raw.get("order_time"))
        if time_img is None and lt:
            time_img = lt                    # 正则漏抽时间时由 LLM 补

    result["order_time"] = time_img
    result["product_name"] = product
    result["ocr_text"] = text                 # 透传 OCR 原文

    if result["status"] == "success":
        return result

    # 正则失败：若大模型给出金额/平台则兜底（安全网）
    if llm_raw is not None:
        norm = _normalize_llm(llm_raw)
        if norm["status"] == "success":
            norm["reason"] = f"正则未匹配({result.get('reason', '')})，已由 LLM 兜底"
            norm["order_time"] = time_img
            norm["product_name"] = product
            norm["ocr_text"] = text
            return norm
    return result


def _need_model(result: dict) -> bool:
    """门控判定：正则前置结果是否还需要调模型兜底。

    触发条件（任一）：
    - status != success（金额/平台缺失，正则拿不全关键字段）
    - 商品名为空 且 平台非淘宝（淘宝商品名留给 Phase B 的 Excel 匹配，更准）
    """
    if result.get("status") != "success":
        return True
    if not result.get("product_name") and result.get("platform") != "淘宝":
        return True
    return False


def recognize_image_gated(backend, image_path, model_fn=None,
                          threshold: float = DEFAULT_THRESHOLD) -> dict:
    """门控识别（方案A：简化）：OCR+正则前置拿金额/平台/时间；拼多多/美团配了模型
    则直接调模型判断商品名，不再先抽正则候选对比、不再二次仲裁。

    backend: OcrBackend 实例（前置必需；None 时文本为空，正则必败→走模型）。
    model_fn: 兜底引擎，签名 model_fn(image_path, ocr_text) -> dict。
              None = 纯前端不调模型，商品名用正则兜底。
    threshold: 置信度阈值。

    策略：
    - 金额/平台：正则为主（准）。
    - 时间：拼多多/美团 正则抽（下单时间/期望时间）。
    - 商品名：纯正则档用正则；模型档直接采用模型结果（模型坏/空则回退正则兜底，
      不再二次调模型）。
    兜底只「补缺」金额/平台/时间；商品名由模型直接给。
    """
    from m1_poc.parser import parse, synthesize_confidence

    text = ""
    if backend is not None:
        try:
            text = backend.recognize(image_path)
        except Exception:  # noqa: BLE001
            text = ""

    result = parse(text, threshold)          # 正则：金额 + 平台
    result["ocr_text"] = text                 # 透传 OCR 原文，供日志/后台验证
    platform = result.get("platform")
    # 拼多多/美团 可从文本正则抽时间；淘宝留空待 Excel
    time_img = extract_order_time(text, platform) if platform in ("拼多多", "美团") else None
    # 商品名基线：正则抽取（仅作兜底，模型档下优先模型）
    product = extract_product_name(text, platform) if platform in ("拼多多", "美团") else None
    if product and _is_bad_product(product):   # 质检：垃圾值视为空
        product = None
    result["order_time"] = time_img
    result["product_name"] = product

    # 门控：拼多多/美团 + 配模型 → 始终跑（直接让模型判商品名）；
    #       其余（非拼多多/美团 或 纯正则档）走原 _need_model。
    always_run = platform in ("拼多多", "美团") and model_fn is not None
    if model_fn is None or (not always_run and not _need_model(result)):
        return result

    # 直接调模型判断（方案A：一次调用，采用模型商品名，不对比不仲裁）
    try:
        mres = model_fn(image_path, text)
    except Exception as e:  # noqa: BLE001
        result["reason"] = (result.get("reason") or "") + f"（模型兜底异常:{e}）"
        return result

    if mres:
        B = mres.get("product_name") or None
        # 直接采用模型商品名；模型坏/空则回退正则兜底（不二次调模型）
        if B and not _is_bad_product(B):
            result["product_name"] = B
        # 金额/平台/时间补缺（正则为主，模型补空缺）
        if result.get("amount") is None and mres.get("amount") is not None:
            result["amount"] = mres["amount"]
        if not result.get("platform") and mres.get("platform"):
            result["platform"] = mres["platform"]
        if not result.get("order_time"):
            lt = normalize_date(mres.get("order_time"))
            if lt:
                result["order_time"] = lt
        # 补缺后重算置信度/状态
        result["confidence"] = synthesize_confidence(
            result.get("amount"), result.get("platform"),
        )
        result["status"] = (
            "success" if result["confidence"] >= threshold else "failed"
        )
        result["used_model"] = True
        result["reason"] = (result.get("reason") or "") + "（模型兜底补缺）"
    return result


def recognize_vision(image_path, vision_backend) -> dict:
    """视觉模式单图识别：截图直送多模态大模型解析金额+平台。

    vision_backend: VisionBackend 实例（来自 llm_backend）。
    异常兜底为失败，绝不让调用方崩溃。
    """
    try:
        res = vision_backend.parse_image(image_path)
        res["ocr_text"] = ""  # 视觉模式无 OCR 文本
        return res
    except Exception as e:  # noqa: BLE001
        return {
            "status": "failed", "amount": None, "platform": None,
            "confidence": 0.0, "reason": f"视觉识别异常: {e}",
            "ocr_text": "",
        }
