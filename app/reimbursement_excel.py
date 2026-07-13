"""报销系统「消费明细导入」Excel 生成。

参考「消费明细导入模版.xlsx」的固定结构（说明文字/费用类型名/系统ID/表头），
把 OCR 识别结果转换为可导入报销系统的明细 Excel。

模板固定结构（不可更改，否则导入失败，见模板 Row1 规则 a）：
  Row1: 说明文字（从模板复制，原样保留）
  Row2: 费用类型名（= Sheet 名，由用户填写，默认"买耗材"）
  Row3: 系统内部类型 ID（从模板复制）
  Row4: 列名表头（从模板复制：金额/消费日期/发票形式/发票/消费事由）
  Row5+: 数据行

字段映射（与「买耗材」类型一致）：
  金额       ← parsed["amount"]
  消费日期   ← parsed["order_time"]（yyyy-MM-dd）
  发票形式   ← 留空（按需求）
  发票       ← 有截图→0 / 其他→1（为后续发票PDF识别预留扩展位）
  消费事由   ← "{月}月{日}日，{平台}购买{商品}等，共计{金额}元。"
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook, load_workbook
from excel_utils import set_col_widths

import warnings


def _build_reason(order_time: str | None, platform: str,
                  product: str | None, amount) -> str:
    """生成消费事由：{月}月{日}日，{平台}购买{商品}等，共计{金额}元。

    参考用户已填好的截图风格。缺字段时优雅降级，避免生成残缺句子。
    """
    # 月/日：从 order_time(yyyy-MM-dd) 取；缺失或异常则不填日期前缀
    md = ""
    if order_time:
        try:
            _, mo, d = order_time.split("-")
            md = f"{int(mo)}月{int(d)}日"
        except Exception:
            md = ""
    date_part = f"{md}，" if md else ""

    # 平台 + 购买动作（平台缺失时退化为"购买"）
    buy_part = f"{platform}购买" if platform else "购买"
    if product:
        buy_part = f"{buy_part}{product}等"

    amt_str = f"{amount}元" if amount is not None else "元"
    return f"{date_part}{buy_part}，共计{amt_str}。"


def invoice_count(parsed: dict) -> int:
    """发票数量：有截图(本报销来源)→0，其他→1。

    本项目识别来源均为截图文件，故当前一律返回 0。
    后续接入发票 PDF 识别后：无源截图的条目→1，有源截图→0。
    若解析器显式给出 invoice_count 则优先采用（扩展位）。
    """
    if parsed.get("invoice_count") is not None:
        return int(parsed["invoice_count"])
    return 0  # 当前结果均来自截图


def build_reimbursement_rows(results, files) -> list[dict]:
    """由识别结果构造报销明细行。results/files 须对齐。

    仅导出「识别成功」且具备「金额 + 消费日期」的条目
    （消费日期为模板必填列，缺则无法导入）。
    """
    rows = []
    for f, parsed in zip(files, results):
        if not parsed or parsed.get("status") != "success":
            continue
        amount = parsed.get("amount")
        order_time = parsed.get("order_time")
        if amount is None or not order_time:
            continue  # 缺必填项，跳过（无法导入）
        rows.append({
            "金额": amount,
            "消费日期": order_time,
            "发票形式": "",                       # 按需求留空
            "发票": invoice_count(parsed),
            "消费事由": _build_reason(
                order_time, parsed.get("platform") or "",
                parsed.get("product_name"), amount),
        })
    return rows


def _clean_header(h) -> str:
    """模板表头形如「金额[「金额值和符号」]，取方括号前的真实列名。"""
    if h is None:
        return ""
    return str(h).split("[")[0].strip()


def write_reimbursement_excel(template_path: str, expense_type: str,
                              rows: list[dict], out_path: str) -> str:
    """以导入模板为骨架生成报销明细 Excel。

    复制模板的 Row1(说明)/Row3(系统ID)/Row4(表头)，Row2 与 Sheet 名
    用 expense_type 覆盖，Row5+ 按模板表头顺序写入数据行。
    """
    wb = load_workbook(template_path)
    src = wb.active

    instruction = src["A1"].value     # Row1 说明文字
    sys_id = src["A3"].value          # Row3 系统类型 ID

    # 读模板 Row4 表头（剔除末尾空列）
    headers = [_clean_header(src.cell(row=4, column=c).value)
               for c in range(1, src.max_column + 1)]
    headers = [h for h in headers if h]

    new_wb = Workbook()
    ws = new_wb.active
    ws.title = expense_type           # Sheet 名 = 费用类型
    if instruction is not None:
        ws["A1"] = instruction        # Row1 说明（原样保留）
    ws["A2"] = expense_type           # Row2 费用类型名
    if sys_id is not None:
        ws["A3"] = sys_id             # Row3 系统 ID

    # Row4：表头
    for c, h in enumerate(headers, start=1):
        ws.cell(row=4, column=c, value=h)

    # Row5+：数据（按模板表头顺序映射）
    for r_idx, row in enumerate(rows, start=5):
        for c, h in enumerate(headers, start=1):
            ws.cell(row=r_idx, column=c, value=row.get(h, ""))

    # 列宽（共享 helper，支持 > 26 列）
    set_col_widths(ws, [22] * len(headers))

    out_path = str(Path(out_path).with_suffix(".xlsx"))
    # 局部屏蔽 openpyxl 的"Workbook contains no default style"提示，
    # 不污染全局警告设置（避免掩盖其他真实问题）
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        new_wb.save(out_path)
    return out_path
