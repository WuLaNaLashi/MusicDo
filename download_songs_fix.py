#!/usr/bin/env python3
"""
YouTube 高音质音乐下载器
目标：
- 保留 YouTube 原始最佳音频，不进行任何转码
- 智能选源：搜索多结果，比较音频码率，选最优版本
- 强制歌名+歌手匹配：防止下载成其他歌曲/翻唱版本
- 繁简兼容：支持繁体中文标题识别
- 自动写 metadata（歌名/歌手/原始视频地址）+ 嵌入封面
- 自动将 webm 无损重封装为 opus
- 同时兼容 opus / m4a 两种音频容器
- hxg 有时会匹配的文件可能不符合预期
"""

import os
import re
import json
import io
import time
import base64
import subprocess
from pathlib import Path

# import song_list
import song_list_completed

# ==================== 配置区 ====================

BASE_DIR = "/home/hanxiao/Music/YoutubeMusic"

SONGS = song_list_completed.song_list

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

# 繁简转换（可选依赖，pip install zhconv）
try:
    import zhconv
    HAS_ZHCONV = True
except ImportError:
    HAS_ZHCONV = False
    log("[INIT WARN] zhconv 未安装，繁简转换不可用 (pip install zhconv)")


# ==================== 工具函数 ====================

def log(msg):
    print(msg, flush=True)


def sanitize(name: str):
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.replace("&", "and")
    return name.strip()


def file_ok(path):
    return os.path.exists(path) and os.path.getsize(path) > 100000


def normalize_chinese(text):
    """统一转为简体（如果 zhconv 可用）"""
    if not text:
        return ""
    text = text.lower()
    if HAS_ZHCONV:
        return zhconv.convert(text, 'zh-hans')
    return text


# ==================== 音频处理 ====================

def remux_to_opus(filepath):
    """
    将 .webm 无损重封装为 .opus（Ogg Opus 容器）。
    增加错误诊断和备用方案。
    """
    if not filepath.endswith(".webm"):
        return filepath

    opus_path = filepath[:-5] + ".opus"

    # 方案 1: 标准重封装
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", filepath,
        "-map", "0:a:0",
        "-c:a", "copy",
        "-sn", "-dn", "-vn",
        "-map_metadata", "0",
        opus_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60)
        if r.returncode == 0 and file_ok(opus_path):
            os.remove(filepath)
            log(f"[REMUX] {os.path.basename(opus_path)} (无损重封装)")
            return opus_path
        else:
            err = r.stderr[:300] if r.stderr else "未知错误"
            log(f"[REMUX WARN] 标准重封装失败: {err}")
            if os.path.exists(opus_path):
                os.remove(opus_path)
    except Exception as e:
        log(f"[REMUX ERR] {e}")
        if os.path.exists(opus_path):
            os.remove(opus_path)

    # 方案 2: 不带原 metadata 重封装
    log("[REMUX RETRY] 尝试不带原 metadata 重封装...")
    cmd2 = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-map", "0:a:0",
        "-c:a", "copy",
        "-sn", "-dn", "-vn",
        opus_path,
    ]
    try:
        r = subprocess.run(cmd2, capture_output=True, text=True, errors='replace', timeout=60)
        if r.returncode == 0 and file_ok(opus_path):
            os.remove(filepath)
            log(f"[REMUX] {os.path.basename(opus_path)} (无损重封装, 无原 metadata)")
            return opus_path
        else:
            err = r.stderr[:300] if r.stderr else "未知错误"
            log(f"[REMUX WARN] 备用方案也失败: {err}")
            if os.path.exists(opus_path):
                os.remove(opus_path)
    except Exception as e:
        log(f"[REMUX ERR] {e}")
        if os.path.exists(opus_path):
            os.remove(opus_path)

    # 方案 3: 输出为 .ogg 再重命名为 .opus
    ogg_path = filepath[:-5] + ".ogg"
    cmd3 = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-map", "0:a:0",
        "-c:a", "copy",
        "-sn", "-dn", "-vn",
        ogg_path,
    ]
    try:
        r = subprocess.run(cmd3, capture_output=True, text=True, errors='replace', timeout=60)
        if r.returncode == 0 and file_ok(ogg_path):
            os.rename(ogg_path, opus_path)
            os.remove(filepath)
            log(f"[REMUX] {os.path.basename(opus_path)} (ogg->opus 无损重封装)")
            return opus_path
        else:
            err = r.stderr[:300] if r.stderr else "未知错误"
            log(f"[REMUX FAIL] 所有重封装方案均失败: {err}")
            if os.path.exists(ogg_path):
                os.remove(ogg_path)
    except Exception as e:
        log(f"[REMUX FAIL] {e}")
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

    log("[REMUX FAIL] 保留原始 webm 文件")
    return filepath


