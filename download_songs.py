#!/usr/bin/env python3
"""
YouTube 高音质音乐下载器
目标：
- 保留 YouTube 原始最佳音频，不进行任何转码
- 智能选源：搜索多结果，比较音频码率，选最优版本
- 自动写 metadata（歌名/歌手/原始视频地址）+ 嵌入封面
- 自动将 webm 无损重封装为 opus
- 同时兼容 opus / m4a 两种音频容器
"""

import os
import re
import json
import io
import time
import base64
import subprocess
from pathlib import Path

# ==================== 配置区 ====================

BASE_DIR = "/home/hanxiao/Music/YoutubeMusic"

SONGS = [
    ("一剪梅", "费玉清"),
    ("离别的车站", "卓依婷"),
]

# yt-dlp 环境（如有 deno 等自定义 PATH 可在此追加）
DENO_BIN = os.path.expanduser("~/.deno/bin")
ENV = {
    **os.environ,
    "PATH": os.environ.get("PATH", "") + ":" + DENO_BIN
}

# ==================== 常量 ====================

AUDIO_EXTS = (".opus", ".webm", ".m4a", ".mp3", ".aac")
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# YouTube 音频 format_id 预估码率（当 yt-dlp 未提供 abr 时兜底）
ABR_ESTIMATE = {
    "251": 160, "251-drc": 160,
    "140": 128, "140-drc": 128,
    "250": 70,  "250-drc": 70,
    "249": 50,  "249-drc": 50,
}


# ==================== 工具函数 ====================

def log(msg):
    print(msg, flush=True)


def sanitize(name: str):
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.replace("&", "and")
    return name.strip()


def file_ok(path):
    return os.path.exists(path) and os.path.getsize(path) > 100000


# ==================== 音频处理 ====================

def remux_to_opus(filepath):
    """将 .webm 无损重封装为 .opus（Ogg Opus 容器），音质零损失"""
    if not filepath.endswith(".webm"):
        return filepath

    opus_path = filepath[:-5] + ".opus"
    cmd = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-map", "0:a",
        "-c:a", "copy",
        "-map_metadata", "0",
        opus_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and file_ok(opus_path):
            os.remove(filepath)
            log(f"[REMUX] {os.path.basename(opus_path)} (无损重封装)")
            return opus_path
        else:
            if os.path.exists(opus_path):
                os.remove(opus_path)
    except Exception:
        if os.path.exists(opus_path):
            os.remove(opus_path)
    return filepath


def set_metadata(filepath, title, artist, comment=None):
    """
    用 ffmpeg -c copy 写入歌名、歌手和原始视频地址（comment）。
    必须在 embed_thumbnail 之前执行，因为 ffmpeg 会重新封装文件。
    """
    ext = os.path.splitext(filepath)[1]
    tmp = filepath + ".tmp" + ext
    cmd = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-map_metadata", "0",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-c", "copy",
        tmp,
    ]
    # 【新增】写入原始视频地址到 comment 字段
    if comment:
        cmd.insert(-1, "-metadata")
        cmd.insert(-1, f"comment={comment}")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and file_ok(tmp):
            os.replace(tmp, filepath)
            return True
    except Exception:
        pass
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def thumb_to_jpeg_bytes(thumb_path):
    """
    将任意缩略图转为标准 JPEG bytes。
    先尝试 ffmpeg，失败则 fallback 到 Pillow（支持 animated webp 取第一帧）。
    输出 640x640 白色背景居中填充，质量 95%。
    """
    # 尝试 1: ffmpeg
    jpg_path = thumb_path + ".fixed.jpg"
    convert_cmd = [
        "ffmpeg", "-y",
        "-i", thumb_path,
        "-vf", "scale=640:640:force_original_aspect_ratio=decrease,"
               "pad=640:640:(ow-iw)/2:(oh-ih)/2:white",
        "-q:v", "2",
        "-strip",
        jpg_path,
    ]
    try:
        r = subprocess.run(convert_cmd, capture_output=True, text=True, timeout=30)
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

    # 尝试 2: Pillow fallback
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
    except Exception as e:
        log(f"[THUMB WARN] Pillow 转换失败: {e}")
        return None


def embed_thumbnail(filepath, thumb_path):
    """
    根据音频容器类型，使用 mutagen 写入对应格式的封面。
    - opus/ogg  -> OggOpus (METADATA_BLOCK_PICTURE)
    - m4a/mp4    -> MP4 (covr)
    """
    try:
        from mutagen.oggopus import OggOpus
        from mutagen.mp4 import MP4, MP4Cover
        from mutagen.flac import Picture
    except ImportError:
        log("[THUMB SKIP] mutagen 未安装 (pip install mutagen)")
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
            pic.type = 3  # Cover (front)
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

        else:
            log(f"[THUMB SKIP] 不支持的音频格式: {ext}")
            return False

    except Exception as e:
        log(f"[THUMB ERR] mutagen 写入失败: {e}")
        return False


def ffprobe_info(filepath):
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            filepath
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
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


# ==================== 智能选源 ====================

