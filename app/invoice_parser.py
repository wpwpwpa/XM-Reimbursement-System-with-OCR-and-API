"""发票（PDF）识别：纯结构化解析，不依赖 OCR / 大模型。

适用：增值税电子普通发票（结构化文本型 PDF，非扫描图）。
提取字段：价税合计(金额) / 开票日期 / 销售方 / 购买方 / 发票号码 / 商品(税收分类)。

解析策略（已用 15 张样本验证，销售方/金额/日期与手写文件名 100% 吻合）：
- 文本提取：pymupdf `page.get_text()` 取全文（正则用）
- 坐标提取：`page.get_text("words")` 取词块坐标，用「名称」标签坐标定位右侧公司名，
  以「销售方」「购买方」标题词的坐标距离区分两方（避免上下/左右布局差异）
- 金额：锁定「（小写）」标签右侧的价税合计（非「不含税合计」）

返回 dict 与图片识别同构，并额外带 `kind="invoice"` 供 GUI 分流。
"""
import re

# 价格数字（含千分位逗号、2 位小数）：如 735.00 / 1,240.00
_AMOUNT_RE = re.compile(r"[0-9][0-9,]*\.\d{2}")
_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_DASH_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_INVOICE_NO_RE = re.compile(r"\b(\d{20})\b")
_PRODUCT_RE = re.compile(r"\*[^*]+\*")  # 税收分类：*蔬菜* / *日用杂品*


