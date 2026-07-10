"""订单字段抽取：从 OCR 文本按平台抽出下单时间/期望时间 与 商品名称。

- 拼多多：时间取「下单时间」后日期；商品名取「商品名称」后文本
- 美团：时间取「期望时间」后日期；商品名取「商品」类标签后文本
- 淘宝：时间与商品名均来自订单 Excel（不在本模块处理，返回 None）

时间统一归一化为 yyyy-MM-dd（Excel 原生日期 / 多种文本写法均兼容）。
"""
import re

_DATE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")
# 美团/拼多多无年格式：M月D日 / MM月DD日（自动补当年份）
_DATE_SHORT_RE = re.compile(r"(?:^|[^0-9])(\d{1,2})[月](\d{1,2})[日]")

# 平台 → 时间字段关键词（列表：按优先级依次尝试）
TIME_KEYWORD = {
    "拼多多": ["下单时间"],
    "美团": ["下单时间", "支付时间", "期望时间"],
}

# 商品名标签（按优先级）；命中其一即取其后文本。
# 注意：不放裸「商品」——美团/拼多多的「商品费用」「商品快照」是 UI 文案行，
# 会误命中导致取到按钮/状态文字；标签缺失时交金额锚点兜底处理更稳。
PRODUCT_LABELS = ["商品名称", "宝贝标题", "商品信息", "商品标题"]

# 结果黑名单：OCR 常把截图里的 UI 按钮/标签文字当商品名，直接丢弃。
# 全是电商截图底部按钮或状态区文案，不可能是真实商品。
BAD_PRODUCT_NAMES = {
    "联系商家", "联系客服", "联系卖家", "客服", "费用", "费用合计",
    "查看物流", "复制", "复制链接", "查看详情", "立即购买", "加入购物车",
    "去购买", "去支付", "支付", "确认收货", "申请退款", "退款", "评价",
    "晒单", "分享", "收藏", "店铺", "进店", "关注", "更多", "展开", "收起",
    "全部", "订单", "订单详情", "暂无", "暂无数据", "加载中", "优惠", "优惠详情",
    # 已知误命中店铺名（整串）
    "菜来了", "小橙阿姨", "绿氧森林园艺店",
}


def normalize_date(raw) -> str | None:
    """把各种日期写法归一化为 yyyy-MM-dd；无法解析返回 None。

    支持格式：
    - 4位年：2026-07-05 / 2026.7.5 / 2026/07/05 / 2026年7月5日
    - 无年（补当年份）：6月29日 / 06月13日（美团/拼多多常见）
    """
    if raw is None:
        return None
    if hasattr(raw, "strftime"):  # datetime / date 对象（Excel 原生日期）
        return raw.strftime("%Y-%m-%d")
    s = str(raw).strip()
    m = _DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    # 无年格式：M月D日 → 补当年份
    m2 = _DATE_SHORT_RE.search(s)
    if m2:
        from datetime import date as _date
        mo, d = int(m2.group(1)), int(m2.group(2))
        y = _date.today().year
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    return None


def _find_after_keyword(text: str, keyword: str, window: int = 40) -> str:
    """返回关键词后方 window 字符内的文本（用于找日期）。"""
    idx = text.find(keyword)
    if idx == -1:
        return ""
    return text[idx: idx + len(keyword) + window]


def extract_order_time(text: str, platform: str | None) -> str | None:
    """按平台取下单/期望时间，归一化为 yyyy-MM-dd。淘宝返回 None。"""
    if not platform or platform not in TIME_KEYWORD:
        return None
    for keyword in TIME_KEYWORD[platform]:
        chunk = _find_after_keyword(text, keyword)
        if chunk:
            d = normalize_date(chunk)
            if d:
                return d
    return None


def _lines(text: str):
    return [ln.strip() for ln in text.splitlines()]


# 商品名尾巴里的规格/价格词，命中即截断，只留标题主体
_SPEC_RE = re.compile(
    r"(规格|数量|实付|打包费|配送费|运费|约?\d+(\.\d+)?[gG克]|/\d+[gG袋]|"
    r"×|x\d+|\d+件|\d+瓶|\d+包|\d+袋|\d+盒|Q[0-9]_[A-Z]|"
    r"\d+(\.\d+)?斤|\d+码|\d+[mM][lL]|\d+[lL])"
)


