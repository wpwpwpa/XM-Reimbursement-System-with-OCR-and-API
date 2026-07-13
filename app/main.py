"""报销截图智能重命名 — GUI 主框架（M2/M3/M4/M5）。

- M2：三页静态布局（文件处理 / 模型选择 / 设置）
- M3：OCR 识别接入（模式 C：RapidOCR + 正则），后台线程逐张识别填表
- M4：重命名执行 + 冲突处理 + 日志 + 撤销 + 配置持久化
- M5：LLM 增强兜底 + 进度条 + 低置信度⚠️标记（正则失败时可选调 OpenAI 兼容 API）

识别逻辑复用 m1_poc 的 OcrBackend / parse，经 app/recognizer 封装。
LLM 后端经 app/llm_backend 封装，支持正则判断/本地API/云端API。
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFrame, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

# 仅导入提示词常量（纯字符串，不触发重型依赖加载）
from llm_backend import _OCR_PROMPT, _VISION_PROMPT  # noqa: E402


IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS
PAGE_SIZE = 10  # PRD 6.1：结果表格每页 10 条
# 结果表列索引：图片(订单)与发票(PDF)共用，列语义通用化
# 原 平台/时间/商品 → 类型/日期/发票号（图片：平台名/下单时间/商品名；发票：发票/开票日期/发票号）
COL_ORIG, COL_AMOUNT, COL_TYPE, COL_DATE, COL_PRODUCT_NAME, COL_NEWNAME, COL_STATUS, COL_RERUN = range(8)
# PyInstaller frozen 时 __file__ 指向只读临时目录 _MEIxxxxx，
# 配置/日志必须落到 exe 同级（可写）。开发模式仍用项目根。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_LOG_DIR = BASE_DIR / "logs"
# 报销导入模板默认路径：置空，由用户在导出对话框选择并持久化（不硬编码本机桌面）
DEFAULT_REIMB_TEMPLATE = ""
# M5：成功但置信度低于此值 → 标记 ⚠️，提示人工核对（PRD 5.4.6）
LOW_CONF_WARN = 0.85

import base64 as _b64


def _obf_encode(s: str) -> str:
    """轻量混淆：base64 编码，避免密钥明文出现在 config.json。

    注意：这不是加密，只是防肉眼直接看到，懂技术的人仍能解码。
    """
    if not s:
        return ""
    return _b64.b64encode(s.encode("utf-8")).decode("ascii")


def _obf_decode(s: str) -> str:
    """还原混淆的密钥。若不是合法 base64（例如旧配置明文），原样返回。"""
    if not s:
        return ""
    try:
        return _b64.b64decode(s.encode("ascii")).decode("utf-8")
    except Exception:
        return s


def _rec_fail(reason: str) -> dict:
    """统一识别失败结果契约。"""
    return {
        "status": "failed", "amount": None, "platform": None,
        "confidence": 0.0, "reason": reason,
    }

TAB_STYLE = """
QPushButton {
    border: none; color: #ecf0f1; background: #2c3e50;
    padding: 14px 8px; font-size: 14px; text-align: left;
}
QPushButton:checked { background: #1abc9c; color: #fff; font-weight: bold; }
QPushButton:hover:!checked { background: #34495e; }
"""

CARD_STYLE = """
QFrame#modelCard {
    border: 1px solid #bdc3c7; border-radius: 6px;
    background: #ffffff; padding: 10px;
}
QFrame#modelCardSelected {
    border: 2px solid #1abc9c; border-radius: 6px;
    background: #f4fffb; padding: 10px;
}
"""

KPI_STYLE = """
QFrame#kpi { background: #ecf0f1; border-radius: 6px; padding: 10px 14px; }
QLabel#kpiNum { font-size: 22px; font-weight: bold; color: #2c3e50; }
QLabel#kpiLabel { font-size: 12px; color: #7f8c8d; }
"""

PRIMARY_BTN = (
    "QPushButton{background:#1abc9c;color:#fff;padding:8px 16px;"
    "border-radius:4px;font-size:13px;}"
    "QPushButton:hover{background:#16a085;}"
    "QPushButton:disabled{background:#bdc3c7;color:#ecf0f1;}"
)
SECONDARY_BTN = (
    "QPushButton{background:#fff;color:#2c3e50;border:1px solid #bdc3c7;"
    "padding:8px 16px;border-radius:4px;font-size:13px;}"
    "QPushButton:hover{background:#ecf0f1;}"
    "QPushButton:disabled{color:#bdc3c7;border-color:#ecf0f1;}"
)


def load_config() -> dict:
    """读取项目根 config.json；缺失/损坏返回空字典。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class RecognitionWorker(QThread):
    """后台逐张识别，避免 RapidOCR 加载/推理阻塞 UI。

    row_done 携带完整 parsed dict（含 confidence / used_llm），供重命名/日志复用。
    """

    row_done = pyqtSignal(int, dict, str)   # row, parsed, new_name
    progress = pyqtSignal(int, int)          # current, total
    finished = pyqtSignal(int, int)          # recognized, failed
    warn = pyqtSignal(str)                   # 非致命提示（Excel 匹配等）

    def __init__(self, files, template: str, threshold: float,
                 llm_kind: str = "正则判断", llm_cfg: dict | None = None,
                 mode: str = "OCR+文字模型", vision_cfg: dict | None = None,
                 taobao_excel: str | None = None, prefix_date: bool = False,
                 invoice_category: str = "发票", hint_year: int | None = None):
        super().__init__()
        self.files = files
        self.template = template
        self.threshold = threshold
        self.llm_kind = llm_kind
        self.llm_cfg = llm_cfg or {}
        self.mode = mode
        self.vision_cfg = vision_cfg or {}
        self.taobao_excel = taobao_excel   # 淘宝订单 Excel 路径（可选）
        self.prefix_date = prefix_date     # 重命名时是否把时间前缀到文件名
        self.invoice_category = invoice_category  # 发票文件名末尾的类别值
        # 主年份提示：单图/少量重识别时本批年份样本不足，用它兜底（来自已识别成功行）
        self.hint_year = hint_year

    def run(self):
        recognized = failed = 0
        total = len(self.files)
        mode = self.mode

        # 淘宝订单 Excel（后期按商品名匹配时间/金额）；加载失败不影响图片识别
        excel_data = None
        if self.taobao_excel:
            try:
                from order_excel import load_taobao_excel
                excel_data = load_taobao_excel(self.taobao_excel)
                if not excel_data["rows"]:
                    self.warn.emit("淘宝订单Excel无有效数据行，淘宝时间/商品名将缺失")
            except Exception as e:
                self.warn.emit(f"淘宝订单Excel读取失败: {e}")

        # 混合文件夹：仅当存在图片时才加载 OCR/视觉后端；PDF 发票无需这些
        has_image = any(Path(f).suffix.lower() in IMAGE_EXTS for f in self.files)

        # ---- 门控架构：三方法统一「OCR+正则前置」，仅难图调模型兜底 ----
        # 方法分类（兼容新旧 mode 取值）：
        #   视觉模型：mode 含「视觉」（视觉模型·本地/云端、旧「本地视觉模型」「云端视觉 API」）
        #   文字模型：mode 以「文字模型」开头 或 旧「OCR+文字模型」
        #   OCR+正则：其余（含旧 OCR+文字模型但 llm_kind=正则判断 → llm 为 None，等价纯前端）
        # （函数内导入重型模块，启动时懒加载）
        if has_image:
            from recognizer import recognize_vision, recognize_image_gated, render_template
            from llm_backend import build_llm_backend, build_vision_backend
        else:
            # PDF-only 模式：不需 OCR/模型后端，但需要新文件名生成
            from recognizer import render_template
        from invoice_parser import recognize_invoice, render_invoice_name
        is_vision = "视觉" in mode
        is_text = (not is_vision) and (
            mode.startswith("文字模型") or mode == "OCR+文字模型"
        )

        # 视觉后端：仅视觉方法加载
        vision = None
        if is_vision and has_image:
            vkind = "本地视觉模型" if "本地" in mode else "云端视觉 API"
            try:
                vision = build_vision_backend(vkind, self.vision_cfg)
            except Exception:  # noqa: BLE001
                vision = None
            if vision is None:
                self.warn.emit("视觉后端未配置，难图将无法兜底（PDF 发票不受影响）")

        # 文字模型 LLM：仅文字方法加载（正则判断 kind 会返回 None → 退化为纯前端）
        llm = None
        if is_text and has_image:
            try:
                llm = build_llm_backend(self.llm_kind, self.llm_cfg)
            except Exception:  # noqa: BLE001
                llm = None

        # OCR 后端：三方法共用的前置，只要有图片就加载
        backend = None
        if has_image:
            try:
                from m1_poc.ocr_backend import OcrBackend
                backend = OcrBackend()  # 模型较重，在线程内加载
            except Exception as e:  # noqa: BLE001
                backend = None
                self.warn.emit(f"OCR 后端加载失败，图片将无法识别（PDF 发票不受影响）: {e}")

        # 装配兜底引擎 model_fn(image_path, ocr_text) -> dict；None = 纯前端不调模型
        # 方案A：商品名直接由模型判断，不二次仲裁
        model_fn = None
        if is_vision and vision is not None:
            model_fn = lambda path, _text: recognize_vision(path, vision)
        elif is_text and llm is not None:
            model_fn = lambda _path, text: llm.parse(text)

        # 阶段A：逐图识别（门控：正则前置，难图才调模型；不做 Excel/年份后处理）
        parsed_list = []
        for i, f in enumerate(self.files):
            # 单图异常/文件缺失只记失败，绝不让线程崩溃拖垮整个应用
            try:
                if not Path(f).exists():
                    parsed = _rec_fail("文件不存在（可能已被移动或重命名）")
                elif Path(f).suffix.lower() in PDF_EXTS:
                    parsed = recognize_invoice(f)   # 发票：结构化解析，不依赖 OCR/模型
                elif Path(f).suffix.lower() in IMAGE_EXTS and backend is not None:
                    parsed = recognize_image_gated(
                        backend, f, model_fn, self.threshold,
                    )
                else:
                    parsed = _rec_fail("该文件类型在当前模式下不支持识别")
            except Exception as e:  # noqa: BLE001
                parsed = _rec_fail(f"识别异常: {e}")
            parsed_list.append(parsed)
            self.progress.emit(i + 1, total)

        # 阶段B：后期处理（年份补全 + 淘宝 Excel 商品名匹配），全由 Python 完成
        self._postprocess(parsed_list, excel_data)

        # 回填结果 + 生成新文件名
        for i, (f, parsed) in enumerate(zip(self.files, parsed_list)):
            status = parsed.get("status")
            ext = Path(f).suffix.lower()
            if status == "success":
                recognized += 1
                if parsed.get("kind") == "invoice":
                    # 发票：独立模板（日期-金额元-销售方-类别.pdf）
                    new_name = render_invoice_name(parsed, self.invoice_category)
                elif self.prefix_date and parsed.get("order_time"):
                    # 时间 + 平台 + 价格（用户勾选「时间前缀」时）
                    new_name = f"{parsed['order_time']}{parsed['platform']}{parsed['amount']}{ext}"
                else:
                    new_name = render_template(
                        self.template, parsed["amount"], parsed["platform"], ext,
                    )
            else:
                failed += 1
                new_name = ""
            self.row_done.emit(i, parsed, new_name)
        self.finished.emit(recognized, failed)

    def _postprocess(self, parsed_list: list, excel_data: dict | None):
        """后期处理（全 Python，不依赖大模型）：平台兜底 + 年份补全 + 淘宝 Excel 匹配。

        - 平台：完全信任模型/解析器的判断，不做文件名兜底（文件名用户可控、不可靠；
          视觉模型已按图片特征词判平台，以图为准）。模型未判出 → 空串。
        - 主年份：本批次成功 order_time 中非待补年份的最新值（如拼多多 2026）
        - 年份补全：order_time 形如 0000-MM-DD（模型只读月日/年份异常）→ 用主年份补
        - 淘宝：用商品名在 Excel「商品名称」列匹配，取「订单提交时间」(时间)
          与「实付金额」(价格，覆盖视觉金额)，商品名缺则补 Excel 的
        """
        from datetime import datetime

        # 0. 平台判定：完全信任模型/解析器的判断，不做文件名兜底
        #    文件名是用户可控的（可能改过名、下载名不对），不可靠；
        #    视觉模型已按图片特征词判平台（拼小圈→拼多多、闪购→美团、
        #    天猫/淘宝/企→淘宝，见 _VISION_PROMPT），以图为准。
        #    模型未判出 → 空串（文件名前缀不出现 "None"）。
        #    发票(PDF)用统一字段名，使后续导出/年份补全逻辑通用。
        for p in parsed_list:
            if p.get("kind") == "invoice":
                p["platform"] = p.get("seller") or p.get("category") or "发票"
                p["order_time"] = p.get("date") or ""
                p["product_name"] = p.get("product") or ""
                p["invoice_count"] = 1  # 有发票PDF
            else:
                p["platform"] = p.get("platform") or ""

        # 1. 主年份
        years = []
        for p in parsed_list:
            ot = p.get("order_time") or ""
            if ot.startswith("0000-"):
                continue
            try:
                y = int(ot.split("-")[0])
                if 1990 <= y <= 2100:
                    years.append(y)
            except Exception:
                pass
        if not years and self.hint_year and 1990 <= self.hint_year <= 2100:
            years.append(self.hint_year)   # 本批无年份样本时用外部提示年份
        main_year = max(years) if years else datetime.now().year

        # 2. 年份补全（0000- 待补；异常年也用主年份 + 其月日）
        for p in parsed_list:
            ot = p.get("order_time") or ""
            if not ot:
                continue
            try:
                y, mo, d = ot.split("-")
                yi = int(y)
                if yi == 0 or yi < 1990 or yi > 2100:
                    p["order_time"] = f"{main_year}-{mo}-{d}"
            except Exception:
                pass

        # 3. 淘宝：商品名匹配 Excel；若匹配不到，按价格兜底
        if excel_data is not None:
            from order_excel import match_by_price, match_by_product
            for p in parsed_list:
                if p.get("platform") != "淘宝":
                    continue
                pn = p.get("product_name")
                row, amb = match_by_product(excel_data, pn)
                reason = "商品名"
                if row is None:
                    # 商品名匹配失败（视觉模型提取的商品名可能与 Excel 差异大），按价格兜底
                    row, amb = match_by_price(excel_data, p.get("amount"))
                    reason = "价格"
                if row is not None:
                    p["order_time"] = row["order_time"]   # 时间来自 Excel 订单提交时间
                    if row["price"] is not None:
                        p["amount"] = row["price"]        # 价格用 Excel 实付金额
                    if row["product"]:
                        p["product_name"] = row["product"]  # Excel 商品名更完整，直接覆盖
                    if amb:
                        self.warn.emit(
                            f"淘宝按{reason}在Excel中匹配到多行，已取最相似行（可能不准）"
                        )
                else:
                    self.warn.emit(f"淘宝图片未在Excel中找到对应订单（商品名：{pn}）")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("报销截图智能重命名")
        self.resize(980, 640)
        self.folder_path: str | None = None
        self.files: list = []           # 当前文件夹图片路径列表（供识别/重命名用）
        self.results: list = []         # 与 files 对齐：每行 parsed dict 或 None
        self.last_renames: list = []    # 最近一批重命名 [(old_abs, new_abs)]
        self._worker: "RecognitionWorker | None" = None
        self._last_clicked_row = None  # 表格行 toggle 选中用
        self._inited = False  # 初始化期间禁止自动保存（控件还在建、赋值会触发信号）
        self._build_ui()
        self._apply_config(load_config())
        self._inited = True

    # ---------- 框架 ----------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # 左侧选项卡栏
        left = QFrame()
        left.setFixedWidth(150)
        left.setStyleSheet("background:#2c3e50;")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        self.tab_btns = []
        for label in ("文件处理", "模型选择", "设置"):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet(TAB_STYLE)
            b.clicked.connect(lambda _, x=label: self._switch(x))
            lv.addWidget(b)
            self.tab_btns.append(b)
        lv.addStretch()
        h.addWidget(left)

        # 右侧：顶部步骤条 + 堆叠内容
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        step = QLabel("选文件夹 › 识别 › 预览 › 执行")
        step.setStyleSheet(
            "background:#ecf0f1;padding:10px 16px;color:#2c3e50;"
            "font-size:13px;border-bottom:1px solid #bdc3c7;"
        )
        rv.addWidget(step)

        self.stack = QStackedWidget()
        rv.addWidget(self.stack)

        self.page_files = self._build_files_page()
        self.page_model = self._build_model_page()
        self.page_settings = self._build_settings_page()
        self.stack.addWidget(self.page_files)
        self.stack.addWidget(self.page_model)
        self.stack.addWidget(self.page_settings)

        h.addWidget(right)
        self._switch("文件处理")

    def _switch(self, name: str):
        idx = {"文件处理": 0, "模型选择": 1, "设置": 2}[name]
        self.stack.setCurrentIndex(idx)
        for b in self.tab_btns:
            b.setChecked(b.text() == name)

    # ========== 文件处理页 ==========
    def _build_files_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # KPI 卡片
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self.kpi_scanned = self._kpi_card("0", "已扫描")
        self.kpi_recognized = self._kpi_card("0", "已识别")
        self.kpi_failed = self._kpi_card("0", "失败")
        kpi_row.addWidget(self.kpi_scanned)
        kpi_row.addWidget(self.kpi_recognized)
        kpi_row.addWidget(self.kpi_failed)
        kpi_row.addStretch()
        v.addLayout(kpi_row)

        # 拖入区
        drop = QLabel("拖入文件夹 或 点击「选择文件夹」")
        drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop.setStyleSheet(
            "border:2px dashed #bdc3c7;border-radius:6px;padding:22px;"
            "color:#7f8c8d;font-size:14px;background:#fafafa;"
        )
        v.addWidget(drop)

        # 时间前缀勾选（识别后文件名带时间前缀）
        cfg_row = QHBoxLayout()
        self.set_prefix_date = QCheckBox("识别后将时间填入文件名前缀（时间+平台+价格）")
        cfg_row.addWidget(self.set_prefix_date)
        cfg_row.addStretch()
        v.addLayout(cfg_row)

        # 按钮行
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_pick = QPushButton("选择文件夹")
        self.btn_pick.setStyleSheet(PRIMARY_BTN)
        self.btn_pick.clicked.connect(self.pick_folder)
        self.btn_recognize = QPushButton("开始识别")
        self.btn_recognize.setStyleSheet(SECONDARY_BTN)
        self.btn_recognize.clicked.connect(self.start_recognition)
        self.btn_rerun_sel = QPushButton("重新识别选中")
        self.btn_rerun_sel.setStyleSheet(SECONDARY_BTN)
        self.btn_rerun_sel.clicked.connect(self.rerun_selected)
        self.btn_rename = QPushButton("执行重命名")
        self.btn_rename.setStyleSheet(SECONDARY_BTN)
        self.btn_rename.clicked.connect(self.execute_rename)
        self.btn_export = QPushButton("导出订单Excel")
        self.btn_export.setStyleSheet(SECONDARY_BTN)
        self.btn_export.clicked.connect(self.export_orders)
        self.btn_export_reimb = QPushButton("导出报销导入Excel")
        self.btn_export_reimb.setStyleSheet(SECONDARY_BTN)
        self.btn_export_reimb.clicked.connect(self.export_reimbursement)
        self.btn_undo = QPushButton("撤销")
        self.btn_undo.setStyleSheet(SECONDARY_BTN)
        self.btn_undo.clicked.connect(self.undo_rename)
        bar.addWidget(self.btn_pick)
        bar.addWidget(self.btn_recognize)
        bar.addWidget(self.btn_rerun_sel)
        bar.addWidget(self.btn_rename)
        bar.addWidget(self.btn_undo)
        bar.addStretch()
        v.addLayout(bar)

        # M5 进度条：识别时显示，平时隐藏
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        v.addWidget(self.progress_bar)

        # 结果表（8 列）：末列为逐行「重新识别」按钮
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["原文件名", "金额", "类型", "日期", "商品名称", "新文件名", "状态", "操作"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        # 支持 Ctrl/Shift 多选整行 → 批量重识别
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        # 单击已选中的行 → toggle 取消选中（无 modifier 时连续点击同一行）
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        v.addWidget(self.table, 1)

        # 底部：导出按钮 + 状态
        foot = QHBoxLayout()
        foot.addWidget(self.btn_export)
        foot.addWidget(self.btn_export_reimb)
        foot.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#7f8c8d;font-size:12px;")
        foot.addWidget(self.status_label)
        v.addLayout(foot)
        return w

    def _kpi_card(self, num: str, label: str) -> QFrame:
        f = QFrame()
        f.setObjectName("kpi")
        f.setStyleSheet(KPI_STYLE)
        fl = QVBoxLayout(f)
        fl.setContentsMargins(0, 0, 0, 0)
        n = QLabel(num)
        n.setObjectName("kpiNum")
        l = QLabel(label)
        l.setObjectName("kpiLabel")
        fl.addWidget(n)
        fl.addWidget(l)
        return f

    def pick_folder(self):
        # 起始目录定位到上次使用的文件夹，避免每次回到项目根
        start = self.folder_path or load_config().get("last_folder") or ""
        d = QFileDialog.getExistingDirectory(self, "选择文件夹", start)
        if d:
            self.load_folder(d)
            self._auto_save("已记住此文件夹并保存")

    def load_folder(self, path: str) -> int:
        """扫描文件夹内图片(jpg/jpeg/png)，填充表格。返回图片数。"""
        self.folder_path = path
        p = Path(path)
        files = sorted(f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_EXTS)
        self.files = files
        self.results = [None] * len(files)
        self.last_renames = []
        # 自动检测同目录下的"订单数据"Excel（截图与Excel同文件夹）
        self.st_tb_excel_path.setText(self._find_order_excel(path))
        self.kpi_scanned.findChild(QLabel, "kpiNum").setText(str(len(files)))
        self.table.setRowCount(len(files))
        for i, f in enumerate(files):
            self.table.setItem(i, COL_ORIG, QTableWidgetItem(f.name))
            self.table.setItem(i, COL_AMOUNT, QTableWidgetItem(""))
            self.table.setItem(i, COL_TYPE, QTableWidgetItem(""))
            self.table.setItem(i, COL_DATE, QTableWidgetItem(""))
            self.table.setItem(i, COL_PRODUCT_NAME, QTableWidgetItem(""))
            self.table.setItem(i, COL_NEWNAME, QTableWidgetItem(""))
            self.table.setItem(i, COL_STATUS, QTableWidgetItem("待识别"))
            self._add_rerun_button(i)
        self._persist_last_folder(path)
        return len(files)

    def _persist_last_folder(self, path: str):
        """选文件夹后自动记录，下次启动恢复（无需点保存配置）。"""
        try:
            cfg = load_config()
            cfg["last_folder"] = path
            save_config(cfg)
        except Exception:  # noqa: BLE001
            pass  # 配置读写失败不应阻断正常流程

    def browse_taobao_excel(self):
        start = (str(Path(self.st_tb_excel_path.text()).parent)
                 if self.st_tb_excel_path.text() else "")
        d, _ = QFileDialog.getOpenFileName(
            self, "选择淘宝订单Excel", start, "Excel 文件 (*.xlsx *.xls)",
        )
        if d:
            self.st_tb_excel_path.setText(d)

    def _find_order_excel(self, folder: str) -> str:
        """在文件夹内自动定位“订单数据”Excel（截图与Excel同目录时）。

        优先文件名含“订单数据”的 xlsx/xls；否则若仅有一个 Excel 也采用；
        多个且无“订单数据”命名则不自动选（交由用户手动浏览）。无则返回空串。
        """
        p = Path(folder)
        if not p.is_dir():
            return ""
        excels = [f for f in p.iterdir()
                  if f.is_file() and f.suffix.lower() in (".xlsx", ".xls")]
        if not excels:
            return ""
        named = [f for f in excels if "订单数据" in f.name]
        if named:
            return str(named[0])
        if len(excels) == 1:
            return str(excels[0])
        return ""

    def export_orders(self):
        """把识别结果（时间/商品名/价格/平台/原文件名）写出为订单汇总 Excel。"""
        if not self.results or all(r is None for r in self.results):
            self.status_label.setText("请先点击「开始识别」")
            return
        try:
            from order_excel import build_order_rows, write_order_excel
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"❌ 导出失败：缺少依赖 openpyxl（{e}）")
            QMessageBox.critical(self, "导出订单Excel", f"缺少依赖 openpyxl：{e}")
            return
        rows = build_order_rows(self.results, self.files)
        if not rows:
            self.status_label.setText("没有可导出的识别结果")
            return
        # 弹出保存对话框，默认到当前文件夹
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_dir = self.folder_path or str(DEFAULT_LOG_DIR)
        default_name = f"订单汇总_{ts}.xlsx"
        out, _ = QFileDialog.getSaveFileName(
            self, "导出订单Excel",
            str(Path(default_dir) / default_name),
            "Excel 文件 (*.xlsx *.xls)",
        )
        if not out:
            return
        try:
            write_order_excel(rows, out)
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"❌ 写入Excel失败: {e}")
            QMessageBox.critical(self, "导出订单Excel", f"写入失败：{e}")
            return
        msg = f"已导出订单Excel：{len(rows)} 行 → {out}"
        self.status_label.setText(msg)
        QMessageBox.information(self, "导出订单Excel", msg)

    def export_reimbursement(self):
        """把识别结果生成为报销系统「消费明细导入」Excel。"""
        if not self.results or all(r is None for r in self.results):
            self.status_label.setText("请先点击「开始识别」")
            return
        try:
            from reimbursement_excel import (
                build_reimbursement_rows, write_reimbursement_excel,
            )
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"❌ 导出失败：缺少依赖 openpyxl（{e}）")
            QMessageBox.critical(self, "导出报销导入Excel", f"缺少依赖 openpyxl：{e}")
            return

        dlg = ReimbDialog(self, default_folder=self.folder_path)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        expense_type = dlg.expense_type.text().strip() or "买耗材"
        template_path = dlg.template_path.text().strip()
        out_path = dlg.out_path.text().strip()

        if not template_path or not Path(template_path).exists():
            QMessageBox.warning(self, "导出报销导入Excel", "请先选择有效的导入模板文件")
            return

        rows = build_reimbursement_rows(self.results, self.files)
        if not rows:
            self.status_label.setText("没有可导出的识别结果（需成功且有金额+日期）")
            QMessageBox.information(
                self, "导出报销导入Excel",
                "没有可导出的条目：需识别成功且同时具备金额与消费日期。",
            )
            return

        if not out_path:
            # 未指定时默认保存到当前处理的文件夹
            base_dir = Path(self.folder_path) if self.folder_path else (
                Path(self.set_logpath.text().strip() or str(DEFAULT_LOG_DIR))
            )
            base_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = str(base_dir / f"报销导入_{expense_type}_{ts}.xlsx")
        else:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            write_reimbursement_excel(template_path, expense_type, rows, out_path)
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"❌ 写入Excel失败: {e}")
            QMessageBox.critical(self, "导出报销导入Excel", f"写入失败：{e}")
            return

        self._persist_reimb_config(template_path, expense_type)
        msg = f"已导出报销导入Excel：{len(rows)} 行 → {out_path}"
        self.status_label.setText(msg)
        QMessageBox.information(self, "导出报销导入Excel", msg)

    def _persist_reimb_config(self, template_path: str, expense_type: str):
        """记住导入模板路径与费用类型，下次对话框免重复选择。"""
        try:
            cfg = load_config()
            cfg.setdefault("settings", {})
            cfg["settings"]["reimbursement_template"] = template_path
            cfg["settings"]["reimbursement_expense_type"] = expense_type
            save_config(cfg)
        except Exception:  # noqa: BLE001
            pass

    def _stub(self, action: str):
        # 功能占位：分页逻辑留待后续里程碑
        self.status_label.setText(f"「{action}」功能待接入")

    # ---------- 逐行重识别（单个/批量）----------
    def _add_rerun_button(self, row: int):
        """在某行的操作列放置「重新识别」按钮（行不重排，捕获行号安全）。"""
        btn = QPushButton("重新识别")
        btn.setStyleSheet(SECONDARY_BTN)
        btn.clicked.connect(lambda _=False, r=row: self.rerun_single(r))
        self.table.setCellWidget(row, COL_RERUN, btn)

    def _iter_rerun_buttons(self):
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, COL_RERUN)
            if w is not None:
                yield w

    def _set_busy(self, busy: bool):
        """识别/重识别进行中：禁用会触发并发的按钮，防止结果错乱。"""
        for b in (self.btn_recognize, self.btn_rerun_sel, self.btn_rename,
                  self.btn_pick, self.btn_undo):
            b.setEnabled(not busy)
        for b in self._iter_rerun_buttons():
            b.setEnabled(not busy)

    def _busy(self) -> bool:
        """是否有识别/重识别 worker 在跑。"""
        return (getattr(self, "_worker", None) is not None
                and self._worker.isRunning()) or (
                getattr(self, "_rerun_worker", None) is not None
                and self._rerun_worker.isRunning())

    def _hint_year(self) -> int | None:
        """从已识别成功行取最新年份，供重识别的年份补全保持一致。"""
        years = []
        for r in (self.results or []):
            ot = (r or {}).get("order_time") or ""
            if ot.startswith("0000-"):
                continue
            try:
                y = int(ot.split("-")[0])
                if 1990 <= y <= 2100:
                    years.append(y)
            except Exception:
                pass
        return max(years) if years else None

    def _build_recognition_params(self) -> dict:
        """收集识别所需参数（全量与逐行重识别共用）。"""
        mode = self._current_mode()
        llm_kind, llm_cfg = self._build_llm_config()
        _, vision_cfg = self._build_vision_config()
        tb_path = self.st_tb_excel_path.text().strip()
        if tb_path and not Path(tb_path).exists():
            self.status_label.setText("⚠️ 淘宝订单Excel路径不存在，已忽略")
            tb_path = ""
        return {
            "template": self.set_template.text(),
            "threshold": self.set_threshold.value(),
            "llm_kind": llm_kind, "llm_cfg": llm_cfg,
            "mode": mode, "vision_cfg": vision_cfg,
            "taobao_excel": tb_path or None,
            "prefix_date": self.set_prefix_date.isChecked(),
            "invoice_category": self.set_invoice_category.text(),
        }

    def rerun_single(self, row: int):
        """单行重识别 → 成功即立即重命名该文件。"""
        self._start_rerun([row])

    def rerun_selected(self):
        """批量重识别选中的多行。"""
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        if not rows:
            self.status_label.setText("请先选中一行或多行再点「重新识别选中」")
            return
        self._start_rerun(rows)

    def _on_table_cell_clicked(self, row: int, col: int):
        """单击已选中的行 → toggle 取消选中（无 modifier 时连续点击同一行）。"""
        modifiers = QApplication.keyboardModifiers()
        if modifiers == Qt.KeyboardModifier.NoModifier:
            if self._last_clicked_row == row:
                self.table.clearSelection()
                self._last_clicked_row = None
            else:
                self._last_clicked_row = row

    def _start_rerun(self, rows: list):
        if not self.folder_path or not self.files:
            self.status_label.setText("请先选择文件夹")
            return
        if self._busy():
            self.status_label.setText("识别进行中，请稍候再重识别")
            return
        rows = [r for r in rows if 0 <= r < len(self.files)]
        if not rows:
            return

        params = self._build_recognition_params()
        sub_files = [self.files[r] for r in rows]
        self._rerun_rowmap = {i: r for i, r in enumerate(rows)}  # 子集idx → 全局行
        # 记录旧状态，完成后重算 KPI（比增量更稳）
        for r in rows:
            self.table.setItem(r, COL_STATUS, QTableWidgetItem("重识别中…"))

        self._set_busy(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(sub_files))
        self.progress_bar.setValue(0)
        self.status_label.setText(f"重识别中 0/{len(sub_files)}")

        self._rerun_worker = RecognitionWorker(
            sub_files, params["template"], params["threshold"],
            llm_kind=params["llm_kind"], llm_cfg=params["llm_cfg"],
            mode=params["mode"], vision_cfg=params["vision_cfg"],
            taobao_excel=params["taobao_excel"],
            prefix_date=params["prefix_date"],
            invoice_category=params["invoice_category"],
            hint_year=self._hint_year(),
        )
        self._rerun_worker.row_done.connect(self._on_rerun_row_done)
        self._rerun_worker.progress.connect(self._on_progress_rerun)
        self._rerun_worker.warn.connect(self._on_warn)
        self._rerun_worker.finished.connect(self._on_rerun_finished)
        self._rerun_worker.start()

    def _on_rerun_row_done(self, sub_idx, parsed, new_name):
        """子集行完成 → 映射回全局行号，回填该行结果。"""
        row = self._rerun_rowmap.get(sub_idx)
        if row is None:
            return
        self._on_row_done(row, parsed, new_name)   # 复用回填逻辑（含状态色标）

    def _on_progress_rerun(self, cur, total):
        if total > 0:
            self.progress_bar.setValue(cur)
        self.status_label.setText(f"重识别中 {cur}/{total}")

    def _on_rerun_finished(self, recognized, failed):
        """重识别结束 → 逐行立即重命名成功项 + 重算 KPI + 写日志。"""
        rows = sorted(self._rerun_rowmap.values())
        folder = Path(self.folder_path)
        strategy = self.set_conflict.currentText()
        entries = []
        renamed = []
        done = 0
        for r in rows:
            entry = self._rename_one(r, folder, strategy)
            entry["rerun"] = True
            entries.append(entry)
            if entry.get("new_name"):
                # _rename_one 已同步 self.files[r]；记录用于撤销（仅实际改名项有）
                old = entry.get("_old_abs")
                new = entry.get("_new_abs")
                if old and new:
                    renamed.append((old, new))
                done += 1
            entry.pop("_old_abs", None)
            entry.pop("_new_abs", None)

        self._refresh_kpi_counts()
        self._write_log(folder, entries)
        self._write_ocr_log(self.results)   # 落盘本次 OCR 原文，供后台验证
        # 重识别的改名并入撤销栈（追加，不清空整批的）
        self.last_renames = list(getattr(self, "last_renames", [])) + renamed
        self.progress_bar.setValue(self.progress_bar.maximum())
        self._set_busy(False)
        self.status_label.setText(
            f"重识别完成：成功 {recognized}，失败 {failed}；已重命名 {done} 个，日志已生成"
        )

    def _rename_one(self, i: int, folder: Path, strategy: str) -> dict:
        """按当前设置为单行生成新文件名并立即改名，返回日志 entry。

        与 execute_rename 单行逻辑一致；金额+平台齐全但缺商品名的失败项
        仍改名，状态保持失败（_old_abs/_new_abs 供调用方入撤销栈，用后 pop）。
        """
        parsed = self.results[i]
        f = self.files[i]

        # 金额+平台齐全即生成新文件名（含失败项：缺商品名但仍可改名）
        new_name = self._make_new_name(i, parsed)
        if new_name:
            self.table.setItem(i, COL_NEWNAME, QTableWidgetItem(new_name))

        entry = self._build_log_entry(i, f, parsed, new_name)
        if not parsed or not new_name:
            entry["status"] = "failed"
            entry["new_name"] = None
            entry["reason"] = (parsed or {}).get("reason", "未生成新文件名")
            return entry

        # 金额+平台齐全但状态失败（如缺商品名）→ 仍重命名，状态保持失败
        keep_failed = parsed.get("status") != "success"
        if keep_failed and not self._renamable(parsed):
            entry["status"] = "failed"
            entry["new_name"] = None
            entry["reason"] = (parsed or {}).get("reason", "识别失败")
            return entry

        target, skip_flag = self._resolve_target(folder, new_name, strategy, f)
        if skip_flag:
            entry["status"] = "skipped"
            entry["new_name"] = None
            entry["reason"] = "目标文件已存在，按策略跳过"
            self.table.setItem(i, COL_STATUS, QTableWidgetItem("目标已存在·跳过"))
            return entry

        try:
            src = Path(f)
            is_noop = src.resolve() == target.resolve()
            if not is_noop:
                src.replace(target)
                self.files[i] = target        # 同步路径，供二次重识别复用
                entry["_old_abs"] = str(src)
                entry["_new_abs"] = str(target)
            self.table.setItem(i, COL_ORIG, QTableWidgetItem(target.name))
            if keep_failed:
                # 金额+平台齐全但缺商品名：文件仍改名，状态保持失败
                self.table.setItem(i, COL_STATUS, QTableWidgetItem("失败·已重命名"))
                entry["status"] = "failed"
                entry["new_name"] = target.name
                entry["reason"] = (parsed or {}).get("reason", "缺少商品名称")
            else:
                self.table.setItem(i, COL_STATUS, QTableWidgetItem("已重命名"))
                entry["status"] = "success"
                entry["new_name"] = target.name
                if is_noop:
                    entry["reason"] = "已符合命名规则，无需改名"
        except Exception as e:  # noqa: BLE001
            entry["status"] = "failed"
            entry["new_name"] = None
            entry["reason"] = f"重命名失败: {e}"
        return entry

    def _refresh_kpi_counts(self):
        """按 self.results 重算「已识别 / 失败」KPI（重识别后同步顶部计数）。"""
        recognized = sum(
            1 for r in self.results if r and r.get("status") == "success"
        )
        failed = sum(
            1 for r in self.results if r and r.get("status") != "success"
        )
        self.kpi_recognized.findChild(QLabel, "kpiNum").setText(str(recognized))
        self.kpi_failed.findChild(QLabel, "kpiNum").setText(str(failed))

    # ---------- M3/M5：识别 ----------
    def start_recognition(self):
        if not self.files:
            self.status_label.setText("请先选择文件夹")
            return
        if self._busy():
            self.status_label.setText("识别进行中，请稍候")
            return
        self._set_busy(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(self.files))
        self.progress_bar.setValue(0)
        self.status_label.setText(f"识别中 0/{len(self.files)}")

        params = self._build_recognition_params()
        self._worker = RecognitionWorker(
            self.files, params["template"], params["threshold"],
            llm_kind=params["llm_kind"], llm_cfg=params["llm_cfg"],
            mode=params["mode"], vision_cfg=params["vision_cfg"],
            taobao_excel=params["taobao_excel"],
            prefix_date=params["prefix_date"],
            invoice_category=params["invoice_category"],
        )
        self._worker.row_done.connect(self._on_row_done)
        self._worker.progress.connect(self._on_progress)
        self._worker.warn.connect(self._on_warn)
        self._worker.finished.connect(self._on_rec_finished)
        self._worker.start()

    def _on_warn(self, msg: str):
        # 非致命提示：追加到状态栏（识别中也可能触发，用 ⚠️ 区分）
        cur = self.status_label.text()
        self.status_label.setText(f"⚠️ {msg}")

    def _on_row_done(self, row, parsed, new_name):
        self.results[row] = parsed
        amount = parsed.get("amount")
        status = parsed["status"]
        used_llm = parsed.get("used_llm", False)

        # 图片(订单)与发票(PDF)共用列：类型/日期/商品名称 语义不同
        is_invoice = parsed.get("kind") == "invoice"
        if is_invoice:
            type_text = parsed.get("category") or "发票"
            date_text = parsed.get("date") or ""
            product_text = parsed.get("product") or ""  # 发票取「项目名称」
        else:
            type_text = parsed.get("platform") or ""
            date_text = parsed.get("order_time") or ""
            product_text = parsed.get("product_name") or ""  # 图片取商品名

        self.table.setItem(
            row, COL_AMOUNT,
            QTableWidgetItem(f"¥{amount}" if amount is not None else "")
        )
        self.table.setItem(row, COL_TYPE, QTableWidgetItem(type_text))
        self.table.setItem(row, COL_DATE, QTableWidgetItem(date_text))
        self.table.setItem(row, COL_PRODUCT_NAME, QTableWidgetItem(product_text))
        self.table.setItem(row, COL_NEWNAME, QTableWidgetItem(new_name))

        if status == "success":
            conf = parsed.get("confidence", 1.0)
            tag = "✅成功"
            item_color = None
            if conf < LOW_CONF_WARN:
                tag = f"⚠️成功·低置信{conf:.2f}"
                item_color = QColor("#e67e22")  # 橙：提示人工核对
            if used_llm:
                tag += "·LLM"
        else:
            tag = "❌失败"
            item_color = QColor("#e74c3c")  # 红：失败
        cell = QTableWidgetItem(tag)
        if item_color is not None:
            cell.setForeground(QBrush(item_color))
            cell.setToolTip(
                parsed.get("reason", "") or f"置信度 {parsed.get('confidence', 0):.2f}"
            )
        self.table.setItem(row, COL_STATUS, cell)

    def _on_progress(self, cur, total):
        if total > 0:
            self.progress_bar.setValue(cur)
        self.status_label.setText(f"识别中 {cur}/{total}")

    def _on_rec_finished(self, recognized, failed):
        self._set_busy(False)
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.kpi_recognized.findChild(QLabel, "kpiNum").setText(str(recognized))
        self.kpi_failed.findChild(QLabel, "kpiNum").setText(str(failed))
        self.status_label.setText(f"识别完成：成功 {recognized}，失败 {failed}")
        self._write_ocr_log(self.results)   # 落盘 OCR 原文，供后台验证
        if self.set_autorename.isChecked() and recognized > 0:
            self.execute_rename()

    # ---------- M4：重命名 / 日志 / 撤销 ----------
    def _renamable(self, parsed) -> bool:
        """失败项仍可重命名：金额 + 平台齐全（即能生成有效新文件名）。"""
        return bool(
            parsed
            and parsed.get("amount") is not None
            and parsed.get("platform")
        )

    def _make_new_name(self, i: int, parsed) -> str:
        """按当前设置生成新文件名；金额+平台齐全即生成（含失败项，供仍重命名）。

        返回 '' 表示无法生成（金额/平台缺失或发票模板缺字段）。
        """
        if not self._renamable(parsed):
            return ""
        from invoice_parser import render_invoice_name
        from recognizer import render_template
        ext = Path(self.files[i]).suffix.lower()
        if parsed.get("kind") == "invoice":
            return render_invoice_name(parsed, self.set_invoice_category.text())
        if self.set_prefix_date.isChecked() and parsed.get("order_time"):
            return f"{parsed['order_time']}{parsed.get('platform', '')}{parsed.get('amount', '')}{ext}"
        return render_template(
            self.set_template.text(), parsed.get("amount"), parsed.get("platform", ""), ext
        )

    def execute_rename(self):
        # 函数内导入，避免模块级触发重型模块加载
        from invoice_parser import render_invoice_name
        from recognizer import render_template

        if not self.folder_path:
            self.status_label.setText("请先选择文件夹")
            return
        if not self.results or all(r is None for r in self.results):
            self.status_label.setText("请先点击「开始识别」")
            return

        # 按当前「时间前缀」勾选状态重新生成新文件名（复选框即实时开关）：
        # 勾选 → 时间+平台+价格；未勾选 → 模板格式（不含时间前缀）。
        # 支持：先识别（不勾选）→ 看结果 → 再勾选/取消 → 点执行重命名即生效。
        for i, parsed in enumerate(self.results):
            # 金额+平台齐全即生成新文件名（含失败项：缺商品名但仍可改名）
            new_name = self._make_new_name(i, parsed)
            if new_name:
                self.table.setItem(i, COL_NEWNAME, QTableWidgetItem(new_name))

        fail_handling = self.set_fail.currentText()
        # 金额+平台齐全的失败项（如缺商品名）仍可改名，不算阻断性失败
        has_fail = any(
            r and r["status"] != "success" and not self._renamable(r)
            for r in self.results if r
        )
        if fail_handling.startswith("中止") and has_fail:
            self.status_label.setText("存在识别失败项，已按设置中止重命名")
            return

        strategy = self.set_conflict.currentText()
        folder = Path(self.folder_path)
        entries = []
        renamed = []
        done = skipped = renamed_fail = 0

        for i, f in enumerate(self.files):
            parsed = self.results[i]
            new_name = self.table.item(i, COL_NEWNAME).text().strip()
            entry = self._build_log_entry(i, f, parsed, new_name)

            if not parsed or not new_name:
                entry["status"] = "failed"
                entry["new_name"] = None
                entry["reason"] = (parsed or {}).get(
                    "reason", "未生成新文件名"
                )
                entries.append(entry)
                skipped += 1
                continue
            # 金额+平台齐全但状态失败（如缺商品名）→ 仍重命名，状态保持失败
            keep_failed = parsed["status"] != "success"
            if keep_failed and not self._renamable(parsed):
                entry["status"] = "failed"
                entry["new_name"] = None
                entry["reason"] = (parsed or {}).get("reason", "识别失败")
                entries.append(entry)
                skipped += 1
                continue

            target, skip_flag = self._resolve_target(folder, new_name, strategy, f)
            if skip_flag:  # 策略=跳过 且目标已存在
                entry["status"] = "skipped"
                entry["new_name"] = None
                entry["reason"] = "目标文件已存在，按策略跳过"
                entries.append(entry)
                skipped += 1
                continue

            try:
                src = Path(f)
                is_noop = src.resolve() == target.resolve()
                if not is_noop:
                    src.replace(target)
                    renamed.append((str(src), str(target)))
                    self.files[i] = target  # 同步路径，供二次重命名/识别复用
                self.table.setItem(i, 0, QTableWidgetItem(target.name))
                if keep_failed:
                    # 金额+平台齐全但缺商品名：文件仍改名，状态保持失败
                    self.table.setItem(i, COL_STATUS, QTableWidgetItem("失败·已重命名"))
                    entry["status"] = "failed"
                    entry["new_name"] = target.name
                    entry["reason"] = (parsed or {}).get("reason", "缺少商品名称")
                    renamed_fail += 1
                else:
                    self.table.setItem(i, COL_STATUS, QTableWidgetItem("已重命名"))
                    entry["status"] = "success"
                    entry["new_name"] = target.name
                    if is_noop:
                        entry["reason"] = "已符合命名规则，无需改名"
                    done += 1
            except Exception as e:  # noqa: BLE001
                entry["status"] = "failed"
                entry["new_name"] = None
                entry["reason"] = f"重命名失败: {e}"
                skipped += 1
            entries.append(entry)

        self._write_log(folder, entries)
        self.last_renames = renamed
        self.status_label.setText(
            f"重命名完成：成功 {done}，失败仍改名 {renamed_fail}，跳过 {skipped}，日志已生成"
        )

    def _resolve_target(self, folder: Path, new_name: str, strategy: str, src=None):
        """返回 (目标路径, 是否跳过)。冲突按策略处理。

        src：当前待改名文件。若目标解析后与自身同路径，视为"已符合命名"，
        非冲突，直接返回（避免把已正确命名的文件误加 (1)）。
        """
        base = folder / new_name
        if not base.exists():
            return base, False
        if src is not None:
            try:
                if base.resolve() == Path(src).resolve():
                    return base, False
            except OSError:
                pass
        if strategy.startswith("覆盖"):
            return base, False
        if strategy.startswith("跳过"):
            return base, True
        # 追加序号：26.52淘宝 (1).jpg
        stem, ext = Path(new_name).stem, Path(new_name).suffix
        n = 1
        while True:
            cand = folder / f"{stem} ({n}){ext}"
            if not cand.exists():
                return cand, False
            n += 1

    def _build_log_entry(self, i, f, parsed, new_name):
        p = parsed or {}
        ok = bool(parsed and parsed["status"] == "success")
        entry = {
            "original_name": Path(f).name,
            "new_name": new_name or None,
            "status": "success" if ok else "failed",
            "amount": p.get("amount"),
            "platform": p.get("platform"),
            "confidence": p.get("confidence"),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        if not ok:
            entry["reason"] = p.get("reason", "识别失败")
        if p.get("used_llm"):
            entry["used_llm"] = True
        return entry

    def _write_log(self, folder: Path, entries: list):
        # 日志写入独立目录，避免污染上传文件夹（默认项目根 /logs）
        log_dir = Path(self.set_logpath.text().strip() or str(DEFAULT_LOG_DIR))
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"rename_log_{ts}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        self._last_log_path = str(path)

    def _write_ocr_log(self, results):
        """识别结束时落盘 OCR 原文（与重命名解耦），供后台验证正则命中。

        不依赖是否执行重命名：识别完即写，关软件也不丢。
        """
        log_dir = Path(self.set_logpath.text().strip() or str(DEFAULT_LOG_DIR))
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"ocr_log_{ts}.json"
        entries = []
        for i, parsed in enumerate(results):
            if not parsed:
                continue
            f = self.files[i] if i < len(self.files) else None
            entries.append({
                "file": Path(f).name if f else None,
                "platform": parsed.get("platform"),
                "amount": parsed.get("amount"),
                "product_name": parsed.get("product_name"),
                "status": parsed.get("status"),
                "reason": parsed.get("reason", ""),
                "used_model": bool(parsed.get("used_model")),
                "ocr_text": parsed.get("ocr_text", ""),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            })
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        self._last_ocr_log_path = str(path)

    def undo_rename(self):
        if not getattr(self, "last_renames", []):
            self.status_label.setText("没有可撤销的操作")
            return
        restored = 0
        for old, new in reversed(self.last_renames):
            new_p, old_p = Path(new), Path(old)
            if new_p.exists() and not old_p.exists():
                new_p.replace(old_p)
                restored += 1
        self.last_renames = []
        self.status_label.setText(f"已撤销：恢复 {restored} 个文件原名")
        if self.folder_path:
            self.load_folder(self.folder_path)

    # ========== 模型选择页（三方法：OCR+正则 / 文字模型 / 视觉模型）==========
    def _build_model_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        title = QLabel("识别方法")
        title.setStyleSheet("font-size:14px;font-weight:bold;color:#2c3e50;")
        v.addWidget(title)

        hint = QLabel(
            "三种方法均先跑 OCR+正则前置，仅难图（金额/平台缺失、"
            "或拼多多/美团商品名为空）才调模型兜底，兼顾速度与准确率。"
        )
        hint.setStyleSheet("font-size:12px;color:#7f8c8d;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.model_group = QButtonGroup(self)
        self.model_group.setExclusive(True)
        self._model_cards = []

        c1 = self._model_card(
            "OCR+正则",
            "最快：仅 RapidOCR + 正则抽金额/平台/时间，不加载任何模型。"
            "难图商品名可能留空（淘宝由订单 Excel 补全）。",
            self._ocr_regex_form(),
            method="OCR+正则",
        )
        v.addWidget(c1["frame"])
        self._model_cards.append(c1)

        c2 = self._model_card(
            "文字模型",
            "难图兜底：把 OCR 文本喂给文本大模型（本地/云端）。"
            "成本低，但看不到原图版面。",
            self._text_model_form(),
            method="文字模型",
        )
        v.addWidget(c2["frame"])
        self._model_cards.append(c2)

        c3 = self._model_card(
            "视觉模型",
            "难图兜底：把原图直送多模态视觉大模型（本地/云端）。"
            "质量最高，较慢、较吃资源。",
            self._vision_model_form(),
            method="视觉模型",
        )
        v.addWidget(c3["frame"])
        self._model_cards.append(c3)

        # 自动保存反馈
        self.model_status = QLabel("")
        self.model_status.setStyleSheet("color:#1abc9c;font-size:12px;")
        v.addWidget(self.model_status)
        v.addStretch()
        scroll.setWidget(body)

        c1["radio"].setChecked(True)
        self._on_model_selected(c1["radio"])
        return scroll

    def _model_card(self, title: str, desc: str, body: QWidget,
                    method: str | None = None) -> dict:
        frame = QFrame()
        frame.setObjectName("modelCard")
        frame.setStyleSheet(CARD_STYLE)
        fl = QVBoxLayout(frame)
        fl.setSpacing(8)

        radio = QRadioButton(title)
        radio.setStyleSheet("font-size:14px;font-weight:bold;color:#2c3e50;")
        desc_lbl = QLabel(desc)
        desc_lbl.setStyleSheet("font-size:12px;color:#7f8c8d;")
        desc_lbl.setWordWrap(True)
        fl.addWidget(radio)
        fl.addWidget(desc_lbl)
        fl.addWidget(body)

        radio.toggled.connect(lambda checked: self._on_model_selected(radio))
        self.model_group.addButton(radio)
        # method 为稳定标识（路由/配置用），与显示标题解耦
        return {"frame": frame, "radio": radio, "body": body,
                "method": method or title}

    def _on_model_selected(self, radio: QRadioButton):
        for c in self._model_cards:
            selected = c["radio"] is radio and radio.isChecked()
            c["frame"].setObjectName(
                "modelCardSelected" if selected else "modelCard"
            )
            c["frame"].setStyleSheet(CARD_STYLE)
            c["body"].setVisible(selected)
        if radio.isChecked():
            self._auto_save("识别方法已切换并保存")

    def _field(self, layout: QFormLayout, label: str, widget: QWidget):
        layout.addRow(label, widget)
        return widget

    def _combo(self, items) -> QComboBox:
        cb = QComboBox()
        cb.addItems(items)
        return cb

    # ---- 方法一：OCR+正则（无模型）----
    def _ocr_regex_form(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ocr_toggle = QCheckBox("启用 RapidOCR")
        self.ocr_toggle.setChecked(True)
        f.addRow("RapidOCR：", self.ocr_toggle)
        self.ocr_toggle.toggled.connect(
            lambda _=None: self._auto_save("OCR 设置已保存")
        )
        return w

    # ---- 方法二：文字模型（本地/云端 兜底）----
    def _text_model_form(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.text_backend = self._combo(["本地", "云端"])
        self._field(f, "后端：", self.text_backend)

        # 本地子表单
        self.ocr_local = QWidget()
        lf = QFormLayout(self.ocr_local)
        lf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ocr_local_endpoint = QLineEdit("http://localhost:1234/v1")
        self._field(lf, "端点：", self.ocr_local_endpoint)
        self.ocr_local_model = QLineEdit()
        self._field(lf, "模型名：", self.ocr_local_model)

        # 云端子表单
        self.ocr_cloud = QWidget()
        cf = QFormLayout(self.ocr_cloud)
        cf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ocr_cloud_key = QLineEdit()
        self.ocr_cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._field(cf, "API Key：", self.ocr_cloud_key)
        self.ocr_cloud_endpoint = QLineEdit()
        self._field(cf, "端点：", self.ocr_cloud_endpoint)
        self.ocr_cloud_model = QLineEdit()
        self._field(cf, "模型名：", self.ocr_cloud_model)

        f.addRow(self.ocr_local)
        f.addRow(self.ocr_cloud)

        self.btn_test = QPushButton("测试连接")
        self.btn_test.setStyleSheet(SECONDARY_BTN)
        self.btn_test.clicked.connect(self.test_connection)
        f.addRow(self.btn_test)

        self.ocr_prompt = QPlainTextEdit(_OCR_PROMPT)
        self.ocr_prompt.setMaximumHeight(90)
        f.addRow("模型提示词：", self.ocr_prompt)

        self.text_backend.currentTextChanged.connect(self._on_text_backend)
        for le in (
            self.ocr_local_endpoint, self.ocr_local_model,
            self.ocr_cloud_key, self.ocr_cloud_endpoint, self.ocr_cloud_model,
        ):
            le.editingFinished.connect(
                lambda: self._auto_save("文字模型配置已保存")
            )
        self.ocr_prompt.textChanged.connect(
            lambda: self._auto_save("LLM 提示词已保存")
        )
        self._on_text_backend(self.text_backend.currentText())
        return w

    def _on_text_backend(self, text: str):
        """文字模型 本地/云端 切换：显示对应子表单并自动保存。"""
        self.ocr_local.setVisible(text == "本地")
        self.ocr_cloud.setVisible(text == "云端")
        self._auto_save("文字模型后端已切换并保存")

    # ---- 方法三：视觉模型（本地/云端 兜底）----
    def _vision_model_form(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.vis_backend = self._combo(["本地", "云端"])
        self._field(f, "后端：", self.vis_backend)

        # 本地子表单
        self.vision_local = QWidget()
        lf = QFormLayout(self.vision_local)
        lf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.vision_local_endpoint = QLineEdit("http://localhost:11434/v1")
        self._field(lf, "端点：", self.vision_local_endpoint)
        self.vision_local_model = QLineEdit()
        self._field(lf, "模型名：", self.vision_local_model)

        # 云端子表单
        self.vision_cloud = QWidget()
        cf = QFormLayout(self.vision_cloud)
        cf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.vision_cloud_key = QLineEdit()
        self.vision_cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._field(cf, "API Key：", self.vision_cloud_key)
        self.vision_cloud_endpoint = QLineEdit()
        self._field(cf, "端点：", self.vision_cloud_endpoint)
        self.vision_cloud_model = QLineEdit()
        self._field(cf, "模型名：", self.vision_cloud_model)

        f.addRow(self.vision_local)
        f.addRow(self.vision_cloud)

        self.btn_test_vis = QPushButton("测试连接")
        self.btn_test_vis.setStyleSheet(SECONDARY_BTN)
        self.btn_test_vis.clicked.connect(self._test_vision)
        f.addRow(self.btn_test_vis)

        self.vision_prompt = QPlainTextEdit(_VISION_PROMPT)
        self.vision_prompt.setMaximumHeight(90)
        f.addRow("模型提示词：", self.vision_prompt)

        self.vis_backend.currentTextChanged.connect(self._on_vis_backend)
        for le in (
            self.vision_local_endpoint, self.vision_local_model,
            self.vision_cloud_key, self.vision_cloud_endpoint,
            self.vision_cloud_model,
        ):
            le.editingFinished.connect(
                lambda: self._auto_save("视觉模型配置已保存")
            )
        self.vision_prompt.textChanged.connect(
            lambda: self._auto_save("视觉提示词已保存")
        )
        self._on_vis_backend(self.vis_backend.currentText())
        return w

    def _on_vis_backend(self, text: str):
        """视觉模型 本地/云端 切换：显示对应子表单并自动保存。"""
        self.vision_local.setVisible(text == "本地")
        self.vision_cloud.setVisible(text == "云端")
        self._auto_save("视觉模型后端已切换并保存")

    def _build_llm_config(self) -> tuple[str, dict]:
        """从文字模型 UI 收集 LLM 配置，返回 (kind, cfg_dict)。"""
        if self.text_backend.currentText() == "本地":
            return "本地API", {
                "endpoint": self.ocr_local_endpoint.text().strip(),
                "model": self.ocr_local_model.text().strip(),
                "prompt": self.ocr_prompt.toPlainText(),
            }
        return "云端API", {
            "endpoint": self.ocr_cloud_endpoint.text().strip(),
            "api_key": self.ocr_cloud_key.text().strip(),
            "model": self.ocr_cloud_model.text().strip(),
            "prompt": self.ocr_prompt.toPlainText(),
        }

    def test_connection(self):
        kind, cfg = self._build_llm_config()
        if kind == "正则判断":
            msg = "✅ 正则判断模式：无需连接，纯正则解析"
            self.status_label.setText(msg)
            QMessageBox.information(self, "测试连接", msg)
            return
        from llm_backend import build_llm_backend

        backend = build_llm_backend(kind, cfg)
        if backend is None:
            missing = []
            if kind in ("本地API", "云端API") and not cfg.get("endpoint"):
                missing.append("端点")
            if kind == "云端API" and not cfg.get("api_key"):
                missing.append("API Key")
            msg = f"❌ 请先填写：{'、'.join(missing)}"
            self.status_label.setText(msg)
            QMessageBox.warning(self, "测试连接", msg)
            return
        ok, msg = backend.test_connection()
        result = f"{'✅' if ok else '❌'} {msg}"
        self.status_label.setText(result)
        if ok:
            QMessageBox.information(self, "测试连接", result)
        else:
            QMessageBox.critical(self, "测试连接", result)

    # ---- 识别模式辅助 ----
    @staticmethod
    def _parse_mode_key(mode: str, ocr_backend: str | None = None):
        """把配置里的 mode（新全键或旧标识）归一化为 (方法, 本地|云端|None)。"""
        if mode == "OCR+正则":
            return "OCR+正则", None
        if mode.startswith("视觉模型"):
            return "视觉模型", ("本地" if "本地" in mode else "云端")
        if mode.startswith("文字模型"):
            return "文字模型", ("本地" if "本地" in mode else "云端")
        # 旧标识兼容
        if mode == "本地视觉模型":
            return "视觉模型", "本地"
        if mode == "云端视觉 API":
            return "视觉模型", "云端"
        if mode in ("OCR+文字模型", "OCR + 文字模型（默认）"):
            if ocr_backend == "本地API":
                return "文字模型", "本地"
            if ocr_backend == "云端API":
                return "文字模型", "云端"
            return "OCR+正则", None  # 正则判断 → 纯前端
        return "OCR+正则", None

    def _current_mode(self) -> str:
        """返回当前识别模式全键：
        OCR+正则 / 文字模型·本地 / 文字模型·云端 / 视觉模型·本地 / 视觉模型·云端。
        方法选自单选卡，本地/云端选自各方法内的后端下拉。
        """
        method = "OCR+正则"
        for c in self._model_cards:
            if c["radio"].isChecked():
                method = c["method"]
                break
        if method == "视觉模型":
            sub = "本地" if self.vis_backend.currentText() == "本地" else "云端"
            return f"视觉模型·{sub}"
        if method == "文字模型":
            sub = "本地" if self.text_backend.currentText() == "本地" else "云端"
            return f"文字模型·{sub}"
        return "OCR+正则"

    def _build_vision_config(self) -> tuple[str, dict]:
        """从视觉模型 UI 收集配置，返回 (kind, cfg_dict)。kind 用旧标识兼容后端。"""
        mode = self._current_mode()
        if mode == "视觉模型·本地":
            return "本地视觉模型", {
                "endpoint": self.vision_local_endpoint.text().strip(),
                "model": self.vision_local_model.text().strip(),
                "prompt": self.vision_prompt.toPlainText(),
            }
        if mode == "视觉模型·云端":
            return "云端视觉 API", {
                "endpoint": self.vision_cloud_endpoint.text().strip(),
                "api_key": self.vision_cloud_key.text().strip(),
                "model": self.vision_cloud_model.text().strip(),
                "prompt": self.vision_prompt.toPlainText(),
            }
        return "", {}

    def _test_vision(self):
        from llm_backend import build_vision_backend

        kind = "本地视觉模型" if self.vis_backend.currentText() == "本地" else "云端视觉 API"
        if kind == "本地视觉模型":
            cfg = {
                "endpoint": self.vision_local_endpoint.text().strip(),
                "model": self.vision_local_model.text().strip(),
            }
        else:
            cfg = {
                "endpoint": self.vision_cloud_endpoint.text().strip(),
                "api_key": self.vision_cloud_key.text().strip(),
                "model": self.vision_cloud_model.text().strip(),
            }
        backend = build_vision_backend(kind, cfg)
        if backend is None:
            missing = []
            if not cfg.get("endpoint"):
                missing.append("端点")
            if not cfg.get("model"):
                missing.append("模型名")
            if kind == "云端视觉 API" and not cfg.get("api_key"):
                missing.append("API Key")
            msg = f"❌ 请先填写：{'、'.join(missing)}"
            self.status_label.setText(msg)
            QMessageBox.warning(self, "测试连接", msg)
            return
        ok, msg = backend.test_connection()
        result = f"{'✅' if ok else '❌'} {msg}"
        self.status_label.setText(result)
        if ok:
            QMessageBox.information(self, "测试连接", result)
        else:
            QMessageBox.critical(self, "测试连接", result)

    # ========== 设置页 ==========
    def _build_settings_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        title = QLabel("设置")
        title.setStyleSheet("font-size:14px;font-weight:bold;color:#2c3e50;")
        v.addWidget(title)

        card = QFrame()
        card.setStyleSheet("QFrame{background:#fff;border:1px solid #ecf0f1;border-radius:6px;}")
        fl = QFormLayout(card)
        fl.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        fl.setContentsMargins(16, 16, 16, 16)
        fl.setSpacing(12)

        self.set_template = self._field(
            fl, "输出格式模板：", QLineEdit("{price}{platform}{ext}")
        )
        self.set_conflict = self._field(
            fl, "文件名冲突策略：",
            self._combo(["追加序号 (1)(2)...", "覆盖", "跳过"]),
        )
        self.set_threshold = self._field(fl, "低置信度阈值：", QDoubleSpinBox())
        self.set_threshold.setRange(0.0, 1.0)
        self.set_threshold.setSingleStep(0.01)
        self.set_threshold.setValue(0.70)
        self.set_fail = self._field(
            fl, "识别失败处理：",
            self._combo(["跳过并记录日志", "中止全部", "弹窗确认"]),
        )
        self.set_logpath = QLineEdit(str(DEFAULT_LOG_DIR))
        self.set_logpath.setReadOnly(True)
        self.btn_logbrowse = QPushButton("打开")
        self.btn_logbrowse.setStyleSheet(SECONDARY_BTN)
        self.btn_logbrowse.clicked.connect(self._open_log_dir)
        log_row = QHBoxLayout()
        log_row.addWidget(self.set_logpath)
        log_row.addWidget(self.btn_logbrowse)
        self._field(fl, "日志保存路径：", log_row)

        # 淘宝订单数据 Excel（从主界面移至此处；选文件夹时自动匹配）
        self.st_tb_excel_path = QLineEdit()
        self.st_tb_excel_path.setPlaceholderText("淘宝订单Excel（仅淘宝用：提供时间/商品名，按价格匹配）")
        self.btn_st_tb_excel = QPushButton("浏览")
        self.btn_st_tb_excel.setStyleSheet(SECONDARY_BTN)
        self.btn_st_tb_excel.clicked.connect(self.browse_taobao_excel)
        st_tb_row = QHBoxLayout()
        st_tb_row.addWidget(self.st_tb_excel_path, 1)
        st_tb_row.addWidget(self.btn_st_tb_excel)
        self._field(fl, "淘宝订单数据Excel：", st_tb_row)

        self.set_autorename = QCheckBox("识别完成后自动执行重命名（少一步操作）")
        self.set_autorename.setChecked(True)
        self._field(fl, "自动重命名：", self.set_autorename)

        self.set_invoice_category = self._field(
            fl, "发票类别值：", QLineEdit("发票")
        )
        self.set_invoice_category.setToolTip(
            "发票文件名末尾的类别段，如 发票/其他；可在识别或重命名前修改"
        )

        self.set_types = self._field(
            fl, "支持文件类型：", QLineEdit(".jpg .jpeg .png .pdf")
        )
        self.set_types.setReadOnly(True)

        v.addWidget(card)

        # 保存配置
        save_row = QHBoxLayout()
        self.save_hint = QLabel("")
        self.save_hint.setStyleSheet("color:#1abc9c;font-size:12px;")
        save_row.addWidget(self.save_hint)
        save_row.addStretch()
        self.btn_save_cfg = QPushButton("保存配置")
        self.btn_save_cfg.setStyleSheet(PRIMARY_BTN)
        self.btn_save_cfg.clicked.connect(self.save_config_action)
        save_row.addWidget(self.btn_save_cfg)
        v.addLayout(save_row)
        v.addStretch()
        return w

    # ---------- M4：配置持久化 ----------
    def _apply_config(self, cfg: dict):
        s = cfg.get("settings", {})
        if "template" in s:
            self.set_template.setText(s["template"])
        if "conflict" in s:
            idx = self.set_conflict.findText(s["conflict"])
            if idx >= 0:
                self.set_conflict.setCurrentIndex(idx)
        if "threshold" in s:
            self.set_threshold.setValue(float(s["threshold"]))
        if "fail" in s:
            idx = self.set_fail.findText(s["fail"])
            if idx >= 0:
                self.set_fail.setCurrentIndex(idx)
        if "types" in s:
            self.set_types.setText(s["types"])
        if "invoice_category" in s:
            self.set_invoice_category.setText(s["invoice_category"])

        m = cfg.get("model", {})
        if "mode" in m:
            mode = m["mode"]
            # 归一化：新全键 / 旧标识 → (方法, 本地|云端|None)
            method, sub = self._parse_mode_key(mode, m.get("ocr_backend"))
            for c in self._model_cards:
                if c["method"] == method:
                    c["radio"].setChecked(True)
                    self._on_model_selected(c["radio"])
                    break
            if method == "文字模型" and sub:
                idx = self.text_backend.findText(sub)
                if idx >= 0:
                    self.text_backend.setCurrentIndex(idx)
            elif method == "视觉模型" and sub:
                idx = self.vis_backend.findText(sub)
                if idx >= 0:
                    self.vis_backend.setCurrentIndex(idx)
        # 独立恢复本地/云端子选择（即使当前方法为 OCR+正则 也保留上次选择）
        if "text_backend" in m:
            idx = self.text_backend.findText(m["text_backend"])
            if idx >= 0:
                self.text_backend.setCurrentIndex(idx)
        if "vis_backend" in m:
            idx = self.vis_backend.findText(m["vis_backend"])
            if idx >= 0:
                self.vis_backend.setCurrentIndex(idx)
        if "ocr_local" in m:
            sub = m["ocr_local"]
            if "endpoint" in sub:
                self.ocr_local_endpoint.setText(sub["endpoint"])
            if "model" in sub:
                self.ocr_local_model.setText(sub["model"])
        if "ocr_cloud" in m:
            sub = m["ocr_cloud"]
            if "endpoint" in sub:
                self.ocr_cloud_endpoint.setText(sub["endpoint"])
            if "api_key" in sub:
                self.ocr_cloud_key.setText(_obf_decode(sub["api_key"]))
            if "model" in sub:
                self.ocr_cloud_model.setText(sub["model"])
        if "vision_local" in m:
            sub = m["vision_local"]
            if "endpoint" in sub:
                self.vision_local_endpoint.setText(sub["endpoint"])
            if "model" in sub:
                self.vision_local_model.setText(sub["model"])
        if "vision_cloud" in m:
            sub = m["vision_cloud"]
            if "endpoint" in sub:
                self.vision_cloud_endpoint.setText(sub["endpoint"])
            if "api_key" in sub:
                self.vision_cloud_key.setText(_obf_decode(sub["api_key"]))
            if "model" in sub:
                self.vision_cloud_model.setText(sub["model"])
        # 模型提示词：旧配置无该字段时保留 UI 内置默认值
        if "ocr_prompt" in m:
            self.ocr_prompt.setPlainText(m["ocr_prompt"])
        if "vision_prompt" in m:
            self.vision_prompt.setPlainText(m["vision_prompt"])
        elif "vision_local_prompt" in m:
            self.vision_prompt.setPlainText(m["vision_local_prompt"])
        elif "vision_cloud_prompt" in m:
            self.vision_prompt.setPlainText(m["vision_cloud_prompt"])

        # 自动重命名开关
        if "auto_rename" in s:
            self.set_autorename.setChecked(bool(s["auto_rename"]))
        # 日志保存目录——固定为项目根 logs/，忽略 config 中的旧值
        # （旧 config 可能存了其他路径，不再使用）
        self.set_logpath.setText(str(DEFAULT_LOG_DIR))
        # 淘宝订单数据 Excel
        if "taobao_excel" in s:
            self.st_tb_excel_path.setText(s["taobao_excel"])

        # 恢复上次上传文件夹（仅在路径仍存在时）
        last = cfg.get("last_folder")
        if last and Path(last).exists() and Path(last).is_dir():
            self.load_folder(last)

    def _collect_config(self) -> dict:
        mode = self._current_mode()   # 全键：OCR+正则 / 文字模型·本地 …
        return {
            "last_folder": self.folder_path,
            "settings": {
                "template": self.set_template.text(),
                "conflict": self.set_conflict.currentText(),
                "threshold": self.set_threshold.value(),
                "fail": self.set_fail.currentText(),
                "types": self.set_types.text(),
                "invoice_category": self.set_invoice_category.text(),
                "auto_rename": self.set_autorename.isChecked(),
                "log_dir": str(DEFAULT_LOG_DIR),
                "taobao_excel": self.st_tb_excel_path.text().strip(),
            },
            "model": {
                "mode": mode,
                "text_backend": self.text_backend.currentText(),
                "vis_backend": self.vis_backend.currentText(),
                "ocr_local": {
                    "endpoint": self.ocr_local_endpoint.text().strip(),
                    "model": self.ocr_local_model.text().strip(),
                },
                # 注意：API Key 以混淆(base64)形式持久化，避免明文落盘
                "ocr_cloud": {
                    "endpoint": self.ocr_cloud_endpoint.text().strip(),
                    "api_key": _obf_encode(self.ocr_cloud_key.text().strip()),
                    "model": self.ocr_cloud_model.text().strip(),
                },
                "vision_local": {
                    "endpoint": self.vision_local_endpoint.text().strip(),
                    "model": self.vision_local_model.text().strip(),
                },
                # 注意：API Key 以混淆(base64)形式持久化，避免明文落盘
                "vision_cloud": {
                    "endpoint": self.vision_cloud_endpoint.text().strip(),
                    "api_key": _obf_encode(self.vision_cloud_key.text().strip()),
                    "model": self.vision_cloud_model.text().strip(),
                },
                # 模型提示词：公开可编辑、持久化（非敏感）
                "ocr_prompt": self.ocr_prompt.toPlainText(),
                "vision_prompt": self.vision_prompt.toPlainText(),
            },
        }

    def _auto_save(self, msg: str):
        """初始化完成后，任意改动都即时落盘，免去手动保存。失败静默。"""
        if not getattr(self, "_inited", False):
            return
        try:
            save_config(self._collect_config())
        except Exception:  # noqa: BLE001
            return  # 配置读写失败不应阻断正常流程
        self.status_label.setText("✅ " + msg)
        self.model_status.setText("✅ " + msg)
        self.save_hint.setText("✅ 配置已保存")

    def save_config_action(self):
        save_config(self._collect_config())
        self.status_label.setText("配置已保存到 config.json")
        self.save_hint.setText("✅ 配置已保存")

    def _open_log_dir(self):
        """打开日志目录——日志固定在项目根 logs/ 下，不可更改。"""
        os.startfile(str(DEFAULT_LOG_DIR))


class ReimbDialog(QDialog):
    """导出报销导入Excel 的参数对话框：费用类型 / 导入模板 / 输出路径。"""

    def __init__(self, parent=None, default_folder: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("导出报销导入Excel")
        self.resize(540, 210)
        self.default_folder = default_folder
        v = QVBoxLayout(self)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        cfg = load_config().get("settings", {})
        default_tmpl = cfg.get("reimbursement_template") or DEFAULT_REIMB_TEMPLATE
        default_type = cfg.get("reimbursement_expense_type") or "买耗材"

        self.expense_type = QLineEdit(default_type)
        form.addRow("费用类型：", self.expense_type)

        self.template_path = QLineEdit(default_tmpl)
        btn_tmpl = QPushButton("浏览")
        btn_tmpl.setStyleSheet(SECONDARY_BTN)
        btn_tmpl.clicked.connect(self._browse_template)
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(self.template_path)
        tmpl_row.addWidget(btn_tmpl)
        form.addRow("导入模板：", tmpl_row)

        self.out_path = QLineEdit()
        self.out_path.setPlaceholderText(
            "留空 → 保存到当前文件夹" if default_folder else "留空 → 保存到日志目录"
        )
        if default_folder:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.out_path.setText(
                str(Path(default_folder) / f"报销导入_{default_type}_{ts}.xlsx")
            )
        btn_out = QPushButton("浏览")
        btn_out.setStyleSheet(SECONDARY_BTN)
        btn_out.clicked.connect(self._browse_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_path)
        out_row.addWidget(btn_out)
        form.addRow("输出路径：", out_row)

        v.addLayout(form)

        hint = QLabel("以导入模板为骨架填数据行；Sheet 名与费用类型一致，其余结构原样保留。")
        hint.setStyleSheet("color:#7f8c8d;font-size:11px;")
        v.addWidget(hint)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _browse_template(self):
        d, _ = QFileDialog.getOpenFileName(
            self, "选择导入模板", self.template_path.text() or "",
            "Excel 文件 (*.xlsx *.xls)",
        )
        if d:
            self.template_path.setText(d)

    def _browse_out(self):
        start = self.out_path.text() or self.default_folder or ""
        d, _ = QFileDialog.getSaveFileName(
            self, "选择输出路径", start,
            "Excel 文件 (*.xlsx)",
        )
        if d:
            self.out_path.setText(d)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
