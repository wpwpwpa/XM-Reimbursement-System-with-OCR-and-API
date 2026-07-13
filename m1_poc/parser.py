"""模式 C 解析层：平台判定(强制正则) + 金额抽取(正则优先) + confidence 合成。

输出统一契约：{ "amount": float, "platform": str, "confidence": float }
"""
import re

# 平台判定：特征词独立互不覆盖，命中即定，不再向下。
# 淘宝系订单截图必含 天猫/淘宝/企(企业购)；美团靠 闪购；拼多多靠 拼小圈。
# 禁用 拼多多驿站(代收驿站非购物平台)、不靠裸词 拼多多(易与快递/驿站混淆)。
# 补充：
# - 拼多多特有「拼单时间」「成交时间」「先用后付」
# - 淘宝常见「交易快照」+「订单编号」组合
PLATFORM_RULES = [
    ("淘宝", ["天猫", "淘宝", "企", "交易快照"]),
    ("美团", ["闪购"]),
    ("拼多多", ["拼小圈", "拼单时间", "成交时间", "先用后付"]),
]

PLATFORM_WHITELIST = {"淘宝", "美团", "拼多多"}

# 金额关键词分级：先取最终付款字段(实付款/实付总额)，再退化到(实付/合计)。
# 截图常同时含商品小计(实付￥X)与最终付款(实付款￥Y)，必须优先后者(PRD:取实付款)。
AMOUNT_KEYWORDS_FINAL = ["实付款", "实付总额"]
AMOUNT_KEYWORDS_FALLBACK = ["实付", "合计"]
_AMOUNT_RE = re.compile(r"([\s\S]{0,15}?)¥?\s*(\d+(?:\.\d+)?)")
_NUM_RE = re.compile(r"¥?\s*(\d+(?:\.\d+)?)")


def detect_platform(text: str):
    """强制走正则，命中任一特征词即定平台；全未命中返回 None。"""
    for platform, keywords in PLATFORM_RULES:
        for kw in keywords:
            if kw in text:
                return platform
    return None


_DISCOUNT_RE = re.compile(r"共减|优惠|立减|减|折扣|抵扣")


def _match_amount_after(text: str, keywords: list[str]):
    """在关键词前后窗口内找金额（OCR 输出顺序可能与阅读顺序不同）。

    跳过被优惠减免词修饰的金额（如「实付款共减￥227.92」中的 227.92 是
    优惠额，真实付款在下行的 ￥361.79），避免把减免额误当实付款。
    """
    for kw in keywords:
        idx = text.find(kw)
        while idx != -1:
            # 优先关键词后方（常规阅读顺序）
            tail = text[idx: idx + len(kw) + 20]
            found = None
            for m in _AMOUNT_RE.finditer(tail):      # 遍历窗口内所有金额
                pre = tail[max(0, m.start(2) - 5): m.start(2)]  # 数字前的修饰词
                if _DISCOUNT_RE.search(pre):
                    continue                          # 优惠减免额，跳过
                found = float(m.group(2))
                break
            if found is not None:
                return found
            # 后方没有，则向前方窗口兜底（如 ￥295v\n实付款）
            head = text[max(0, idx - 20): idx + len(kw)]
            m = _NUM_RE.search(head)
            if m:
                return float(m.group(1))
            idx = text.find(kw, idx + 1)
    return None


def extract_amount(text: str):
    """正则优先抽实付金额。优先最终付款字段，退化到实付/合计，最后全文首个金额。"""
    val = _match_amount_after(text, AMOUNT_KEYWORDS_FINAL)
    if val is not None:
        return val
    val = _match_amount_after(text, AMOUNT_KEYWORDS_FALLBACK)
    if val is not None:
        return val
    # 兜底：全文第一个数字（可能不准）
    m2 = _NUM_RE.search(text)
    if m2:
        return float(m2.group(1))
    return None


def is_valid_amount(amount) -> bool:
    return isinstance(amount, (int, float)) and amount > 0 and amount < 1_000_000


def synthesize_confidence(amount, platform) -> float:
    """PRD 5.2/5.4.6 confidence 合成（默认阈值 0.70）。
    base 0.5 成功返回JSON；+0.3 字段完整；+0.1 金额合法合理；+0.1 平台归一化在白名单。
    缺 amount 或缺 platform → 合成分 < 0.70 → 失败。
    """
    conf = 0.5
    if amount is not None and platform is not None:
        conf += 0.3
    if is_valid_amount(amount):
        conf += 0.1
    if platform in PLATFORM_WHITELIST:
        conf += 0.1
    return round(conf, 2)


def parse(text: str, confidence_threshold: float = 0.70) -> dict:
    """纯文本 → 统一 JSON。金额/日期正则优先；LLM/视觉兜底在 recognizer 层（方案A 直接采用模型结果，不在此层）。"""
    platform = detect_platform(text)
    amount = extract_amount(text)
    confidence = synthesize_confidence(amount, platform)
    status = "success" if confidence >= confidence_threshold else "failed"
    return {
        "amount": amount,
        "platform": platform,
        "confidence": confidence,
        "status": status,
    }


if __name__ == "__main__":
    sample = "天猫超市\n订单号:12345\n实付款 ¥26.52\n企业购专享"
    print(parse(sample))
    sample2 = "美团闪购\n实付\n¥14.6\n闪购专送"  # 跨行金额
    print(parse(sample2))
    sample3 = "拼小圈\n已拼单\n合计 ¥14.1"  # 拼小圈 + 跨行
    print(parse(sample3))
