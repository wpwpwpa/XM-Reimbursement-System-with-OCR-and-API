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
from pathlib import Path

# 价格数字（含千分位逗号、2 位小数）：如 735.00 / 1,240.00
_AMOUNT_RE = re.compile(r"[0-9][0-9,]*\.\d{2}")
_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_DASH_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_INVOICE_NO_RE = re.compile(r"\b(\d{20})\b")
_PRODUCT_RE = re.compile(r"\*[^*]+\*")  # 税收分类：*蔬菜* / *日用杂品*


def _to_ymd(y, mo, d) -> str | None:
    """把年/月/日组成本地化 YYYY-MM-DD；任一非数字则 None。"""
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except Exception:
        return None


def _norm_date(cn=None, dash=None) -> str | None:
    if cn:
        return _to_ymd(cn.group(1), cn.group(2), cn.group(3))
    if dash:
        return _to_ymd(dash.group(1), dash.group(2), dash.group(3))
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


def _find_title(words: list, target: str):
    """在 words 中查找标题词（兼容竖排拆字，如 '销'/'售'/'方' 分散成多词）。

    返回命中的代表 word（取标题首字所在 word），未找到返回 None。
    """
    # 1) 完整/包含匹配（横排，如淘宝）
    for w in words:
        if target in w[4].replace(" ", ""):
            return w
    # 2) 竖排：同列（x 接近）单字按 y 递增拼接，看是否含 target（如京东）
    singles = [
        w for w in words
        if len(w[4].replace(" ", "")) == 1
        and w[4].strip() not in ("：", ":", "）", ")", "（", "(")
    ]
    cols: dict[float, list] = {}
    for w in singles:
        cx = round((w[0] + w[2]) / 2 / 8) * 8  # 列量化
        cols.setdefault(cx, []).append(w)
    for col in cols.values():
        col.sort(key=lambda w: w[1])
        phrase = "".join(w[4].replace(" ", "") for w in col)
        if target in phrase:
            return col[phrase.find(target)]
    return None


def _extract_parties(words: list) -> tuple[str | None, str | None]:
    """用坐标法区分销售方 / 购买方名称。

    返回 (seller, buyer)。
    - 「名称」标签右侧同行文本即公司名（横排，如淘宝）；
    - 若为上下布局（标签在上、公司在下，如京东竖排 PDF），取标签同列下方第一行；
    - 标题词兼容竖排拆字（'销售方' 可能拆成 '销'/'售'/'方'）；
    - 归属：与「销售方」「购买方」标题词距离近者归属；均无标题时按名称标签 y 顺序（上=销售方）。
    """
    seller_title = _find_title(words, "销售方")
    buyer_title = _find_title(words, "购买方")

    def _is_name_label(t: str) -> bool:
        norm = t.replace(" ", "")
        return norm.startswith("名称") and not any(
            k in norm for k in ("项目", "劳务", "服务", "应税")
        )

    name_labels = [w for w in words if _is_name_label(w[4].strip())]
    _SKIP = ("：", ":", "）", ")", "（", "(", "")

    seller = buyer = None
    for nw in name_labels:
        # 同行右侧（横排）
        right = [
            w for w in words
            if abs(w[1] - nw[1]) <= 4 and w[0] > nw[0] + 1
            and w[4].strip() not in _SKIP
        ]
        if not right:
            # 同列下方第一行（上下布局，如京东）
            col_c = (nw[0] + nw[2]) / 2
            below = [
                w for w in words
                if w[1] > nw[3] and abs((w[0] + w[2]) / 2 - col_c) <= 60
                and w[4].strip() not in _SKIP
            ]
            if below:
                below.sort(key=lambda w: w[1])
                first_y = below[0][1]
                right = sorted(
                    [w for w in below if abs(w[1] - first_y) <= 4],
                    key=lambda w: w[0],
                )
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
            # 无标题词：按名称标签 y 顺序，上方（先出现）为销售方
            if seller is None:
                seller = company
            elif buyer is None:
                buyer = company
    return seller, buyer


def _extract_seller_by_text(text: str) -> str | None:
    """坐标法失败时的文本兜底：从销售方区块提取公司名称。

    兼容竖排标签（'销售方'→'销\\n售\\n方'，'名称'→'名 称'），
    且允许「名称:」与公司名跨行（中间隔信用代码等行）。
    """
    # 竖排标签还原
    norm = re.sub(r"销\s*售\s*方", "销售方", text)
    norm = re.sub(r"名\s*称", "名称", norm)

    # 1) 销售方区块内「名称」后的公司名（非贪婪跳过中间行，捕获含公司后缀的行）
    m = re.search(
        r"销售方.*?名称[:：]\s*((?:[^\n]*\n)*?)([^\n]*(?:有限公司|公司|集团|股份)[^\n]*)",
        norm, re.DOTALL,
    )
    if m:
        return m.group(2).strip(" ：:（）()")

    # 2) 兜底：全文第一个「名称」后的公司名
    m2 = re.search(
        r"名称[:：]\s*((?:[^\n]*\n)*?)([^\n]*(?:有限公司|公司|集团|股份)[^\n]*)",
        norm, re.DOTALL,
    )
    if m2:
        return m2.group(2).strip(" ：:（）()")
    return None


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
    if not seller:
        seller = _extract_seller_by_text(text)

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