def _strip_spec(name: str) -> str:
    """截断商品名跟随的规格/价格尾巴，只留标题主体。

    例：『鹤来香黄小米500g/袋 实付9.1』→『鹤来香黄小米』。
    截断后若为空则保留原值（避免整行即是规格时误伤）。
    """
    if not name:
        return name
    m = _SPEC_RE.search(name)
    if not m:
        return name
    head = name[:m.start()].strip()
    return head or name


def _is_bad_product(name: str) -> bool:
    """结果黑名单：OCR 误把 UI 按钮/状态文案/金额行/标签尾巴当商品名时拦截。"""
    if not name:
        return True
    if len(name) <= 1:
        return True
    if name in BAD_PRODUCT_NAMES:
        return True
    # 标签尾巴（短标签「商品」切「商品名称」行会露出「名称」等），绝非零散商品名
    if name in ("名称", "信息", "标题"):
        return True
    # 扩充匹配：含「联系商家」等长尾变体的子串
    for bad in ("联系商家", "联系客服", "查看物流", "立即购买", "加入购物车",
                "去支付", "确认收货", "申请退款", "复制链接"):
        if bad in name:
            return True
    # 金额行不可能是商品名（¥14.1 / 14.10 / ￥9.9 等）
    if name.startswith(("¥", "￥")) or re.fullmatch(r"\d+(\.\d+)?", name):
        return True
    # 店铺名/商家名（明确店后缀，避免裸「店」误伤便利店类真商品名）
    if any(suffix in name for suffix in
           ("旗舰店", "专卖店", "企业店", "官方店", "店铺", "市场店")):
        return True
    # 物流/快递/费用相关（具体快递公司名、订单状态词、费用行）
    for bad in ("快递", "物流", "包裹", "申通", "圆通", "中通", "韵达", "极兔",
                "顺丰", "发生交易", "交易争议", "订单编号", "待揽收", "已签收",
                "费用", "费用合计"):
        if bad in name:
            return True
    return False


# ---------- 金额锚点兜底：商品名 = 夹在噪声结构之间的纯文本段 ----------

# 整行噪声（店/群/按钮/状态/物流/营销/UI标签/价格行）。命中整行即排除。
_ANCHOR_NOISE = (
    "店", "店铺", "旗舰店", "专卖店", "企业店", "官方店", "市场店",
    "群", "粉丝群", "进商家", "加入", "去其他店", "联系商家", "联系客服",
    "申请退款", "分享商品", "查看物流", "再次拼单", "确认收货", "复制",
    "已签收", "包裹", "交易成功", "快递", "物流", "号码保护", "极兔",
    "申通", "圆通", "中通", "韵达", "顺丰", "成长值", "代金券", "订单信息",
    "商品费用", "期望时间", "配送地址", "打包费", "配送费", "实付", "实付款",
    "承诺达", "安心闪购", "专属权益", "本单", "提前", "送达", "不支持",
    "7天无理由", "真空包装", "规格", "数量", "费用", "×",
)


def _line_is_noise(ln: str) -> bool:
    """整行噪声判定：含店/群/按钮/状态/物流/营销/价格等结构词，或非中文为主。"""
    if not ln:
        return True
    if ln.startswith("<"):          # 状态行（<本单已提前…送达）
        return True
    if "￥" in ln or "¥" in ln:     # 价格行（实付￥13.6 / ￥14.1）
        return True
    for kw in _ANCHOR_NOISE:
        if kw in ln:
            return True
    if ln in BAD_PRODUCT_NAMES:
        return True
    return False


def _looks_like_product(ln: str) -> bool:
    """像商品标题：足够长、以中文为主、不含噪声结构。"""
    if not ln or len(ln) < 4:
        return False
    if _line_is_noise(ln):
        return False
    cn = sum(1 for ch in ln if "一" <= ch <= "鿿")
    if cn < 2:
        return False
    return True


