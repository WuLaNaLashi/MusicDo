#!/usr/bin/env python3
"""
检测 MP3 文件中的 163 key 标识，并将匹配的曲目复制到 163/ 目录下。
支持多种检测方式：ID3 标签、文件头元数据、以及整个文件的二进制搜索。
"""

import os
import shutil
import struct
from pathlib import Path
from mutagen.mp3 import MP3
from mutagen.id3 import ID3


def has_163_key_in_id3(filepath):
    """只检测 COMM/comment 标签中是否包含 163 key"""
    try:
        audio = MP3(filepath)
        if audio.tags is None:
            return False

        # 只检查 COMM (Comment) 标签
        for tag_id, value in audio.tags.items():
            if 'COMM' not in tag_id:
                continue  # 跳过非 comment 标签

            tag_str = str(value).lower()
            if '163' in tag_str and 'key' in tag_str:
                return True
            if 'netease' in tag_str:
                return True
        return False
    except Exception:
        return False


def has_163_key_in_binary(filepath, max_scan_size=256 * 1024):
    """
    二进制扫描已禁用，只依赖 COMM 标签检测。
    避免二进制误报（如封面图数据、随机字节匹配等）。
    """
    return False


def has_163_key_in_comment(filepath):
    """专门检测 Comment 标签中的 163 key"""
    try:
        audio = MP3(filepath)
        if audio.tags is None:
            return False

        # 只检查 COMM (Comment) 标签
        for tag_id in audio.tags.keys():
            if 'COMM' not in tag_id:
                continue  # 跳过非 comment 标签

            comm_value = str(audio.tags[tag_id]).lower()
            if '163' in comm_value and 'key' in comm_value:
                return True
            if 'netease' in comm_value:
                return True

        return False
    except Exception:
        return False


def is_163_song(filepath):
    """
    综合判断：文件是否含有 163 key 标识
    使用多种检测方式，任一命中即返回 True
    """
    filepath = str(filepath)

    # 方法1: ID3 标签检测
    if has_163_key_in_id3(filepath):
        return True, 'ID3 tag'

    # 方法2: Comment 标签专门检测
    if has_163_key_in_comment(filepath):
        return True, 'Comment tag'

    # 方法3: 二进制扫描
    if has_163_key_in_binary(filepath):
        return True, 'Binary marker'

    return False, None


def scan_and_copy_163_songs(source_dir, dest_dir=None, move=False):
    """
    扫描源目录中的所有 MP3，将含 163 key 的复制或移动到 163/ 目录

    Args:
        move: True 为移动(剪切)，False 为复制
    """
    if dest_dir is None:
        dest_dir = Path.cwd() / '163'
    else:
        dest_dir = Path(dest_dir)

    # 创建目标目录
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source_dir)
    if not source_path.exists():
        print(f"❌ 源目录不存在: {source_dir}")
        return

    # 查找所有 MP3 文件，排除 163 目录避免重复扫描
    mp3_files = [
        p for p in source_path.rglob("*.mp3")
        if "163" not in p.parts and p.name != "_163_songs_manifest.txt"
    ]
    print(f"🔍 发现 {len(mp3_files)} 个 MP3 文件")
    print(f"📁 目标目录: {dest_dir}")
    print("=" * 60)

    found_count = 0
    copied_count = 0
    failed_copies = []

    for i, mp3_file in enumerate(mp3_files, 1):
        is_163, method = is_163_song(mp3_file)

        if is_163:
            found_count += 1
            print(f"\n[{i}/{len(mp3_files)}] ⚠️ 发现 163 曲目: {mp3_file.name}")
            print(f"         检测方式: {method}")
            print(f"         来源: {mp3_file.parent}")

            # 直接复制到 163 根目录，不保留子目录结构
            dest_file = dest_dir / mp3_file.name

            # 处理重名文件
            counter = 1
            original_dest = dest_file
            while dest_file.exists():
                stem = original_dest.stem
                suffix = original_dest.suffix
                dest_file = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            try:
                if move:
                    # 使用 os.rename 实现真正的移动（原子操作）
                    # 同文件系统下是重命名，跨文件系统会报错，此时回退到 copy2+remove
                    try:
                        os.rename(str(mp3_file), str(dest_file))
                    except OSError:
                        # 跨文件系统，先复制再删除
                        shutil.copy2(mp3_file, dest_file)
                        os.remove(str(mp3_file))
                    copied_count += 1
                    file_size = os.path.getsize(dest_file) / (1024 * 1024)
                    print(f"         ✅ 已移动 ({file_size:.1f} MB)")
                else:
                    shutil.copy2(mp3_file, dest_file)
                    copied_count += 1
                    file_size = os.path.getsize(dest_file) / (1024 * 1024)
                    print(f"         ✅ 已复制 ({file_size:.1f} MB)")
            except Exception as e:
                failed_copies.append((mp3_file.name, str(e)))
                print(f"         ❌ {'移动' if move else '复制'}失败: {e}")
        else:
            if i % 50 == 0 or i == len(mp3_files):
                print(f"[{i}/{len(mp3_files)}] 扫描中...", end='\r')

    # 生成报告
    print("\n" + "=" * 60)
    action_name = "移动" if move else "复制"
    print(f"📊 扫描完成!")
    print(f"   总文件数: {len(mp3_files)}")
    print(f"   163 曲目: {found_count}")
    print(f"   成功{action_name}: {copied_count}")
    if failed_copies:
        print(f"   {action_name}失败: {len(failed_copies)}")
    print(f"\n📂 结果保存在: {dest_dir}")

    # 生成清单文件
    manifest_path = dest_dir / '_163_songs_manifest.txt'
    with open(manifest_path, 'w', encoding='utf-8') as f:
        f.write("# 163 曲目清单\n")
        f.write(f"# 扫描目录: {source_dir}\n")
        f.write(f"# 生成时间: {__import__('datetime').datetime.now().isoformat()}\n\n")

        for mp3_file in mp3_files:
            is_163, method = is_163_song(mp3_file)
            if is_163:
                f.write(f"[163-{method}] {mp3_file.name}\n")

    print(f"📝 清单文件: {manifest_path}")

    if failed_copies:
        print("\n❌ 复制失败的文件:")
        for name, err in failed_copies:
            print(f"   - {name}: {err}")


if __name__ == "__main__":
    import argparse
    import datetime

    parser = argparse.ArgumentParser(
        description='检测 MP3 中的 163 key 并整理到 163/ 目录'
    )
    parser.add_argument(
        'source_dir',
        nargs='?',
        default='/home/hanxiao/Music/LocalMusic/',
        help='要扫描的源目录 (默认: /home/hanxiao/Music/Bilibili/new_s/)'
    )
    parser.add_argument(
        '--dest', '-d',
        default=None,
        help='目标目录 (默认: 当前目录下的 163/)'
    )
    parser.add_argument(
        '--move', '-m',
        action='store_true',
        help='移动(剪切)文件而不是复制'
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='仅检测不复制/移动'
    )

    args = parser.parse_args()

    if args.dry_run:
        print("🔍 试运行模式 (仅检测，不复制)")
        source_path = Path(args.source_dir)
        mp3_files = [
            p for p in source_path.rglob("*.mp3")
            if "163" not in p.parts and p.name != "_163_songs_manifest.txt"
        ]
        found = 0
        for mp3_file in mp3_files:
            is_163, method = is_163_song(mp3_file)
            if is_163:
                found += 1
                print(f"⚠️ [163-{method}] {mp3_file.name}")
        print(f"\n共发现 {found} 个 163 曲目")
    else:
        scan_and_copy_163_songs(args.source_dir, args.dest, move=args.move)