def set_metadata(filepath, title, artist, comment=None):
    """
    用 ffmpeg -c copy 写入歌名、歌手和原始视频地址。
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
    if comment:
        cmd.insert(-1, "-metadata")
        cmd.insert(-1, f"comment={comment}")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60)
        if r.returncode == 0 and file_ok(tmp):
            os.replace(tmp, filepath)
            return True
        else:
            err = r.stderr[:300] if r.stderr else "未知错误"
            log(f"[META WARN] ffmpeg 写入失败: {err}")
    except Exception as e:
        log(f"[META ERR] {e}")

    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def thumb_to_jpeg_bytes(thumb_path):
    """
    将任意缩略图转为标准 JPEG bytes。
    先尝试 ffmpeg，失败则 fallback 到 Pillow。
    """
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
        r = subprocess.run(convert_cmd, capture_output=True, text=True, errors='replace', timeout=30)
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
    except Exception as e:
        log(f"[THUMB WARN] Pillow 转换失败: {e}")
        return None


def embed_thumbnail(filepath, thumb_path):
    """
    根据音频容器类型写入封面。
    - opus/ogg  -> OggOpus (METADATA_BLOCK_PICTURE)
    - m4a/mp4    -> MP4 (covr)
    - webm       -> ffmpeg 尝试嵌入（作为 fallback）
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

        elif ext == ".webm":
            log("[THUMB] webm 容器尝试 ffmpeg 嵌入封面...")
            tmp = filepath + ".cover.webm"
            cmd = [
                "ffmpeg", "-y",
                "-i", filepath,
                "-i", thumb_path,
                "-map", "0:a",
                "-map", "1:v",
                "-c:a", "copy",
                "-c:v", "copy",
                "-disposition:v:0", "attached_pic",
                tmp,
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60)
                if r.returncode == 0 and file_ok(tmp):
                    os.replace(tmp, filepath)
                    return True
                else:
                    err = r.stderr[:200] if r.stderr else "未知错误"
                    log(f"[THUMB WARN] webm 封面嵌入失败: {err}")
            except Exception as e:
                log(f"[THUMB WARN] webm 封面嵌入异常: {e}")
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            return False

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


# ==================== 智能选源 ====================

def pick_best_audio_id(song, artist):
    """
    搜索前 5 个结果，强制歌名匹配 + 歌手匹配 + 繁简兼容。
    """
    search_query = f'ytsearch5:"{song}" "{artist}" audio'
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", "chrome",
        "--quiet", "--no-warnings",
        "--flat-playlist",
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

    best_id = None
    best_score = -1
    song_lower = song.lower()
    artist_lower = artist.lower()

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
            r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=30, env=ENV)
            if r.returncode != 0:
                continue
            info = json.loads(r.stdout.strip())
        except Exception:
            continue

        duration = info.get("duration") or 0
        if duration < 60 or duration > 600:
            continue

        title = info.get("title") or ""
        channel = info.get("channel") or ""

        title_norm = normalize_chinese(title)
        song_norm = normalize_chinese(song_lower)
        artist_norm = normalize_chinese(artist_lower)
        channel_norm = normalize_chinese(channel)

        # 硬性歌名过滤
        song_matched = song_norm in title_norm
        if not song_matched:
            song_stripped = re.sub(r'[\s\-~《》\(\)（）\[\]]', '', song_norm)
            title_stripped = re.sub(r'[\s\-~《》\(\)（）\[\]]', '', title_norm)
            song_matched = song_stripped in title_stripped

        if not song_matched:
            log(f"  [SKIP] 歌名不匹配: {title[:40]}")
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

        log(f"  [CANDIDATE] {title[:45]:<<45} | "
            f"abr={max_abr}k fmt={best_fmt} | song={'✓' if song_matched else '✗'} "
            f"artist={'✓' if artist_matched else '✗'} | score={score}")

        if score > best_score:
            best_score = score
            best_id = vid

    if best_id:
        log(f"[PICK] 最佳源 ID: {best_id} (score={best_score})")
    else:
        log("[PICK] 无合格结果，将 fallback")
    return best_id


# ==================== 主流程 ====================

def download_song(song, artist):
    base_name = sanitize(f"{song}-{artist}")
    existing = find_downloaded_file(base_name)

    if existing:
        log(f"[SKIP] 已存在: {os.path.basename(existing)}")
        return True

    log(f"[DL] {song} - {artist}")

    best_id = pick_best_audio_id(song, artist)
    if best_id:
        target_url = f"https://www.youtube.com/watch?v={best_id}"
        source_url = target_url
    else:
        search_query = f'ytsearch1:"{song}" "{artist}" official audio'
        cmd_id = [
            "yt-dlp",
            "--cookies-from-browser", "chrome",
            "--quiet", "--no-warnings",
            "--print", "%(id)s",
            search_query,
        ]
        try:
            r = subprocess.run(cmd_id, capture_output=True, text=True, errors='replace', timeout=30, env=ENV)
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
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=300, env=ENV)
        if r.returncode != 0:
            log("[RETRY] fallback search")
            query2 = f'ytsearch3:"{song}" "{artist}"'
            cmd[-1] = query2
            r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=300, env=ENV)
            if r.returncode != 0:
                err = r.stderr[:300] if r.stderr else "未知错误"
                log(f"[FAIL] {err}")
                return False

        fp = find_downloaded_file(base_name)
        if not fp:
            log("[FAIL] 下载文件不存在")
            return False

        # 1) 无损重封装
        fp = remux_to_opus(fp)

        # 2) 先写文字 metadata
        meta_ok = set_metadata(fp, song, artist.replace("&", " / "), comment=source_url)
        if meta_ok:
            log(f"[META] 已写入歌名/歌手/来源")
        else:
            log("[META FAIL] 歌名/歌手写入失败")

        # 3) 再写封面
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