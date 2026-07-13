#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对指定目录下的图片和 PDF 进行随机重命名（保留原扩展名，无冲突）。
- 默认 preview 模式：只打印将改成的名字，不改动任何文件。
- 加 --execute 才真实重命名，并写出 old<->new 映射 CSV，便于逆向恢复。
- 随机串仅用字母+数字，避开易混字符(0/O/1/l/I)。
"""
import argparse
import csv
import os
import random
import string
import sys

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
PDF_EXTS = {".pdf"}
ALL_EXTS = IMG_EXTS | PDF_EXTS

# 避开易混字符，降低肉眼误读
_ALPHABET = (string.ascii_letters + string.digits).translate(
    str.maketrans("", "", "0O1lI")
)


def random_name(length: int) -> str:
    return "".join(random.choices(_ALPHABET, k=length))


def collect_files(folder: str):
    files = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in ALL_EXTS:
            files.append(path)
    return files


def plan_renames(files, length: int):
    used = {os.path.basename(f) for f in files}
    mapping = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        while True:
            new_base = random_name(length) + ext
            if new_base not in used:
                used.add(new_base)
                break
        mapping.append((f, os.path.join(os.path.dirname(f), new_base)))
    return mapping


def main():
    ap = argparse.ArgumentParser(description="图片/PDF 随机重命名（安全：先预览，--execute 才改）")
    ap.add_argument(
        "--dir",
        default=None,
        help="目标目录（省略则在运行时交互输入）",
    )
    ap.add_argument("--execute", action="store_true", help="真实重命名（默认仅预览）")
    ap.add_argument("--length", type=int, default=12, help="随机名长度(>=6)")
    ap.add_argument("--mapping", default=None, help="映射 CSV 输出路径")
    args = ap.parse_args()

    if args.length < 6:
        print("长度至少 6", file=sys.stderr)
        sys.exit(2)

    target = args.dir
    if not target:
        target = input("请输入要随机命名的文件夹路径：").strip().strip('"')
    if not target:
        print("未提供文件夹路径", file=sys.stderr)
        sys.exit(2)
    if not os.path.isdir(target):
        print(f"目录不存在：{target}", file=sys.stderr)
        sys.exit(2)

    files = collect_files(target)
    if not files:
        print("未找到图片/PDF 文件")
        return

    print(f"共扫描到 {len(files)} 个文件（图片/PDF）：\n")
    mapping = plan_renames(files, args.length)
    for old, new in mapping:
        print(f"  {os.path.basename(old)}  ->  {os.path.basename(new)}")

    mapping_path = args.mapping or os.path.join(target, "_rename_mapping.csv")

    if args.execute:
        do_rename(mapping, mapping_path)
    else:
        # 预览后交互确认，避免误改
        ans = input("\n确认将上述文件随机重命名？(输入 y 执行 / 其它取消)：").strip().lower()
        if ans == "y":
            do_rename(mapping, mapping_path)
        else:
            print("[已取消] 未做任何改动。")
            print(f"如需稍后执行，可加 --execute 参数，或直接输入 y 确认。")


def do_rename(mapping, mapping_path):
    for old, new in mapping:
        os.rename(old, new)
    with open(mapping_path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["old_path", "new_path"])
        for old, new in mapping:
            w.writerow([old, new])
    print(f"\n已重命名 {len(mapping)} 个文件。映射已写入：{mapping_path}")
    print("如需恢复：按 CSV 中 old/new 反向 os.rename 即可（脚本未提供 undo，手动按列反向执行）。")


if __name__ == "__main__":
    main()
