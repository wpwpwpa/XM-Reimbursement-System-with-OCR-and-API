"""回归测试：复现用户报告的三连 bug 并验证修复。

场景（用户实际操作序列）：
  1. 文件本来就命名正确（如 26.52淘宝.png）
  2. 识别成功 → 重命名（旧版会误加 (1)）
  3. 再点一次重命名（旧版报"文件不存在"）
  4. 再点开始识别（旧版闪退）

修复目标：
  - 重命名对"已符合命名"的文件为 no-op，不加 (1)
  - 二次重命名幂等，不报错
  - 二次识别不崩溃
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from main import MainWindow

SRC_IMG = Path(__file__).resolve().parent.parent / "m1_poc/test_images/taobao.png"
CORRECT_NAME = "26.52淘宝.png"  # = {price}{platform}{ext}，即"本来就对"的名字


def _recognize_sync(win):
    """同步驱动后台识别线程直到完成。"""
    win.start_recognition()
    loop = QEventLoop()
    win._worker.finished.connect(lambda *_: loop.quit())
    QTimer.singleShot(60000, loop.quit)  # 兜底超时
    loop.exec()
    win._worker.wait()


def main():
    assert SRC_IMG.exists(), f"缺少合成测试图：{SRC_IMG}"
    app = QApplication(sys.argv)

    tmp = Path(tempfile.mkdtemp(prefix="bugfix_"))
    try:
        target = tmp / CORRECT_NAME
        shutil.copy(SRC_IMG, target)  # 文件本来就命名正确

        win = MainWindow()
        win.show()
        win.set_autorename.setChecked(False)  # 测试手动控制重命名时机
        n = win.load_folder(str(tmp))
        assert n == 1, f"应扫描到 1 张，实际 {n}"

        # 第一次识别
        _recognize_sync(win)
        assert win.results[0] and win.results[0]["status"] == "success", "识别应成功"
        new_name = win.table.item(0, 3).text()
        assert new_name == CORRECT_NAME, f"新文件名应为 {CORRECT_NAME}，实际 {new_name}"
        print(f"[OK] 识别成功，新文件名 = {new_name}")

        # 第一次重命名 —— 关键：不能加 (1)
        win.execute_rename()
        files_after = [p.name for p in tmp.iterdir() if p.suffix == ".png"]
        assert CORRECT_NAME in files_after, f"应保留 {CORRECT_NAME}，实际 {files_after}"
        assert not any("(1)" in f for f in files_after), \
            f"BUG: 已正确命名的文件被误加 (1)！目录={files_after}"
        assert win.table.item(0, 0).text() == CORRECT_NAME
        assert win.files[0].name == CORRECT_NAME, "self.files 应与磁盘同步"
        print(f"[OK] 首次重命名为 no-op，未加 (1)：{files_after}")

        # 第二次重命名 —— 关键：幂等，不报"文件不存在"
        win.execute_rename()
        files_after2 = [p.name for p in tmp.iterdir() if p.suffix == ".png"]
        assert files_after2 == files_after, f"二次重命名应幂等，实际 {files_after2}"
        # 幂等判据：成功 1、跳过/失败 0（不能出现"文件不存在"类报错）
        assert "跳过/失败 0" in win.status_label.text(), \
            f"BUG: 二次重命名出现失败：{win.status_label.text()}"
        print(f"[OK] 二次重命名幂等，无错误：{win.status_label.text()}")

        # 第二次识别 —— 关键：不闪退
        _recognize_sync(win)
        assert win.results[0]["status"] == "success", "二次识别仍应成功"
        print("[OK] 二次识别正常，无闪退")

        # 附加：文件缺失场景不崩溃
        win.files = [tmp / "不存在的文件.png"]
        win.results = [None]
        win.table.setRowCount(1)
        from PyQt6.QtWidgets import QTableWidgetItem
        for c in range(5):
            win.table.setItem(0, c, QTableWidgetItem(""))
        _recognize_sync(win)
        assert win.results[0]["status"] == "failed", "缺失文件应标记失败而非崩溃"
        assert "不存在" in win.results[0].get("reason", ""), win.results[0]
        print("[OK] 文件缺失场景优雅降级为失败，未崩溃")

        print("\n=== 全部回归测试通过：三连 bug 已修复 ===")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
