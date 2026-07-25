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


def recognize_one(backend, image_path, threshold: float = DEFAULT_THRESHOLD,
                  llm=None) -> dict:
    """单图识别（薄封装 recognize_image_gated）。

    金额/平台/时间正则为主，商品名由大模型兜底补缺——逻辑统一收敛到
    recognize_image_gated，避免两份门控分叉。

    backend: OcrBackend 实例（RapidOCR 模型加载较重，调用方创建复用）。
    llm: 可选的 LLMBackend 实例（None = 纯正则，不调任何模型 → 商品名留空）。
        提供时转为 model_fn 委托给 gated；其返回形如 {amount, platform,
        product_name, order_time}，与 gated 的 model_fn 契约一致。
    """
    model_fn = None
    if llm is not None:
        model_fn = lambda path, text, regex: llm.parse(text, regex_result=regex)
    return recognize_image_gated(backend, image_path, model_fn=model_fn,
                                 threshold=threshold)


def _require_product_for_images(result: dict) -> None:
    """图片四要素（金额/平台/日期/商品名）齐全才算成功。

    发票（kind=invoice）商品名是税目分类、非真实商品名，不适用此规则。
    仅对当前 success 的图片降级：缺任一要素 → failed + 原因标注。
    """
    if result.get("kind") == "invoice":
        return
    if result.get("status") != "success":
        return
    missing = []
    if result.get("amount") is None:
        missing.append("金额")
    if not result.get("platform"):
        missing.append("平台")
    if not result.get("order_time"):
        missing.append("日期")
    if not result.get("product_name"):
        missing.append("商品名")
    if missing:
        result["status"] = "failed"
        result["reason"] = (result.get("reason") or "") + "（缺少" + "、".join(missing) + "）"


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
                          threshold: float = DEFAULT_THRESHOLD,
                          force_model: bool = False,
                          text: str | None = None) -> dict:
    """门控识别（方案A：简化）：OCR+正则前置拿金额/平台/时间；拼多多/美团配了模型
    则直接调模型判断商品名，不再先抽正则候选对比、不再二次仲裁。

    backend: OcrBackend 实例（前置必需；None 时文本为空，正则必败→走模型）。
    model_fn: 兜底引擎，签名 model_fn(image_path, ocr_text) -> dict。
              None = 纯前端不调模型，商品名用正则兜底。
    threshold: 置信度阈值。
    force_model: 用户「调试强制」开关。True=每张图都调模型（核对/调试用）；
                 False=智能门控，仅正则未拿全时调模型（默认）。

    策略：
    - 金额/平台：正则为主（准）。
    - 时间：拼多多/美团 正则抽（下单时间/期望时间）。
    - 商品名：纯正则档用正则；模型档直接采用模型结果（模型坏/空则回退正则兜底，
      不再二次调模型）。
    兜底只「补缺」金额/平台/时间；商品名由模型直接给。
    """
    from m1_poc.parser import parse, synthesize_confidence

    if text is None:
        text = ""
        if backend is not None:
            try:
                text = backend.recognize(image_path)
            except Exception:  # noqa: BLE001
                text = ""

    result = parse(text, threshold)          # 正则：金额 + 平台
    result["ocr_text"] = text                 # 透传 OCR 原文，供日志/后台验证
    platform = result.get("platform")
    # 拼多多/美团/京东 可从文本正则抽时间；淘宝留空待 Excel
    time_img = extract_order_time(text, platform) if platform in ("拼多多", "美团", "京东") else None
    # 商品名：正则抽取（所有平台均尝试；模型档下优先模型）
    product = extract_product_name(text, platform)
    if product and _is_bad_product(product):   # 质检：垃圾值视为空
        product = None
    result["order_time"] = time_img
    result["product_name"] = product
    # 快照纯正则输出（调模型前），供日志对比「正则抽到啥 vs 模型补啥」
    result["regex_result"] = {
        "amount": result.get("amount"),
        "platform": result.get("platform"),
        "order_time": result.get("order_time"),
        "product_name": result.get("product_name"),
        "confidence": result.get("confidence"),
    }

    # 门控（由用户「调试强制」开关控制，force_model 来自 UI）：
    #   force_model=True  → 每张图都调模型（以模型为准，用于核对/调试）；
    #   force_model=False → 智能门控：仅当正则未拿全（缺商品名/状态非 success）才调模型。
    #   注：拼多多/美团 正则通常拿不到商品名，智能门控下仍会自然触发模型，
    #       无需单独 always_run 分支。
    if model_fn is None or (not force_model and not _need_model(result)):
        _require_product_for_images(result)
        return result

    # 直接调模型判断（方案A：一次调用，采用模型商品名，不对比不仲裁）
    try:
        mres = model_fn(image_path, text, result["regex_result"])
    except Exception as e:  # noqa: BLE001
        result["reason"] = (result.get("reason") or "") + f"（模型兜底异常:{e}）"
        return result

    if mres:
        # 模型已是裁判：以模型结果为准，正则仅当模型字段空/坏时兜底（见下方后处理）
        if mres.get("amount") is not None:
            result["amount"] = mres["amount"]
        if mres.get("platform"):
            result["platform"] = mres["platform"]
        if mres.get("order_time"):
            result["order_time"] = normalize_date(mres.get("order_time"))
        B = mres.get("product_name") or None
        if B and not _is_bad_product(B):
            result["product_name"] = B

    # 后处理：模型未给 platform 或 product_name 时，用 OCR 文本正则再兜底一次。
    # 这样本地小模型即使只给出 amount，也能配合正则完成识别。
    platform = result.get("platform")
    if not platform:
        from m1_poc.parser import detect_platform
        _detected = detect_platform(text)
        if _detected:
            result["platform"] = _detected
            platform = _detected
    if platform in ("拼多多", "美团") and not result.get("product_name"):
        _prod = extract_product_name(text, platform)
        if _prod and not _is_bad_product(_prod):
            result["product_name"] = _prod
        # 同时补时间（若模型/正则此前都未给出）
        if not result.get("order_time"):
            _ot = extract_order_time(text, platform)
            if _ot:
                result["order_time"] = _ot

    if mres or result.get("amount") is not None:
        # 补缺后重算置信度/状态
        result["confidence"] = synthesize_confidence(
            result.get("amount"), result.get("platform"),
        )
        result["status"] = (
            "success" if result["confidence"] >= threshold else "failed"
        )
        if mres:
            result["used_model"] = True
            result["reason"] = (result.get("reason") or "") + "（模型裁判）"
            # 透传模型原始返回（含完整 content / HTTP 错误体），供日志排查
            raw = mres.get("model_raw", "")
            if raw:
                result["model_raw"] = raw

    _require_product_for_images(result)
    return result


def recognize_vision(image_path, vision_backend, regex_result: dict | None = None) -> dict:
    """视觉模式单图识别：截图直送多模态大模型解析金额+平台。

    vision_backend: VisionBackend 实例（来自 llm_backend）。
    regex_result: 正则快照，喂给模型作裁判参考（非数据源）。
    异常兜底为失败，绝不让调用方崩溃。
    """
    try:
        res = vision_backend.parse_image(image_path, regex_result=regex_result)
        res["ocr_text"] = ""  # 视觉模式无 OCR 文本
        return res
    except Exception as e:  # noqa: BLE001
        return {
            "status": "failed", "amount": None, "platform": None,
            "confidence": 0.0, "reason": f"视觉识别异常: {e}",
            "ocr_text": "",
        }