def _find_first(lines, pred):
    for i, ln in enumerate(lines):
        if pred(ln):
            return i
    return None


def _extract_by_label(lines):
    """原 PRODUCT_LABELS 策略：取标签后文本（或下一非空行）。"""
    skip_prefixes = ("实付", "合计", "下单", "期望", "订单", "金额", "运费", "优惠")
    for label in PRODUCT_LABELS:
        for i, ln in enumerate(lines):
            if label in ln:
                after = ln.split(label, 1)[1].strip(" ：:·\t")
                after = _strip_spec(after)
                if after and not _is_bad_product(after):
                    return after
                for nxt in lines[i + 1: i + 3]:
                    if nxt and not nxt.startswith(skip_prefixes):
                        nxt2 = _strip_spec(nxt)
                        if nxt2 and not _is_bad_product(nxt2):
                            return nxt2
    return None


def _extract_by_price_anchor(lines, platform):
    """金额锚点兜底：商品名夹在噪声结构（店/群/按钮/状态/￥/×）之间的纯文本段。

    - 美团：首个「实付￥/实付款 ￥」为终点，向上回溯到首个商品行（跳过店/群/状态）。
    - 拼多多：首个「￥数字」为主行，向上回溯到首个商品行、向下取紧邻续行（遇 ×/规格停）。
    商品名不会被金额/数量符号拆分，故符号两侧文本段拼接即完整标题。
    """
    n = len(lines)
    if platform == "美团":
        anchor = _find_first(lines, lambda ln: ("实付" in ln or "实付款" in ln)
                             and ("￥" in ln or "¥" in ln))
    else:  # 拼多多
        anchor = _find_first(lines, lambda ln: ("￥" in ln or "¥" in ln)
                             and re.search(r"[￥¥]\s*\d", ln))
    if anchor is None:
        return None

    # 向上回溯：从锚点前一行找首个「商品行」作为起点，跳过店/群/状态/物流噪声
    start = anchor
    for i in range(anchor - 1, -1, -1):
        if _line_is_noise(lines[i]):
            continue
        if _looks_like_product(lines[i]):
            start = i
            break
        break  # 既非噪声也非商品 → 停止，避免越界到无关区

    # 向下扩展：仅拼多多需要（￥夹在标题中间，后半在锚点后）。
    # 美团标题绝不在「实付￥」之后，向下扩展只会误吞「精选小米」等分类标签。
    end = anchor
    if platform == "拼多多":
        for i in range(anchor + 1, min(anchor + 5, n)):
            ln = lines[i]
            if _line_is_noise(ln):
                break
            if _looks_like_product(ln):
                end = i
            else:
                break

    kept = [lines[i] for i in range(start, end + 1) if not _line_is_noise(lines[i])]
    if not kept:
        return None
    # 锚点兜底不调 _strip_spec：商品名是符号（￥/×）间的完整文本段，
    # 标题内自带的重量/规格（如 瘦肉约100g、黄小米500g/袋）属标题一部分，不剥。
    name = "".join(kept).strip()
    return name if name and not _is_bad_product(name) else None


def extract_product_name(text: str, platform: str | None) -> str | None:
    """从 OCR 文本抽商品名（拼多多/美团）。淘宝返回 None（来自 Excel）。

    策略：
    1) PRODUCT_LABELS 优先（订单含「商品名称/宝贝标题」等标签时直接取其后文本）。
    2) 标签缺失时兜底：金额锚点法——商品名 = 夹在噪声结构之间的纯文本段。
       - 美团：首个「实付￥」为终点，向上回溯跳过店/群/状态到首个商品行。
       - 拼多多：首个「￥数字」为主行，向上回溯到首个商品行、向下取续行。
    """
    if not platform or platform not in ("拼多多", "美团"):
        return None
    lines = _lines(text)
    if not lines:
        return None

    # 1) 标签优先
    by_label = _extract_by_label(lines)
    if by_label:
        return by_label

    # 2) 金额锚点兜底
    return _extract_by_price_anchor(lines, platform)
