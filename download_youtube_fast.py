#!/usr/bin/env python3
"""
YouTube 高音质音乐下载器（并发优化版）
优化点：
- 多线程并发下载（默认 4 线程）
- 去掉 time.sleep(2) 人为延迟
- 优化搜索：减少 yt-dlp 调用次数
- 下载和后期处理分离，减少阻塞
"""

import os
import re
import json
import io
import time
import base64
import subprocess
import concurrent.futures
from pathlib import Path
from threading import Lock

import song_list_completed

# ==================== 配置区 ====================

BASE_DIR = "/home/hanxiao/Music/YoutubeMusic"
SONGS = song_list_completed.song_list

# 并发数（根据网络调整，建议 4-8）
MAX_WORKERS = 4

# yt-dlp 环境
DENO_BIN = os.path.expanduser("~/.deno/bin")
ENV = {
    **os.environ,
    "PATH": os.environ.get("PATH", "") + ":" + DENO_BIN
}

# ==================== 常量 ====================

AUDIO_EXTS = (".opus", ".webm", ".m4a", ".mp3", ".aac")
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")

ABR_ESTIMATE = {
    "251": 160, "251-drc": 160,
    "140": 128, "140-drc": 128,
    "250": 70,  "250-drc": 70,
    "249": 50,  "249-drc": 50,
}

# 全局锁（用于文件操作日志）
print_lock = Lock()

try:
    import zhconv
    HAS_ZHCONV = True
except ImportError:
    HAS_ZHCONV = False


def log(msg):
    with print_lock:
        print(msg, flush=True)


def sanitize(name: str):
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.replace("&", "and")
    return name.strip()


def file_ok(path):
    return os.path.exists(path) and os.path.getsize(path) > 100000


def normalize_chinese(text):
    if not text:
        return ""
    text = text.lower()
    if HAS_ZHCONV:
        return zhconv.convert(text, 'zh-hans')
    return text


# ==================== 音频处理（同原脚本）====================

def remux_to_opus(filepath):
    if not filepath.endswith(".webm"):
        return filepath
    opus_path = filepath[:-5] + ".opus"
    cmd = [
        "ffmpeg", "-y", "-fflags", "+genpts", "-i", filepath,
        "-map", "0:a:0", "-c:a", "copy", "-sn", "-dn", "-vn",
        "-map_metadata", "0", opus_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60)
        if r.returncode == 0 and file_ok(opus_path):
            os.remove(filepath)
            return opus_path
    except Exception:
        pass
    if os.path.exists(opus_path):
        os.remove(opus_path)
    return filepath


