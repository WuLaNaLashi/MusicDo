#!/usr/bin/env python3
"""Analyze MP3 files in a folder, detect duplicates by song name and artist."""
import os
import re
import subprocess
import json
import shutil
from collections import defaultdict
from datetime import datetime

def parse_filename(filename):
    """Extract song title and artist from filename."""
    # Remove extension
    name = os.path.splitext(filename)[0]

    # Remove [Pxxx] prefix like [P063], [P039], etc.
    name = re.sub(r'^\[P\d+\]', '', name)

    # Remove 【xxx】 prefixes
    name = re.sub(r'^【[^】]*】', '', name)

    # Remove leading numbers like "063.", "39.", "66. "
    name = re.sub(r'^\d+\.\s*', '', name)

    # Check if wrapped in 书名号 《...》
    m = re.match(r'^《([^》]+)》', name)
    if m:
        return m.group(1).strip(), ""

    # Pattern: "歌曲名-歌手"
    if '-' in name:
        parts = name.split('-', 1)
        part1 = parts[0].strip()
        part2 = parts[1].strip() if len(parts) > 1 else ""

        # Can't determine which is song or artist, try both orders
        # Return both possibilities for comparison
        return part1, part2

    # Just song name (no artist)
    return name.strip(), ""

def get_file_info(filepath):
    """Get file metadata: size, date, duration, bitrate."""
    info = {
        'path': filepath,
        'filename': os.path.basename(filepath),
        'size': os.path.getsize(filepath),
        'date': datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%Y-%m-%d %H:%M:%S'),
    }

    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', filepath],
            capture_output=True, text=True, timeout=10
        )
        fmt = json.loads(result.stdout)['format']
        info['duration'] = float(fmt.get('duration', 0))
        info['bitrate'] = int(fmt.get('bit_rate', 0)) // 1000
        info['title'] = fmt.get('tags', {}).get('title', '')
        info['artist'] = fmt.get('tags', {}).get('artist', '')
    except Exception as e:
        info['duration'] = 0
        info['bitrate'] = 0
        info['title'] = ''
        info['artist'] = ''
        info['error'] = str(e)

    return info

def format_duration(seconds):
    """Format seconds to mm:ss."""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"

def format_size(bytes):
    """Format bytes to MB."""
    return f"{bytes / (1024*1024):.2f}MB"

def normalize_name(name):
    """Normalize name for comparison (lowercase, remove spaces)."""
    if not name:
        return ""
    return re.sub(r'\s+', '', name.lower())

def find_duplicates(directory):
    """Find duplicate songs in directory."""
    # Group by (normalized_song, normalized_artist)
    groups = defaultdict(list)

    for f in os.listdir(directory):
        if not f.lower().endswith('.mp3'):
            continue

        filepath = os.path.join(directory, f)
        if not os.path.isfile(filepath):
            continue

        song, artist = parse_filename(f)
        info = get_file_info(filepath)
        info['parsed_song'] = song
        info['parsed_artist'] = artist

        # Create key for grouping (normalized)
        # Try both orders: song-artist and artist-song
        key1 = (normalize_name(song), normalize_name(artist))
        key2 = (normalize_name(artist), normalize_name(song))

        # Also check embedded metadata if available
        meta_key = (normalize_name(info.get('title', '')), normalize_name(info.get('artist', '')))

        # Use the key that matches existing groups, or create new
        found_key = None
        for k in [key1, key2, meta_key]:
            if k in groups and k != ('', ''):
                found_key = k
                break

        if found_key:
            groups[found_key].append(info)
        else:
            # Use key1 as primary
            if key1 != ('', ''):
                groups[key1].append(info)

    # Find groups with more than one file
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    return duplicates

