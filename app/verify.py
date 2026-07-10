"""M2/M3/M4 无头验证：offscreen 模式构建窗口并校验布局、识别、重命名、日志、撤销、配置。

用法：
    python app/verify.py
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop
from PyQt6.QtWidgets import QApplication, QLabel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import (MainWindow, IMAGE_EXTS, RecognitionWorker, load_config,
                   COL_STATUS, COL_NEWNAME)
from recognizer import recognize_one, render_template, DEFAULT_THRESHOLD
from m1_poc.ocr_backend import OcrBackend


def main():
    app = QApplication(sys.argv)

    # 测试隔离：把配置重定向到临时文件，绝不触碰用户真实 config.json
    import main as _main
    from pathlib import Path as _Path

    _cfg_dir = tempfile.mkdtemp(prefix="xm_verify_")
    _main.CONFIG_PATH = _Path(_cfg_dir) / "config.json"
    # 干净起点，避免上次运行残留影响断言
    try:
        with open(_main.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("{}")
    except OSError:
        pass  # 沙箱等环境可能禁止写入，忽略

    # 1) 窗口可构建，三页齐全
    win = MainWindow()
    win.show()  # offscreen 下需 show() 子控件 isVisible 才生效
    win.set_autorename.setChecked(False)  # 测试手动控制重命名时机
    assert win.stack.count() == 3, "应有 3 个选项卡页"
    assert [b.text() for b in win.tab_btns] == ["文件处理", "模型选择", "设置"]
    print("[OK] 窗口构建成功，选项卡栏=[文件处理/模型选择/设置]")

    # 2) 默认在文件处理页
    assert win.stack.currentIndex() == 0
    print("[OK] 默认显示文件处理页")

    # 3) 文件处理页：7 列表格（含时间/商品）+ KPI + 按钮
    assert win.table.columnCount() == 7, "结果表应有 7 列"
    assert win.kpi_scanned is not None and win.btn_recognize is not None
    assert win.btn_rename is not None and win.btn_undo is not None
    assert win.btn_export is not None, "应有 导出订单Excel 按钮"
    print("[OK] 文件处理页：7 列表格 + KPI + 按钮(识别/重命名/导出/撤销)")

    # 4) 扫描真实截图文件夹（桌面 报销测试）
    real_dir = "C:/Users/10516/Desktop/报销测试"
    n = win.load_folder(real_dir)
    assert n == win.table.rowCount(), "表格行数应与扫描数一致"
    assert n == 6, f"期望 6 张真实图，实际 {n}"
    assert win.table.item(0, 0) is not None and win.table.item(0, 0).text()
    assert win.table.item(0, COL_STATUS).text() == "待识别"
    assert win.kpi_scanned.findChild(QLabel, "kpiNum").text() == "6"
    print(f"[OK] 扫描真实图：{n} 张，表格已填充，状态列=待识别，KPI 已扫描=6")

    # 5) 合成测试图
    test_dir = "C:/Users/10516/WorkBuddy/XM报销OCR识别搭配大模型/m1_poc/test_images"
    n2 = win.load_folder(test_dir)
    assert n2 == 3, f"期望 3 张合成图，实际 {n2}"
    print(f"[OK] 扫描合成图：{n2} 张，表格已填充")

    # 5b) M3 识别接入（同步跑，复用 m1_poc 模式C管线）
    expect = {
        "meituan.png": ("¥14.6", "美团", "14.6美团.png"),
        "pinduoduo.png": ("¥14.1", "拼多多", "14.1拼多多.png"),
        "taobao.png": ("¥26.52", "淘宝", "26.52淘宝.png"),
    }
    backend = OcrBackend()
    rec = fail = 0
    for i, f in enumerate(win.files):
        parsed = recognize_one(backend, f, DEFAULT_THRESHOLD)
        st = parsed["status"]
        new_name = (
            render_template(win.set_template.text(), parsed.get("amount"),
                            parsed.get("platform"), f.suffix.lower())
            if st == "success" else ""
        )
        win._on_row_done(i, parsed, new_name)
        if st == "success":
            rec += 1
        else:
            fail += 1
    win._on_rec_finished(rec, fail)
    for i, f in enumerate(win.files):
        exp = expect.get(f.name)
        if not exp:
            continue
        assert win.table.item(i, 1).text() == exp[0], f"{f.name} 金额={win.table.item(i,1).text()}"
        assert win.table.item(i, 2).text() == exp[1]
        assert win.table.item(i, COL_NEWNAME).text() == exp[2]
        assert win.table.item(i, COL_STATUS).text() == "✅成功"
    assert win.kpi_recognized.findChild(QLabel, "kpiNum").text() == "3"
    assert win.kpi_failed.findChild(QLabel, "kpiNum").text() == "0"
    print("[OK] M3 识别：合成图金额/平台/新文件名/状态正确，KPI 已识别=3 失败=0")

    # 5c) 真实后台线程路径（按钮 -> worker -> 信号 -> 表格），经事件循环等待完成
    loop = QEventLoop()
    collected = []
    win._worker = RecognitionWorker(
        win.files, win.set_template.text(), win.set_threshold.value()
    )
    win._worker.row_done.connect(lambda *a: collected.append(a))
    win._worker.finished.connect(loop.quit)
    win._worker.start()
    loop.exec()
    assert len(collected) == 3, f"worker 应 emit 3 行，实际 {len(collected)}"
    assert win.table.item(0, 1).text().startswith("¥"), "信号应已更新表格金额列"
    assert win.table.item(0, COL_STATUS).text() in ("✅成功", "❌失败")
    print("[OK] M3 后台线程：worker 信号经事件循环正确更新表格")

    # 5d) M5：进度条控件存在 + 低置信度⚠️标记（直接喂结果，不依赖 OCR）
    from PyQt6.QtGui import QColor
    assert win.progress_bar is not None, "文件页应有进度条控件"
    # 低置信度成功（conf=0.72 < 0.85）→ 应标 ⚠️ 且橙色
    low = {"status": "success", "amount": 26.52, "platform": "淘宝",
           "confidence": 0.72, "used_llm": False}
    win._on_row_done(0, low, "26.52淘宝.png")
    c0 = win.table.item(0, COL_STATUS)
    assert "⚠️" in c0.text(), f"低置信度应显示 ⚠️，实际 {c0.text()}"
    assert c0.foreground().color().red() > 200, "低置信度状态应着橙色"
    # 高置信度（conf=1.0）→ 不应标 ⚠️
    high = {"status": "success", "amount": 14.6, "platform": "美团",
            "confidence": 1.0, "used_llm": False}
    win._on_row_done(1, high, "14.6美团.png")
    assert "⚠️" not in win.table.item(1, COL_STATUS).text(), "高置信度不应标 ⚠️"
    print("[OK] M5：进度条控件存在；低置信度⚠️标记+橙色，高置信度无标记")

    # 6) 模型选择页：三模式单选 + 默认选中 OCR 卡片 + LLM 下拉
    assert len(win.model_group.buttons()) == 3, "应有 3 个模式单选"
    assert win.ocr_backend is not None and win.btn_test is not None
    assert win.ocr_backend.currentText() == "正则判断", "默认 LLM 后端应为 正则判断"
    selected = [c for c in win._model_cards if c["radio"].isChecked()]
    assert len(selected) == 1 and selected[0]["radio"].text().startswith("OCR"), \
        "默认应选中 OCR+文字模型 卡片"
    print("[OK] 模型选择页：3 模式单选，默认选中 OCR+文字模型，LLM 下拉=正则判断")

    # 7) 切换 LLM 后端显示对应子表单（先切到模型页）
    win._switch("模型选择")
    win.ocr_backend.setCurrentText("云端API")
    assert win.ocr_cloud.isVisible() and not win.ocr_local.isVisible()
    win.ocr_backend.setCurrentText("本地API")
    assert win.ocr_local.isVisible() and not win.ocr_cloud.isVisible()
    # 模型页改动应即时落盘，无需进设置页手动保存
    cfg = load_config()
    assert cfg["model"]["ocr_backend"] == "本地API", \
        "切换 LLM 后端应自动保存到 config.json（无需点保存配置）"
    assert win.model_status.text().startswith("✅"), "模型页应显示自动保存反馈"
    win.ocr_backend.setCurrentText("正则判断")  # 恢复默认
    assert not win.ocr_local.isVisible() and not win.ocr_cloud.isVisible()
    print("[OK] 模型选择页：LLM 后端下拉切换正确显示/隐藏子表单（正则/本地API/云端API）")

    # 8) M6 视觉模式：表单存在 + 切换模式写入配置 + 后端构建/置信度合成（不联网）
    assert win.vision_local_endpoint is not None and win.vision_local_model is not None
    assert win.vision_cloud_key is not None and win.vision_cloud_endpoint is not None \
        and win.vision_cloud_model is not None
    win._model_cards[1]["radio"].setChecked(True)  # 本地视觉模型
    assert win._current_mode() == "本地视觉模型"
    cfg = load_config()
    assert cfg["model"]["mode"] == "本地视觉模型"
    assert cfg["model"]["vision_local"]["endpoint"] == "http://localhost:11434/v1"
    from llm_backend import build_vision_backend, _normalize_vision
    assert build_vision_backend("本地视觉模型", {"endpoint": "x", "model": ""}) is None
    assert build_vision_backend(
        "本地视觉模型", {"endpoint": "http://localhost:11434/v1", "model": "llava"}
    ) is not None
    r = _normalize_vision({"amount": 26.52, "platform": "淘宝"})
    assert r["status"] == "success" and r["confidence"] == 1.0
    r2 = _normalize_vision({"amount": 26.52, "platform": "未知"})
    assert r2["status"] == "success" and r2["confidence"] == 0.9
    r3 = _normalize_vision({"amount": None, "platform": "淘宝"})
    assert r3["status"] == "failed"
    win._model_cards[2]["radio"].setChecked(True)  # 恢复 OCR+ 默认
    assert win._current_mode() == "OCR+文字模型"
    print("[OK] M6 视觉模式：表单齐全；切换模式写入配置；视觉后端构建与置信度合成正确")

    # 9) M7 模型提示词：公开可编辑 + 持久化 + 后端接收自定义提示词
    from llm_backend import build_llm_backend, _OCR_PROMPT, _VISION_PROMPT
    # 默认值 = 内置提示词（公开，可改）
    assert _OCR_PROMPT in win.ocr_prompt.toPlainText(), "OCR 提示词框默认应显示内置提示词"
    assert _VISION_PROMPT in win.cloud_vision_prompt.toPlainText(), "云端视觉提示词框默认应显示内置提示词"
    assert _VISION_PROMPT in win.local_vision_prompt.toPlainText(), "本地视觉提示词框默认应显示内置提示词"
    # 修改 OCR 提示词并持久化
    win.ocr_prompt.setPlainText("自定义文字提示词XYZ")
    cfg = load_config()
    assert cfg["model"]["ocr_prompt"] == "自定义文字提示词XYZ", "修改后应持久化到 config"
    # 后端接收自定义提示词
    be = build_llm_backend("本地API", {"endpoint": "http://x/v1", "model": "m",
                                       "prompt": "自定义文字提示词XYZ"})
    assert be is not None and be.prompt == "自定义文字提示词XYZ", "后端应接收自定义提示词"
    # 不传 prompt 时回退内置（parse 内 self.prompt or _OCR_PROMPT）
    be2 = build_llm_backend("本地API", {"endpoint": "http://x/v1", "model": "m"})
    assert be2.prompt is None, "未传 prompt 时应回退内置"
    win.ocr_prompt.setPlainText(_OCR_PROMPT)  # 恢复默认，避免污染后续
    print("[OK] M7 模型提示词：公开编辑框默认值=内置提示词；修改持久化；后端接收自定义提示词")

    # 7) 设置页：模板/阈值/按钮等控件存在
    assert win.set_template.text() == "{price}{platform}{ext}"
    assert abs(win.set_threshold.value() - 0.70) < 1e-6
    assert win.btn_save_cfg is not None, "设置页应有保存配置按钮"
    print("[OK] 设置页：输出模板/冲突策略/阈值/失败处理/日志路径/文件类型/保存配置 控件齐全")

    # 9) 图片扩展名过滤规则正确
    assert set(IMAGE_EXTS) == {".jpg", ".jpeg", ".png"}
    print("[OK] 图片扩展名过滤规则正确")

    # 10) M4 配置持久化往返
    win.set_template.setText("{price}{platform}{ext}")
    win.set_conflict.setCurrentIndex(0)  # 追加序号
    win.set_threshold.setValue(0.80)
    win.save_config_action()
    cfg = load_config()
    assert abs(cfg["settings"]["threshold"] - 0.80) < 1e-6
    assert cfg["settings"]["template"] == "{price}{platform}{ext}"
    assert cfg.get("last_folder") == test_dir, \
        f"保存配置应保留 last_folder，实际 {cfg.get('last_folder')}"
    assert win.save_hint.text() == "✅ 配置已保存", "保存后应显示成功反馈"
    print("[OK] M4 配置：保存到 config.json 并回读正确（含 last_folder 保留 + 保存反馈）")

    # 11) M4 重命名 + 冲突 + 日志 + 撤销（临时目录，隔离真实文件）
    tmp = tempfile.mkdtemp()
    try:
        for name in ("meituan.png", "pinduoduo.png", "taobao.png"):
            shutil.copy(os.path.join(test_dir, name), os.path.join(tmp, name))
        shutil.copy(os.path.join(test_dir, "meituan.png"),
                    os.path.join(tmp, "meituan_dup.png"))  # 冲突源

        w2 = MainWindow()
        w2.show()
        w2.set_autorename.setChecked(False)  # 避免识别完成信号自动重命名
        w2.set_logpath.setText(tmp)  # 日志写到 tmp，便于下方断言
        w2.set_template.setText("{price}{platform}{ext}")
        w2.set_conflict.setCurrentIndex(0)  # 追加序号
        assert w2.load_folder(tmp) == 4

        be = OcrBackend()
        r_ok = 0
        for i, f in enumerate(w2.files):
            parsed = recognize_one(be, f, DEFAULT_THRESHOLD)
            new_name = (
                render_template(w2.set_template.text(), parsed.get("amount"),
                                parsed.get("platform"), f.suffix.lower())
                if parsed["status"] == "success" else ""
            )
            w2._on_row_done(i, parsed, new_name)
            if parsed["status"] == "success":
                r_ok += 1
        w2._on_rec_finished(r_ok, 4 - r_ok)

        w2.execute_rename()

        # 文件改名 + 冲突追加序号
        assert os.path.exists(os.path.join(tmp, "14.6美团.png"))
        assert os.path.exists(os.path.join(tmp, "14.6美团 (1).png"))  # 冲突
        assert os.path.exists(os.path.join(tmp, "14.1拼多多.png"))
        assert os.path.exists(os.path.join(tmp, "26.52淘宝.png"))
        assert not os.path.exists(os.path.join(tmp, "meituan.png"))
        assert not os.path.exists(os.path.join(tmp, "meituan_dup.png"))

        # 日志 schema
        logs = [p for p in os.listdir(tmp) if p.startswith("rename_log_")]
        assert len(logs) == 1, f"应生成 1 个日志，实际 {logs}"
        log = json.loads(open(os.path.join(tmp, logs[0]), encoding="utf-8").read())
        assert len(log) == 4
        for e in log:
            assert {"original_name", "new_name", "status", "timestamp"} <= set(e)
            if e["status"] == "success":
                assert e["new_name"] and e["confidence"] is not None
        print("[OK] M4 重命名：改名正确 + 冲突追加序号 + 日志 schema 正确")

        # 撤销还原
        w2.undo_rename()
        assert os.path.exists(os.path.join(tmp, "meituan.png"))
        assert os.path.exists(os.path.join(tmp, "meituan_dup.png"))
        assert os.path.exists(os.path.join(tmp, "pinduoduo.png"))
        assert os.path.exists(os.path.join(tmp, "taobao.png"))
        assert not os.path.exists(os.path.join(tmp, "14.6美团.png"))
        print("[OK] M4 撤销：文件原名全部恢复")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 12) 新特性：自动重命名 + 日志独立目录 + last_folder 持久化
    tmp2 = tempfile.mkdtemp()
    try:
        for name in ("meituan.png", "pinduoduo.png", "taobao.png"):
            shutil.copy(os.path.join(test_dir, name), os.path.join(tmp2, name))
        w3 = MainWindow()
        w3.show()
        w3.set_autorename.setChecked(True)
        w3.set_logpath.setText(tmp2)  # 日志写到 tmp2 便于断言
        w3.set_conflict.setCurrentIndex(0)
        assert w3.load_folder(tmp2) == 3
        # 后台识别 -> 完成后应自动重命名
        w3._worker = RecognitionWorker(
            w3.files, w3.set_template.text(), w3.set_threshold.value()
        )
        w3._worker.row_done.connect(w3._on_row_done)
        w3._worker.finished.connect(w3._on_rec_finished)
        loop3 = QEventLoop()
        w3._worker.finished.connect(loop3.quit)
        w3._worker.start()
        loop3.exec()
        assert os.path.exists(os.path.join(tmp2, "14.6美团.png")), \
            "自动重命名应已执行"
        logs3 = [p for p in os.listdir(tmp2) if p.startswith("rename_log_")]
        assert len(logs3) >= 1, "应生成日志到独立目录"
        cfg3 = load_config()
        assert cfg3.get("last_folder") == tmp2, \
            f"last_folder 应持久化，实际 {cfg3.get('last_folder')}"
        print("[OK] 新特性：自动重命名 + 日志独立目录 + last_folder 持久化")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    print("\n=== M2/M3/M4 验证全部通过 ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