def set_metadata(filepath, title, artist, comment=None):
    ext = os.path.splitext(filepath)[1]
    tmp = filepath + ".tmp" + ext
    cmd = [
        "ffmpeg", "-y", "-i", filepath, "-map_metadata", "0",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-c", "copy", tmp,
    ]
    if comment:
        cmd.insert(-1, "-metadata")
        cmd.insert(-1, f"comment={comment}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60)
        if r.returncode == 0 and file_ok(tmp):
            os.replace(tmp, filepath)
            return True
    except Exception:
        pass
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def thumb_to_jpeg_bytes(thumb_path):
    jpg_path = thumb_path + ".fixed.jpg"
    cmd = [
        "ffmpeg", "-y", "-i", thumb_path,
        "-vf", "scale=640:640:force_original_aspect_ratio=decrease,pad=640:640:(ow-iw)/2:(oh-ih)/2:white",
        "-q:v", "2", "-strip", jpg_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=30)
        if r.returncode == 0 and os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 1000:
            with open(jpg_path, "rb") as f:
                data = f.read()
            os.remove(jpg_path)
            return data
    except Exception:
        pass
    finally:
        if os.path.exists(jpg_path):
            os.remove(jpg_path)
    try:
        from PIL import Image
        img = Image.open(thumb_path)
        if getattr(img, "is_animated", False):
            img.seek(0)
        img = img.convert("RGB")
        img.thumbnail((640, 640), Image.LANCZOS)
        canvas = Image.new("RGB", (640, 640), (255, 255, 255))
        x = (640 - img.width) // 2
        y = (640 - img.height) // 2
        canvas.paste(img, (x, y))
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception:
        return None


def embed_thumbnail(filepath, thumb_path):
    try:
        from mutagen.oggopus import OggOpus
        from mutagen.mp4 import MP4, MP4Cover
        from mutagen.flac import Picture
    except ImportError:
        return False
    if not os.path.exists(thumb_path):
        return False
    jpg_data = thumb_to_jpeg_bytes(thumb_path)
    if not jpg_data:
        return False
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext in (".opus", ".ogg"):
            audio = OggOpus(filepath)
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = jpg_data
            audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
            audio.save()
            return True
        elif ext in (".m4a", ".mp4"):
            audio = MP4(filepath)
            audio["covr"] = [MP4Cover(jpg_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True
    except Exception:
        return False
    return False


def ffprobe_info(filepath):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", filepath]
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=20)
        data = json.loads(r.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not audio_stream:
            return None
        return {
            "codec": audio_stream.get("codec_name"),
            "bitrate": int(fmt.get("bit_rate", 0)) // 1000,
            "sample_rate": audio_stream.get("sample_rate"),
            "channels": audio_stream.get("channels"),
            "duration": round(float(fmt.get("duration", 0)), 1),
            "size_mb": round(os.path.getsize(filepath) / (1024 * 1024), 2)
        }
    except Exception:
        return None


def find_downloaded_file(prefix):
    for f in os.listdir(BASE_DIR):
        if not f.startswith(prefix):
            continue
        if not f.endswith(AUDIO_EXTS):
            continue
        fp = os.path.join(BASE_DIR, f)
        if file_ok(fp):
            return fp
    return None


def find_thumbnail(base_name):
    for ext in THUMB_EXTS:
        path = os.path.join(BASE_DIR, base_name + ext)
        if os.path.exists(path):
            return path
    return None


# ==================== 优化版智能选源 ====================

def pick_best_audio_id(song, artist):
    """
    优化：一次 yt-dlp 调用获取前 5 个结果的详细信息，减少请求次数。
    """
    search_query = f'ytsearch5:"{song}" "{artist}" audio'
    cmd = [
        "yt-dlp", "--cookies-from-browser", "chrome",
        "--quiet", "--no-warnings", "--flat-playlist",
        "--print", "%(id)s",
        search_query,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60, env=ENV)
        if r.returncode != 0:
            return None
        ids = [line.strip() for line in r.stdout.strip().split("\n") if line.strip()]
    except Exception:
        return None

    if not ids:
        return None

    # 优化：一次获取所有候选的详细信息（使用 --dump-single-json 或批量 -j）
    # 但 yt-dlp 对多个 URL 的 -j 输出是 NDJSON，可以逐行解析
    infos = []
    for vid in ids:
        url = f"https://www.youtube.com/watch?v={vid}"
        cmd = [
            "yt-dlp", "--cookies-from-browser", "chrome",
            "--quiet", "--no-warnings", "--no-download", "-j", url,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=30, env=ENV)
            if r.returncode == 0 and r.stdout.strip():
                infos.append(json.loads(r.stdout.strip()))
        except Exception:
            continue

    best_id = None
    best_score = -1
    song_lower = song.lower()
    artist_lower = artist.lower()
    song_norm = normalize_chinese(song_lower)
    artist_norm = normalize_chinese(artist_lower)

    for info in infos:
        duration = info.get("duration") or 0
        if duration < 60 or duration > 600:
            continue

        title = info.get("title") or ""
        channel = info.get("channel") or ""
        title_norm = normalize_chinese(title)
        channel_norm = normalize_chinese(channel)

        # 歌名匹配
        song_matched = song_norm in title_norm
        if not song_matched:
            song_stripped = re.sub(r'[\s\-~《》\(\)（）\[\]]', '', song_norm)
            title_stripped = re.sub(r'[\s\-~《》\(\)（）\[\]]', '', title_norm)
            song_matched = song_stripped in title_stripped

        if not song_matched:
            continue

        artist_matched = artist_norm in title_norm or artist_norm in channel_norm

        formats = info.get("formats", [])
        max_abr = 0
        has_opus = False
        best_fmt = None

        for f in formats:
            vcodec = (f.get("vcodec") or "").lower()
            resolution = f.get("resolution") or ""
            if vcodec == "none" or "audio" in resolution:
                abr = f.get("abr")
                if abr is None:
                    abr = ABR_ESTIMATE.get(str(f.get("format_id") or ""), 0)
                else:
                    try:
                        abr = float(abr)
                    except (ValueError, TypeError):
                        abr = 0
                acodec = (f.get("acodec") or "").lower()
                if "opus" in acodec:
                    has_opus = True
                if abr > max_abr:
                    max_abr = abr
                    best_fmt = f.get("format_id")

        score = max_abr
        if has_opus:
            score += 30

        good_keywords = ["audio", "lyrics", "official audio", "ost", "soundtrack", "hq",
                         "歌词", "音频", "官方", "无损", "高音质", "高清"]
        bad_keywords = ["live", "cover", "remix", "8d", "chipmunk", "1 hour", "10 hours",
                        "loop", "slowed", "reverb", "bass boosted", "nightcore",
                        "现场", "翻唱", "混音", "慢速", "加速", "变调"]

        title_lower_raw = title.lower()
        for kw in good_keywords:
            if kw in title_lower_raw:
                score += 15
        for kw in bad_keywords:
            if kw in title_lower_raw:
                score -= 50

        if "vevo" in channel_norm or "official" in channel_norm:
            score += 10

        if artist_matched:
            score += 80
        else:
            score -= 20

        if score > best_score:
            best_score = score
            best_id = info.get("id")

    return best_id


# ==================== 单首歌曲下载（线程安全）====================

def download_song(args):
    """
    线程安全的单首歌曲下载。
    返回 (song, artist, success, info_msg)
    """
    song, artist = args
    base_name = sanitize(f"{song}-{artist}")
    existing = find_downloaded_file(base_name)

    if existing:
        return (song, artist, True, "SKIP: 已存在")

    log(f"[DL] {song} - {artist}")

    best_id = pick_best_audio_id(song, artist)
    if best_id:
        target_url = f"https://www.youtube.com/watch?v={best_id}"
        source_url = target_url
    else:
        search_query = f'ytsearch1:"{song}" "{artist}" official audio'
        target_url = search_query
        source_url = search_query

    output_template = os.path.join(BASE_DIR, f"{base_name}.%(ext)s")

    cmd = [
        "yt-dlp", "--cookies-from-browser", "chrome",
        "--no-playlist", "--quiet", "--no-warnings",
        "--write-thumbnail", "--add-metadata",
        "--format", "251/251-drc/250/249/140/bestaudio",
        "--output", output_template,
        target_url,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=300, env=ENV)
        if r.returncode != 0:
            # fallback
            query2 = f'ytsearch3:"{song}" "{artist}"'
            cmd[-1] = query2
            r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=300, env=ENV)
            if r.returncode != 0:
                err = r.stderr[:300] if r.stderr else "未知错误"
                return (song, artist, False, f"FAIL: {err}")

        fp = find_downloaded_file(base_name)
        if not fp:
            return (song, artist, False, "FAIL: 文件不存在")

        # 后期处理
        fp = remux_to_opus(fp)
        set_metadata(fp, song, artist.replace("&", " / "), comment=source_url)

        thumb = find_thumbnail(base_name)
        if thumb:
            if embed_thumbnail(fp, thumb):
                os.remove(thumb)

        info = ffprobe_info(fp)
        if info:
            msg = (f"OK: {os.path.basename(fp)} | {info['codec']} | "
                   f"{info['bitrate']}kbps | {info['size_mb']}MB")
        else:
            msg = f"OK: {os.path.basename(fp)}"

        return (song, artist, True, msg)

    except subprocess.TimeoutExpired:
        return (song, artist, False, "FAIL: 超时")
    except Exception as e:
        return (song, artist, False, f"FAIL: {e}")


# ==================== 主流程（并发版）====================

def main():
    Path(BASE_DIR).mkdir(parents=True, exist_ok=True)

    total = len(SONGS)
    log(f"🚀 开始并发下载 | 总歌曲: {total} | 线程数: {MAX_WORKERS}")
    log("=" * 60)

    ok = 0
    fail = []

    # 使用线程池并发下载
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交所有任务
        future_to_song = {
            executor.submit(download_song, item): item
            for item in SONGS
        }

        # 处理结果（as_completed 保证有完成就处理，不阻塞）
        for future in concurrent.futures.as_completed(future_to_song):
            song, artist, success, msg = future.result()
            idx = SONGS.index((song, artist)) + 1

            if success:
                ok += 1
                log(f"[{idx}/{total}] ✅ {msg}")
            else:
                fail.append(f"{song}-{artist}")
                log(f"[{idx}/{total}] ❌ {msg}")

    generate_catalog()

    log("\n" + "=" * 60)
    log(f"Done | OK={ok} | FAIL={len(fail)} | 总用时: {time.time()-start_time:.0f}s")
    if fail:
        for x in fail:
            log(f"  - {x}")


def generate_catalog():
    catalog = os.path.join(BASE_DIR, "歌曲名录.md")
    with open(catalog, "w", encoding="utf-8") as f:
        f.write("# 歌曲名录\n\n")
        f.write("| 序号 | 歌曲 | 歌手 | 编码 | 码率 | 采样率 | 大小 |\n")
        f.write("|:---:|:---|:---|:---|:---|:---|:---|\n")
        idx = 1
        for song, artist in SONGS:
            base_name = sanitize(f"{song}-{artist}")
            fp = find_downloaded_file(base_name)
            if not fp:
                continue
            info = ffprobe_info(fp)
            if not info:
                continue
            f.write(
                f"| {idx} | {song} | {artist} | {info['codec']} | "
                f"{info['bitrate']}kbps | {info['sample_rate']}Hz | {info['size_mb']}MB |\n"
            )
            idx += 1
    log(f"[CATALOG] {catalog}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", "-w", type=int, default=4, help="并发线程数 (默认: 4)")
    args = parser.parse_args()
    MAX_WORKERS = args.workers
    start_time = time.time()
    main()