def print_file_info(info, index):
    """Print file info for comparison."""
    print(f"\n[{index}] {info['filename']}")
    print(f"    路径: {info['path']}")
    print(f"    大小: {format_size(info['size'])}")
    print(f"    日期: {info['date']}")
    print(f"    时长: {format_duration(info['duration'])}")
    print(f"    码率: {info['bitrate']}kbps")
    print(f"    解析: 歌曲='{info['parsed_song']}' 歌手='{info['parsed_artist']}'")
    if info.get('title') or info.get('artist'):
        print(f"    元数据: title='{info.get('title', '')}' artist='{info.get('artist', '')}'")

def move_to_del(filepath, del_dir):
    """Move file to del directory instead of deleting."""
    if not os.path.exists(del_dir):
        os.makedirs(del_dir)

    filename = os.path.basename(filepath)
    target = os.path.join(del_dir, filename)

    # If target exists, add timestamp
    if os.path.exists(target):
        base = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1]
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        target = os.path.join(del_dir, f"{base}_{timestamp}{ext}")

    shutil.move(filepath, target)
    return target

def main():
    import sys

    if len(sys.argv) > 1:
        directory = sys.argv[1]
    else:
        directory = os.getcwd()

    if not os.path.isdir(directory):
        print(f"错误: 目录不存在 - {directory}")
        return

    del_dir = os.path.join(directory, "del")

    print(f"分析目录: {directory}")
    print("=" * 60)

    duplicates = find_duplicates(directory)

    if not duplicates:
        print("\n未发现重复歌曲。")
        return

    total_duplicates = len(duplicates)
    print(f"\n发现 {total_duplicates} 组可能重复的歌曲:\n")

    processed = 0
    moved_files = []

    for (song_key, artist_key), files in duplicates.items():
        processed += 1
        song_display = song_key if song_key else "(未知歌曲)"
        artist_display = artist_key if artist_key else "(未知歌手)"

        print(f"\n{'='*60}")
        print(f"[{processed}/{total_duplicates}] 可能重复: 歌曲='{song_display}' 歌手='{artist_display}'")
        print(f"发现 {len(files)} 个文件:")

        # Print all files in this group
        for i, info in enumerate(files, 1):
            print_file_info(info, i)

        # Let user decide
        while True:
            print(f"\n操作选项:")
            print(f"  1-n: 移动第 n 个文件到 del 文件夹")
            print(f"  a: 移动当前组所有文件到 del 文件夹")
            print(f"  s: 跳过这组，不做任何操作")
            print(f"  q: 退出程序")

            choice = input("\n请选择操作: ").strip().lower()

            if choice == 'q':
                print("\n用户中止。")
                break
            elif choice == 's':
                print("跳过此组。")
                break
            elif choice == 'a':
                # Move all files to del
                for info in files:
                    target = move_to_del(info['path'], del_dir)
                    moved_files.append(target)
                    print(f"已移动: {info['filename']} -> del/{os.path.basename(target)}")
                break
            elif choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(files):
                    info = files[idx - 1]
                    target = move_to_del(info['path'], del_dir)
                    moved_files.append(target)
                    print(f"\n已移动: {info['filename']} -> del/{os.path.basename(target)}")

                    # Check if still has duplicates
                    remaining = [f for f in files if f['path'] != info['path']]
                    if len(remaining) > 1:
                        print(f"\n此组仍有 {len(remaining)} 个文件，继续检查...")
                        files = remaining
                        for i, f in enumerate(files, 1):
                            print_file_info(f, i)
                        continue
                    else:
                        print("此组已无重复。")
                        break
                else:
                    print(f"无效编号: {choice}，请输入 1-{len(files)}")
            else:
                print(f"无效选项: {choice}")

        if choice == 'q':
            break

    # Summary
    print(f"\n{'='*60}")
    print(f"检查完成!")
    if moved_files:
        print(f"已移动 {len(moved_files)} 个文件到 del 文件夹:")
        for f in moved_files:
            print(f"  - {f}")
    else:
        print("未移动任何文件。")

if __name__ == "__main__":
    main()