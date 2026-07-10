# -*- mode: python ; coding: utf-8 -*-
"""XM报销OCR PyInstaller 打包配置 (onedir)。

构建：在 managed venv 下执行
    pyinstaller XM报销OCR.spec --noconfirm

产物：dist/XM报销OCR/XM报销OCR.exe + 依赖文件夹（首次启动零解压）

设计要点：
- onedir：启动即开，无需解压 200MB+；分发时 zip 整个文件夹
- m1_poc 作为数据包打入（含 __init__.py 使其可 import）
- rapidocr_onnxruntime / onnxruntime 必须 collect-all：含 ONNX 模型权重与 native DLL
- 不打包 config.json：避免分发用户本机绝对路径，exe 首次运行自动生成空配置
- LM（LM Studio + GGUF）天然外部 HTTP，与本 exe 无关
"""
from PyInstaller.utils.hooks import collect_all

# ---- collect native deps / data / binaries ----
rapiddatas, rapiddatas_bin, rapiddatas_hid = collect_all('rapidocr_onnxruntime')
ortdatas, ortdatas_bin, ortdatas_hid = collect_all('onnxruntime')
qt6datas, qt6datas_bin, qt6datas_hid = collect_all('PyQt6')

all_datas = [
    ('m1_poc', 'm1_poc'),       # OCR + 正则解析，作为顶层包打入
] + rapiddatas + ortdatas + qt6datas

all_binaries = rapiddatas_bin + ortdatas_bin + qt6datas_bin
all_hidden = (rapiddatas_hid + ortdatas_hid + qt6datas_hid
              + ['rapidocr_onnxruntime', 'onnxruntime'])

a = Analysis(
    ['app/main.py'],
    pathex=['.'],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'tensorboard'],  # venv 误装 torch(2GB+)，RapidOCR 用 onnxruntime 推理不需要，排除以防 exe 爆炸
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='XM报销OCR',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX 压缩 native DLL 易触发杀软误报，关闭
    runtime_tmpdir=None,
    console=False,        # GUI 程序，无控制台窗口
    disable_windowed_traceback=False,
    icon=None,            # 暂无图标；如需可改为 icon='assets/app.ico'
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='XM报销OCR',
)
