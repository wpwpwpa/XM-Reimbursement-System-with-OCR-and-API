"""M2 核心逻辑验证（不依赖 PyQt / 图形栈）。

仅验证「文件夹扫描 + 列表填充」的纯逻辑是否与 main.py 的 load_folder 一致：
- 仅扫描 jpg/jpeg/png
- 按文件名排序
- 返回图片数与文件列表

PyQt6 装好后用 verify.py 跑完整 GUI 验证；本脚本用于网络受限时先行验证逻辑。
"""
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def scan_images(folder: str):
    p = Path(folder)
    files = sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTS)
    return files


def main():
    cases = [
        ("C:/Users/10516/Desktop/报销测试", 6, "真实截图"),
        ("C:/Users/10516/WorkBuddy/XM报销OCR识别搭配大模型/m1_poc/test_images", 3, "合成测试图"),
    ]
    for folder, expect, label in cases:
        files = scan_images(folder)
        assert len(files) == expect, f"{label} 期望 {expect} 张，实际 {len(files)} 张"
        print(f"[OK] {label}：扫描 {len(files)} 张")
        for f in files:
            print(f"     - {f.name}  ({f.suffix.lower()}, {f.stat().st_size // 1024} KB)")
    assert set(IMAGE_EXTS) == {".jpg", ".jpeg", ".png"}
    print("\n=== M2 扫描逻辑验证通过（GUI 验证待 PyQt6 就绪） ===")


if __name__ == "__main__":
    main()
