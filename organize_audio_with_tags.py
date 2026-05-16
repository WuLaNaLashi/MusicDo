#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频文件整理脚本
功能：递归扫描指定目录，将具有完整元数据（标题/艺术家/专辑）的音频文件
      移动到目标目录，并生成完整的列表信息。
"""

import os
import sys
import shutil
import csv
from pathlib import Path
from datetime import datetime

try:
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp4 import MP4
    from mutagen.asf import ASF
    from mutagen.wave import WAVE
    from mutagen.id3 import ID3
except ImportError:
    print("错误：请先安装 mutagen 库")
    print("  pip3 install mutagen")
    sys.exit(1)


# ==================== 配置 ====================
SOURCE_DIR = Path("/home/hanxiao/Music/163")
TARGET_DIR = Path("/home/hanxiao/Music/yzj")
REPORT_FILE = TARGET_DIR / "audio_catalog.csv"
SUPPORTED_EXTS = {
    '.mp3', '.flac', '.ogg', '.opus', '.m4a', '.mp4', 
    '.wav', '.wma', '.aac', '.oga', '.spx', '.wma'
}


def get_audio_metadata(filepath: Path) -> dict:
    """
    提取音频文件的标题、艺术家、专辑信息。
    返回包含 'title', 'artist', 'album' 的字典，若不存在则为空字符串。
    """
    result = {"title": "", "artist": "", "album": ""}

    try:
        audio = File(str(filepath))
        if audio is None:
            return result

        # 根据文件类型提取标签
        ext = filepath.suffix.lower()

        if ext == '.mp3' or isinstance(audio, MP3):
            # ID3 标签
            if audio.tags:
                result["title"] = str(audio.tags.get("TIT2", ""))
                result["artist"] = str(audio.tags.get("TPE1", ""))
                result["album"] = str(audio.tags.get("TALB", ""))

        elif ext == '.flac' or isinstance(audio, FLAC):
            if audio.tags:
                result["title"] = audio.tags.get("title", [""])[0]
                result["artist"] = audio.tags.get("artist", [""])[0]
                result["album"] = audio.tags.get("album", [""])[0]

        elif ext in ('.ogg', '.oga', '.spx') or isinstance(audio, OggVorbis):
            if audio.tags:
                result["title"] = audio.tags.get("title", [""])[0]
                result["artist"] = audio.tags.get("artist", [""])[0]
                result["album"] = audio.tags.get("album", [""])[0]

        elif ext == '.opus' or isinstance(audio, OggOpus):
            if audio.tags:
                result["title"] = audio.tags.get("title", [""])[0]
                result["artist"] = audio.tags.get("artist", [""])[0]
                result["album"] = audio.tags.get("album", [""])[0]

        elif ext in ('.m4a', '.mp4') or isinstance(audio, MP4):
            if audio.tags:
                result["title"] = audio.tags.get("\xa9nam", [""])[0]
                result["artist"] = audio.tags.get("\xa9ART", [""])[0]
                result["album"] = audio.tags.get("\xa9alb", [""])[0]

        elif ext == '.wma' or isinstance(audio, ASF):
            if audio.tags:
                result["title"] = audio.tags.get("Title", [""])[0]
                result["artist"] = audio.tags.get("Author", [""])[0]
                result["album"] = audio.tags.get("WM/AlbumTitle", [""])[0]

        elif ext == '.wav' or isinstance(audio, WAVE):
            # WAV 通常没有标签，但 mutagen 可能支持 ID3
            if hasattr(audio, 'tags') and audio.tags:
                result["title"] = str(audio.tags.get("TIT2", ""))
                result["artist"] = str(audio.tags.get("TPE1", ""))
                result["album"] = str(audio.tags.get("TALB", ""))

        else:
            # 通用回退
            if hasattr(audio, 'tags') and audio.tags:
                # 尝试常见的 Vorbis 风格标签
                tags = audio.tags
                if hasattr(tags, 'getall'):
                    # VorbisComment
                    def get_vorbis(field):
                        vals = tags.getall(field)
                        return vals[0] if vals else ""
                    result["title"] = get_vorbis("title")
                    result["artist"] = get_vorbis("artist")
                    result["album"] = get_vorbis("album")
                elif hasattr(tags, 'get'):
                    # ID3 风格
                    result["title"] = str(tags.get("TIT2", ""))
                    result["artist"] = str(tags.get("TPE1", ""))
                    result["album"] = str(tags.get("TALB", ""))

    except Exception as e:
        # 读取失败，返回空
        pass

    # 清理值（去除首尾空白，处理 "None" 字符串）
    for key in result:
        val = result[key]
        if val is None or str(val).lower() == "none":
            result[key] = ""
        else:
            result[key] = str(val).strip()
            # 处理 mutagen 返回的带引号字符串，如 "TIT2('title')"
            if result[key].startswith("TIT2(") or result[key].startswith("TPE1(") or result[key].startswith("TALB("):
                # 提取括号内的内容
                import re
                m = re.search(r"\((.*?)\)$", result[key])
                if m:
                    result[key] = m.group(1).strip("\'\"")

    return result


def has_complete_metadata(meta: dict) -> bool:
    """检查是否包含完整的标题、艺术家、专辑信息"""
    return all(meta.get(k, "").strip() for k in ("title", "artist", "album"))


def safe_move(src: Path, dst_dir: Path, dry_run: bool = False) -> Path:
    """
    安全移动文件到目标目录，处理文件名冲突。
    返回最终的目标路径。
    """
    dst = dst_dir / src.name

    # 处理文件名冲突
    counter = 1
    stem = src.stem
    suffix = src.suffix
    while dst.exists():
        dst = dst_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    if not dry_run:
        shutil.move(str(src), str(dst))

    return dst


def main(dry_run: bool = False):
    print("=" * 60)
    print("音频文件整理脚本")
    print(f"扫描目录: {SOURCE_DIR}")
    print(f"目标目录: {TARGET_DIR}")
    print(f"模式: {'模拟运行 (dry-run)' if dry_run else '实际执行'}")
    print("=" * 60)

    if not SOURCE_DIR.exists():
        print(f"错误：源目录不存在: {SOURCE_DIR}")
        sys.exit(1)

    # 创建目标目录
    if not dry_run:
        TARGET_DIR.mkdir(parents=True, exist_ok=True)

    # 收集结果
    moved_files = []      # 成功移动的文件
    skipped_files = []    # 元数据不完整的文件
    error_files = []      # 读取失败的文件

    # 递归扫描
    all_files = list(SOURCE_DIR.rglob("*"))
    audio_files = [f for f in all_files if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS]

    print(f"\n共发现 {len(audio_files)} 个音频文件，开始扫描...\n")

    for idx, filepath in enumerate(audio_files, 1):
        rel_path = filepath.relative_to(SOURCE_DIR)
        print(f"[{idx}/{len(audio_files)}] 扫描: {rel_path}", end=" ")

        try:
            meta = get_audio_metadata(filepath)

            if has_complete_metadata(meta):
                print(f"[✓ 完整] {meta['title']} - {meta['artist']} | {meta['album']}")

                if not dry_run:
                    final_path = safe_move(filepath, TARGET_DIR)
                else:
                    final_path = TARGET_DIR / filepath.name

                moved_files.append({
                    "original_path": str(filepath),
                    "new_path": str(final_path),
                    "filename": final_path.name,
                    "title": meta["title"],
                    "artist": meta["artist"],
                    "album": meta["album"],
                })
            else:
                missing = [k for k in ("title", "artist", "album") if not meta.get(k, "").strip()]
                print(f"[✗ 缺失: {', '.join(missing)}]")
                skipped_files.append({
                    "path": str(filepath),
                    "missing": ", ".join(missing),
                    "title": meta["title"],
                    "artist": meta["artist"],
                    "album": meta["album"],
                })

        except Exception as e:
            print(f"[✗ 错误: {e}]")
            error_files.append({
                "path": str(filepath),
                "error": str(e),
            })

    # 生成报告
    print("\n" + "=" * 60)
    print("处理完成，生成报告...")

    if not dry_run:
        # 写入 CSV 报告
        with open(REPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "序号", "文件名", "标题", "艺术家", "专辑", "原始路径", "新路径"
            ])
            writer.writeheader()
            for i, item in enumerate(moved_files, 1):
                writer.writerow({
                    "序号": i,
                    "文件名": item["filename"],
                    "标题": item["title"],
                    "艺术家": item["artist"],
                    "专辑": item["album"],
                    "原始路径": item["original_path"],
                    "新路径": item["new_path"],
                })
        print(f"\n列表文件已保存: {REPORT_FILE}")

    # 统计输出
    print(f"\n{'='*60}")
    print("统计结果:")
    print(f"  音频文件总数: {len(audio_files)}")
    print(f"  已移动(完整元数据): {len(moved_files)}")
    print(f"  已跳过(元数据缺失): {len(skipped_files)}")
    print(f"  读取错误: {len(error_files)}")
    print(f"{'='*60}")

    if moved_files:
        print(f"\n已移动文件预览 (前10条):")
        for item in moved_files[:10]:
            print(f"  • {item['title']} - {item['artist']} ({item['album']})")
        if len(moved_files) > 10:
            print(f"  ... 还有 {len(moved_files)-10} 条")

    if skipped_files and len(skipped_files) <= 20:
        print(f"\n跳过文件预览:")
        for item in skipped_files[:10]:
            print(f"  • {item['path']} (缺失: {item['missing']})")

    return moved_files, skipped_files, error_files


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="整理音频文件")
    parser.add_argument("--dry-run", action="store_true", 
                        help="模拟运行，不实际移动文件")
    args = parser.parse_args()

    main(dry_run=args.dry_run)