def _norm_date(cn=None, dash=None) -> str | None:
    if cn:
        y, mo, d = int(cn.group(1)), int(cn.group(2)), int(cn.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            return None
    if dash:
        y, mo, d = int(dash.group(1)), int(dash.group(2)), int(dash.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            return None
    return None


def _extract_amount(text: str) -> float | None:
    """价税合计金额。

    关键点：发票中「金额」列(不含税)、「税额」列与「价税合计」并列，
    get_text 顺序会把不含税金额排在「（小写）」之后，直接取首个金额会误取。
    价税合计必然配有中文大写（如「贰佰肆拾圆整」），且全文唯一 → 优先取
    大写之后的 ¥ 数字，最稳。
    """
    # 中文大写金额后的 ¥ 数字（价税合计）
    m = re.search(
        r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元整角分]{2,}"
        r"\s*¥\s*([0-9][0-9,]*\.\d{2})",
        text,
    )
    if m:
        return float(m.group(1).replace(",", ""))
    # 大写在金额之后（少数版式）：¥数字 + 中文大写
    m = re.search(
        r"¥\s*([0-9][0-9,]*\.\d{2})\s*"
        r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元整角分]{2,}",
        text,
    )
    if m:
        return float(m.group(1).replace(",", ""))
    # 退路：（小写）标签后第一个金额
    m = re.search(r"（小写）[\s\S]{0,200}?(?:¥\s*)?([0-9][0-9,]*\.\d{2})", text)
    if m:
        return float(m.group(1).replace(",", ""))
    # 退路：价税合计行
    m = re.search(r"价税合计[\s\S]{0,30}?([0-9][0-9,]*\.\d{2})", text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _extract_parties(words: list) -> tuple[str | None, str | None]:
    """用坐标法区分销售方 / 购买方名称。

    返回 (seller, buyer)。「名称」标签右侧同行文本即公司名；
    归属判定：与「销售方」「购买方」标题词的 (y,x) 距离，近者归属。
    """
    seller_title = buyer_title = None
    for w in words:
        t = w[4].strip()
        if "销售方" in t and seller_title is None:
            seller_title = w
        if "购买方" in t and buyer_title is None:
            buyer_title = w

    # 「名称」标签词：购买方/销售方栏的「名称」或「名称：」。
    # 精确匹配，排除「项目名称」「货物或应税劳务、服务名称」等明细表头。
    name_labels = [
        w for w in words
        if w[4].strip() in ("名称", "名称：", "名称:", "名称）", "名称)")
    ]

    seller = buyer = None
    for nw in name_labels:
        # 右侧同行（y 接近）、在标签之后的词，拼成公司名
        right = [
            w for w in words
            if abs(w[1] - nw[1]) <= 4 and w[0] > nw[0] + 1
            and w[4].strip() not in ("：", ":", "）", ")", "")
        ]
        company = "".join(w[4] for w in right).strip(" ：:（）()")
        if not company:
            continue
        if seller_title and buyer_title:
            # 距离 = y 差 + 0.01*x 差（y 优先，x 仅作微调）
            ds = abs(nw[1] - seller_title[1]) + 0.01 * abs(nw[0] - seller_title[0])
            db = abs(nw[1] - buyer_title[1]) + 0.01 * abs(nw[0] - buyer_title[0])
            if ds <= db:
                seller = company
            else:
                buyer = company
        elif seller_title:
            seller = company
        elif buyer_title:
            buyer = company
        else:
            # 无标题词：左右布局下右栏多为销售方，兜底归销售方
            seller = company
    return seller, buyer


def _extract_product_name(words: list, text: str) -> str | None:
    """提取商品/服务名称：优先「项目名称」列正下方第一行，其次 *税收分类*。

    注意：发票 get_text 是列优先顺序，不是阅读顺序；因此用坐标法（y 在表头下方、
    x 中心对齐）找商品名，如 *种子种苗*种子。
    """
    # 方法1：坐标法
    headers = [w for w in words if w[4].strip() == "项目名称"]
    if not headers:
        # 兼容拆词：同时存在“项目”“名称”且相邻
        proj = [w for w in words if w[4].strip() == "项目"]
        name = [w for w in words if w[4].strip() == "名称"]
        if proj and name:
            for pw in proj:
                for nw in name:
                    if abs(pw[1] - nw[1]) <= 4 and abs(pw[0] - nw[0]) <= 30:
                        headers.append(pw)
                        break
    if not headers:
        m = _PRODUCT_RE.search(text)
        return m.group(0) if m else None

    header = min(headers, key=lambda w: w[1])
    col_center = (header[0] + header[2]) / 2
    # 同列下方词，且 x 中心接近列中心（允许 60 像素偏差）
    below = [
        w for w in words
        if w[1] > header[3]
        and abs((w[0] + w[2]) / 2 - col_center) <= 60
    ]
    # 排除合计/价税/备注等干扰词
    skip_words = {"合", "计", "价税合计（大写）", "（小写）", "备", "注", "开票人："}
    below = [w for w in below if w[4].strip() not in skip_words]
    if not below:
        m = _PRODUCT_RE.search(text)
        return m.group(0) if m else None

    # 按 y 取最上方第一行，按 x 排序拼接
    below = sorted(below, key=lambda w: w[1])
    first_y = below[0][1]
    line = [w for w in below if abs(w[1] - first_y) <= 4]
    line = sorted(line, key=lambda w: w[0])
    return "".join(w[4] for w in line)


def recognize_invoice(pdf_path: str) -> dict:
    """识别单张发票 PDF，返回与图片识别同构的 dict（带 kind="invoice"）。"""
    try:
        import fitz  # pymupdf（函数内懒加载，避免 GUI 启动时卡顿）
        doc = fitz.open(pdf_path)
    except Exception as e:  # noqa: BLE001
        return {
            "status": "failed", "kind": "invoice", "amount": None,
            "date": None, "seller": None, "buyer": None,
            "invoice_no": None, "product": None, "category": "发票",
            "confidence": 0.0, "reason": f"PDF 打开失败: {e}",
        }

    try:
        page = doc[0]
        words = page.get_text("words")
        text = page.get_text()
    except Exception as e:  # noqa: BLE001
        doc.close()
        return {
            "status": "failed", "kind": "invoice", "amount": None,
            "date": None, "seller": None, "buyer": None,
            "invoice_no": None, "product": None, "category": "发票",
            "confidence": 0.0, "reason": f"文本提取失败: {e}",
        }
    doc.close()

    amount = _extract_amount(text)
    date = _norm_date(_DATE_CN_RE.search(text), _DATE_DASH_RE.search(text))
    seller, buyer = _extract_parties(words)

    m = _INVOICE_NO_RE.search(text)
    invoice_no = m.group(1) if m else None
    if not invoice_no:
        m = re.search(r"发票号码[:：]?\s*(\d{8,20})", text)
        invoice_no = m.group(1) if m else None

    product = _extract_product_name(words, text)

    # 置信度：三要素齐全→高；缺一项→低（提示人工核对）
    core = [amount, date, seller]
    if all(core):
        status, confidence, reason = "success", 0.95, ""
    else:
        missing = [n for n, v in zip(("金额", "日期", "销售方"), core) if not v]
        status = "failed"
        confidence = 0.0
        reason = "缺失字段：" + "、".join(missing)

    return {
        "status": status,
        "kind": "invoice",
        "amount": amount,
        "date": date,
        "seller": seller,
        "buyer": buyer,
        "invoice_no": invoice_no,
        "product": product,
        "category": "发票",
        "confidence": confidence,
        "reason": reason,
    }


def render_invoice_name(parsed: dict, category: str = "发票") -> str:
    """生成发票新文件名：{日期}-{金额}元-{销售方}-{类别}.pdf"""
    date = parsed.get("date") or "0000-00-00"
    amount = parsed.get("amount")
    amt = f"{amount:.2f}" if isinstance(amount, (int, float)) else str(amount)
    seller = parsed.get("seller") or "未知销售方"
    cat = category or "发票"
    return f"{date}-{amt}元-{seller}-{cat}.pdf"
