#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频文件信息提取脚本
功能：递归扫描音频文件，提取歌名和歌手信息，输出为 Python tuple 格式。
输出格式：("歌曲名", "歌手"),  如果只有一项信息则默认为歌名：("歌名", "")
"""

import os
import sys
from pathlib import Path

try:
    from mutagen import File
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp4 import MP4
    from mutagen.asf import ASF
    from mutagen.wave import WAVE
except ImportError:
    print("错误：请先安装 mutagen 库")
    print("  pip3 install mutagen")
    sys.exit(1)


# ==================== 配置 ====================
SOURCE_DIR = Path("/home/hanxiao/Music/163")
OUTPUT_FILE = Path("/home/hanxiao/Music/song_list.py")
SUPPORTED_EXTS = {
    '.mp3', '.flac', '.ogg', '.opus', '.m4a', '.mp4',
    '.wav', '.wma', '.aac', '.oga', '.spx'
}


def get_title_artist(filepath: Path) -> tuple:
    """
    提取音频文件的标题和艺术家。
    返回 (title, artist)，若不存在则为空字符串。
    """
    title = ""
    artist = ""

    try:
        audio = File(str(filepath))
        if audio is None:
            return ("", "")

        ext = filepath.suffix.lower()
        tags = getattr(audio, 'tags', None)

        if ext == '.mp3' or isinstance(audio, MP3):
            if tags:
                title = str(tags.get("TIT2", ""))
                artist = str(tags.get("TPE1", ""))

        elif ext == '.flac' or isinstance(audio, FLAC):
            if tags:
                title = tags.get("title", [""])[0]
                artist = tags.get("artist", [""])[0]

        elif ext in ('.ogg', '.oga', '.spx') or isinstance(audio, OggVorbis):
            if tags:
                title = tags.get("title", [""])[0]
                artist = tags.get("artist", [""])[0]

        elif ext == '.opus' or isinstance(audio, OggOpus):
            if tags:
                title = tags.get("title", [""])[0]
                artist = tags.get("artist", [""])[0]

        elif ext in ('.m4a', '.mp4') or isinstance(audio, MP4):
            if tags:
                title = tags.get("\xa9nam", [""])[0]
                artist = tags.get("\xa9ART", [""])[0]

        elif ext == '.wma' or isinstance(audio, ASF):
            if tags:
                title = tags.get("Title", [""])[0]
                artist = tags.get("Author", [""])[0]

        elif ext == '.wav' or isinstance(audio, WAVE):
            if tags:
                title = str(tags.get("TIT2", ""))
                artist = str(tags.get("TPE1", ""))

        else:
            if tags:
                if hasattr(tags, 'getall'):
                    def get_vorbis(field):
                        vals = tags.getall(field)
                        return vals[0] if vals else ""
                    title = get_vorbis("title")
                    artist = get_vorbis("artist")
                elif hasattr(tags, 'get'):
                    title = str(tags.get("TIT2", ""))
                    artist = str(tags.get("TPE1", ""))

    except Exception:
        pass

    # 清理值
    def clean(val):
        if val is None or str(val).lower() == "none":
            return ""
        s = str(val).strip()
        # 处理 mutagen 返回的类似 TIT2('title') 的格式
        import re
        if s.startswith("TIT2(") or s.startswith("TPE1(") or s.startswith("TALB("):
            m = re.search(r"\((.*?)\)$", s)
            if m:
                s = m.group(1).strip("\'\"")
        return s

    return (clean(title), clean(artist))


def main():
    print("=" * 50)
    print("音频文件信息提取")
    print(f"扫描目录: {SOURCE_DIR}")
    print(f"输出文件: {OUTPUT_FILE}")
    print("=" * 50)

    if not SOURCE_DIR.exists():
        print(f"错误：源目录不存在: {SOURCE_DIR}")
        sys.exit(1)

    # 递归扫描音频文件
    audio_files = [
        f for f in SOURCE_DIR.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
    ]

    print(f"\n共发现 {len(audio_files)} 个音频文件，开始提取...\n")

    results = []
    no_meta = []

    for idx, filepath in enumerate(audio_files, 1):
        rel_path = filepath.relative_to(SOURCE_DIR)
        title, artist = get_title_artist(filepath)

        # 如果只有一个信息，默认为歌名
        if title and not artist:
            # 只有歌名
            song_name = title
            singer = ""
        elif artist and not title:
            # 只有歌手，按用户要求"只有一个信息默认为歌名"
            # 但这里只有歌手，没有歌名，我们把歌手当歌名？还是留空？
            # 用户说"如果只有一个信息默认为歌名"，所以只有歌手时，歌手作为歌名
            song_name = artist
            singer = ""
        elif title and artist:
            song_name = title
            singer = artist
        else:
            # 两个都没有，尝试从文件名推断
            song_name = filepath.stem
            singer = ""
            no_meta.append(str(rel_path))
            print(f"[{idx}/{len(audio_files)}] ⚠ 无元数据，使用文件名: {rel_path}")
            results.append((song_name, singer))
            continue

        print(f"[{idx}/{len(audio_files)}] ✓ {song_name} - {singer or '(无歌手)'}")
        results.append((song_name, singer))

    # 去重（保持顺序）
    seen = set()
    unique_results = []
    for item in results:
        if item not in seen:
            seen.add(item)
            unique_results.append(item)

    # 写入输出文件
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("# 自动生成的歌单\n")
        f.write(f"# 扫描目录: {SOURCE_DIR}\n")
        f.write(f"# 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# 总计: {len(unique_results)} 首\n\n")
        f.write("song_list = [\n")
        for song_name, singer in unique_results:
            # 处理字符串中的引号，使用转义
            safe_name = song_name.replace('"', '\\"')
            safe_singer = singer.replace('"', '\\"')
            f.write(f'  ("{safe_name}", "{safe_singer}"),\n')
        f.write("]\n")

    print(f"\n{'='*50}")
    print(f"提取完成！")
    print(f"  音频文件总数: {len(audio_files)}")
    print(f"  有效记录: {len(unique_results)} 首")
    print(f"  无元数据(使用文件名): {len(no_meta)} 首")
    print(f"  输出文件: {OUTPUT_FILE}")
    print(f"{'='*50}")

    if unique_results:
        print(f"\n预览 (前10条):")
        for item in unique_results[:10]:
            print(f"  {item}")
        if len(unique_results) > 10:
            print(f"  ... 还有 {len(unique_results)-10} 条")


if __name__ == "__main__":
    main()