def pick_best_audio_id(song, artist):
    """
    搜索前 3 个结果，比较音频码率、编码、标题语义，返回最佳视频 ID。
    """
    search_query = f"ytsearch3:{song} {artist} audio"
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", "chrome",
        "--quiet", "--no-warnings",
        "--flat-playlist",
        "--print", "%(id)s",
        search_query,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=ENV)
        if r.returncode != 0:
            return None
        ids = [line.strip() for line in r.stdout.strip().split("\n") if line.strip()]
    except Exception:
        return None

    if not ids:
        return None

    best_id = None
    best_score = -1

    for vid in ids:
        url = f"https://www.youtube.com/watch?v={vid}"
        cmd = [
            "yt-dlp",
            "--cookies-from-browser", "chrome",
            "--quiet", "--no-warnings",
            "--no-download", "-j",
            url,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=ENV)
            if r.returncode != 0:
                continue
            info = json.loads(r.stdout.strip())
        except Exception:
            continue

        # 过滤：时长不在 1~10 分钟的直接跳过（排除直播、短视频、超长混音）
        duration = info.get("duration") or 0
        if duration < 60 or duration > 600:
            continue

        title = (info.get("title") or "").lower()
        channel = (info.get("channel") or "").lower()

        # 解析可用音频格式的最高码率
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

        # 语义评分
        score = max_abr
        if has_opus:
            score += 30

        good_keywords = ["audio", "lyrics", "official audio", "ost", "soundtrack", "hq"]
        bad_keywords = ["live", "cover", "remix", "8d", "chipmunk", "1 hour", "10 hours",
                        "loop", "slowed", "reverb", "bass boosted", "nightcore"]

        for kw in good_keywords:
            if kw in title:
                score += 15
        for kw in bad_keywords:
            if kw in title:
                score -= 50

        if "vevo" in channel or "official" in channel:
            score += 10

        log(f"  [CANDIDATE] {info.get('title','')[:45]:<<45} | "
            f"abr={max_abr}k fmt={best_fmt} | score={score}")

        if score > best_score:
            best_score = score
            best_id = vid

    if best_id:
        log(f"[PICK] 最佳源 ID: {best_id} (score={best_score})")
    return best_id


# ==================== 主流程 ====================

def download_song(song, artist):
    base_name = sanitize(f"{song}-{artist}")
    existing = find_downloaded_file(base_name)

    if existing:
        log(f"[SKIP] 已存在: {os.path.basename(existing)}")
        return True

    log(f"[DL] {song} - {artist}")

    # 智能选源，并确保始终拿到真实的 YouTube 视频地址用于 comment
    best_id = pick_best_audio_id(song, artist)
    if best_id:
        target_url = f"https://www.youtube.com/watch?v={best_id}"
        source_url = target_url
    else:
        # fallback：先解析搜索首条的真实 ID，再下载，确保 comment 是真实 URL
        search_query = f"ytsearch1:{song} {artist} official audio"
        cmd_id = [
            "yt-dlp",
            "--cookies-from-browser", "chrome",
            "--quiet", "--no-warnings",
            "--print", "%(id)s",
            search_query,
        ]
        try:
            r = subprocess.run(cmd_id, capture_output=True, text=True, timeout=30, env=ENV)
            fallback_id = r.stdout.strip().split("\n")[0].strip()
            if fallback_id:
                target_url = f"https://www.youtube.com/watch?v={fallback_id}"
                source_url = target_url
                log(f"[PICK] fallback 解析到 ID: {fallback_id}")
            else:
                target_url = search_query
                source_url = search_query
        except Exception:
            target_url = search_query
            source_url = search_query

    output_template = os.path.join(BASE_DIR, f"{base_name}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--cookies-from-browser", "chrome",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--write-thumbnail",
        "--add-metadata",
        "--format",
        "251/251-drc/250/249/140/bestaudio",
        "--output",
        output_template,
        target_url,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=ENV)
        if r.returncode != 0:
            log("[RETRY] fallback search")
            query2 = f"ytsearch3:{song} {artist}"
            cmd[-1] = query2
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=ENV)
            if r.returncode != 0:
                log(f"[FAIL] {r.stderr[:300]}")
                return False

        fp = find_downloaded_file(base_name)
        if not fp:
            log("[FAIL] 下载文件不存在")
            return False

        # 1) 无损重封装（仅 webm -> opus）
        fp = remux_to_opus(fp)

        # 2) 先写文字 metadata + 原始视频地址（ffmpeg 重新封装）
        meta_ok = set_metadata(
            fp,
            song,
            artist.replace("&", " / "),
            comment=source_url,  # 【新增】写入原始视频地址
        )
        if meta_ok:
            log(f"[META] 已写入歌名/歌手/来源: {source_url}")
        else:
            log("[META FAIL] 歌名/歌手写入失败")

        # 3) 再写封面（mutagen 原地修改，自动识别 opus/m4a）
        thumb = find_thumbnail(base_name)
        if thumb:
            if embed_thumbnail(fp, thumb):
                log("[THUMB] 已嵌入封面")
                os.remove(thumb)
            else:
                log(f"[THUMB FAIL] 保留缩略图: {os.path.basename(thumb)}")

        info = ffprobe_info(fp)
        if info:
            log(
                f"[OK] {os.path.basename(fp)} | "
                f"{info['codec']} | {info['bitrate']}kbps | "
                f"{info['sample_rate']}Hz | {info['size_mb']}MB"
            )

        return True

    except subprocess.TimeoutExpired:
        log("[TIMEOUT]")
        return False
    except Exception as e:
        log(f"[ERR] {e}")
        return False


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


def main():
    Path(BASE_DIR).mkdir(parents=True, exist_ok=True)

    ok = 0
    fail = []

    for i, (song, artist) in enumerate(SONGS, 1):
        log(f"\n--- [{i}/{len(SONGS)}] ---")
        if download_song(song, artist):
            ok += 1
        else:
            fail.append(f"{song}-{artist}")
        time.sleep(2)

    generate_catalog()

    log("\n" + "=" * 60)
    log(f"Done | OK={ok} | FAIL={len(fail)}")
    if fail:
        for x in fail:
            log(f"  - {x}")


if __name__ == "__main__":
    main()