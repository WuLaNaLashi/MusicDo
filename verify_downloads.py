#!/usr/bin/env python3
"""
已下载音频文件批量验证工具（修复版）
遍历指定目录下的所有音频文件，读取 comment 中的 YouTube 来源网址，
通过 yt-dlp 获取视频实际信息，与文件名对比，判断下载是否正确。
"""

import os
import re
import json
import subprocess
import concurrent.futures
from pathlib import Path
from datetime import datetime
from threading import Lock

try:
    import zhconv
    HAS_ZHCONV = True
except ImportError:
    HAS_ZHCONV = False

try:
    from mutagen.oggopus import OggOpus
    from mutagen.mp4 import MP4
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

# ==================== 配置 ====================

SCAN_DIR = "/Users/hxg/Downloads/Youtube/YoutubeMusic_2025"
REPORT_PATH = os.path.join(SCAN_DIR, "下载验证报告.md")
MAX_WORKERS = 2  # 降低并发，避免 YouTube 限流
COOKIES_FILE = None  # Cookie 文件路径，如 "cookies.txt"
QUERY_DELAY = 3.0    # 每次 yt-dlp 查询间隔（秒），避免 bot 检测

print_lock = Lock()
import time as time_module

def log(msg, end='\n'):
    with print_lock:
        print(msg, end=end, flush=True)


def normalize_chinese(text):
    if not text:
        return ""
    text = text.lower()
    if HAS_ZHCONV:
        return zhconv.convert(text, 'zh-hans')
    return text


# ==================== 元数据读取 ====================

def extract_comment_and_url(filepath):
    if not HAS_MUTAGEN:
        return None, None, "mutagen 未安装 (pip install mutagen)"

    ext = os.path.splitext(filepath)[1].lower()
    comment = None
    url = None

    try:
        if ext in ('.opus', '.ogg'):
            audio = OggOpus(filepath)
            for key in audio.keys():
                if 'comment' in key.lower() or 'description' in key.lower():
                    comment = str(audio[key])
                    break
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(filepath)
            if 'comment' in audio:
                comment = str(audio['comment'])
            elif '\xa9cmt' in audio:
                comment = str(audio['\xa9cmt'])
        elif ext == '.mp3':
            audio = MP3(filepath)
            if audio.tags:
                for tag_id in audio.tags.keys():
                    if 'COMM' in tag_id:
                        comment = str(audio.tags[tag_id])
                        break
        elif ext == '.flac':
            audio = FLAC(filepath)
            if audio.tags and 'comment' in audio.tags:
                comment = str(audio.tags['comment'])
    except Exception as e:
        return None, None, f"读取元数据失败: {e}"

    if comment:
        urls = re.findall(r'https?://[^\s<>"\'\']+', comment)
        if urls:
            url = urls[0]

    return comment, url, None


def parse_filename(filename):
    name = os.path.splitext(filename)[0]
    parts = name.split('-', 1)
    if len(parts) == 2:
        song, artist = parts[0], parts[1]
        song = song.replace('_', ' ').strip()
        artist = artist.replace('_', ' ').strip()
        return song, artist
    else:
        return name.replace('_', ' ').strip(), ""


def get_local_duration(filepath):
    """从本地音频文件读取时长（秒），作为 yt-dlp 获取失败的 fallback"""
    try:
        from mutagen.oggopus import OggOpus
        from mutagen.mp4 import MP4
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC

        ext = os.path.splitext(filepath)[1].lower()
        audio = None

        if ext in ('.opus', '.ogg'):
            audio = OggOpus(filepath)
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(filepath)
        elif ext == '.mp3':
            audio = MP3(filepath)
        elif ext == '.flac':
            audio = FLAC(filepath)

        if audio and hasattr(audio, 'info') and audio.info:
            return int(audio.info.length)
    except Exception:
        pass

    # fallback: ffprobe
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            duration = float(data.get("format", {}).get("duration", 0))
            return int(duration) if duration > 0 else None
    except Exception:
        pass

    return None


# ==================== 视频信息获取（增强诊断版）====================

def get_video_info(url):
    """
    用 yt-dlp 获取视频信息。
    返回: (video_dict_or_None, error_message_or_None)
    """
    if not url or ('youtube.com' not in url and 'youtu.be' not in url):
        return None, "URL 格式不支持"

    # 先测试 yt-dlp 是否可用
    try:
        test = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        if test.returncode != 0:
            return None, f"yt-dlp 不可用: {test.stderr[:200]}"
    except FileNotFoundError:
        return None, "yt-dlp 未安装 (pip install -U yt-dlp)"
    except Exception as e:
        return None, f"yt-dlp 检测失败: {e}"

    # 尝试获取视频信息（不带 cookie，避免 macOS 沙盒问题）
    cmd = ["yt-dlp", "--quiet", "--no-warnings", "--no-download","--ignore-no-formats-error", "--cookies","cookies.txt", "-j", url]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=45)

        if r.returncode != 0:
            stderr = r.stderr[:300] if r.stderr else "无错误输出"
            # 常见错误诊断
            if "429" in stderr:
                return None, "YouTube 限流 (HTTP 429)，请降低并发或稍后重试"
            elif "Sign in" in stderr or "confirm" in stderr.lower():
                return None, "YouTube 要求登录验证 (bot检测)"
            elif "unavailable" in stderr.lower():
                return None, "视频已下架或不可用"
            elif "private" in stderr.lower():
                return None, "视频已设为私密"
            else:
                return None, f"yt-dlp 失败: {stderr}"

        if not r.stdout.strip():
            return None, "yt-dlp 无输出"

        # 处理可能的 NDJSON 多行输出，取第一行
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if not lines:
            return None, "yt-dlp 输出为空"

        try:
            info = json.loads(lines[0])
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败: {e} | 原始输出: {lines[0][:200]}"

        if not isinstance(info, dict):
            return None, f"yt-dlp 返回非 dict 类型: {type(info).__name__}"

        return {
            "title": info.get("title", ""),
            "channel": info.get("channel", ""),
            "duration": info.get("duration") or 0,
            "id": info.get("id", ""),
            "url": url,
        }, None

    except subprocess.TimeoutExpired:
        return None, "yt-dlp 查询超时 (45s)"
    except Exception as e:
        return None, f"yt-dlp 异常: {e}"


# ==================== 匹配判断 ====================

def calculate_match_score(expected_song, expected_artist, video_info, video_err=None):
    """
    计算匹配分数。
    video_info 必须是 dict 或 None。
    """
    if not isinstance(video_info, dict):
        reason = f"无法获取视频信息: {video_err}" if video_err else "无法获取视频信息"
        return 0, "UNKNOWN", [reason]

    title = video_info.get("title", "")
    channel = video_info.get("channel", "")
    duration = video_info.get("duration", 0)

    title_norm = normalize_chinese(title)
    channel_norm = normalize_chinese(channel)
    song_norm = normalize_chinese(expected_song)
    artist_norm = normalize_chinese(expected_artist)

    score = 50
    reasons = []

    # 时长
    if duration < 30:
        score -= 40
        reasons.append(f"时长过短 ({duration}s)")
    elif duration < 60:
        score -= 20
        reasons.append(f"时长偏短 ({duration}s)")
    elif duration > 600:
        score -= 15
        reasons.append(f"时长过长 ({duration}s)")
    else:
        score += 10
        reasons.append(f"时长正常 ({duration}s)")

    # 歌名
    song_matched = False
    if song_norm in title_norm:
        song_matched = True
        score += 25
        reasons.append(f"歌名匹配: '{expected_song}'")
    else:
        song_stripped = re.sub(r'[\s\-~《》（）\[\]]', '', song_norm)
        title_stripped = re.sub(r'[\s\-~《》（）\[\]]', '', title_norm)
        if song_stripped in title_stripped:
            song_matched = True
            score += 20
            reasons.append(f"歌名模糊匹配: '{expected_song}'")
        else:
            score -= 30
            reasons.append(f"⚠️ 歌名不匹配: 期望 '{expected_song}'，实际 '{title[:40]}'")

    # 歌手
    if artist_norm:
        if artist_norm in title_norm or artist_norm in channel_norm:
            score += 20
            reasons.append(f"歌手匹配: '{expected_artist}'")
        else:
            score -= 15
            reasons.append(f"⚠️ 歌手不匹配: 期望 '{expected_artist}'")

    # 坏词
    bad_keywords = [
        "live", "cover", "remix", "8d", "chipmunk", "1 hour", "10 hours",
        "loop", "slowed", "reverb", "bass boosted", "nightcore",
        "reaction", "review", "compilation", "mix", "playlist",
        "现场", "翻唱", "混音", "慢速", "加速", "变调", "反应",
        "合集", "串烧", "dj", "伴奏", "karaoke", "instrumental",
        "tutorial", "how to", "unboxing", "gameplay", "vlog",
        "podcast", "interview", "news", "talk", "speech",
    ]
    title_lower = title.lower()
    found_bad = [kw for kw in bad_keywords if kw in title_lower]
    if found_bad:
        score -= 25 * len(found_bad)
        reasons.append(f"❌ 标题含非音乐关键词: {', '.join(found_bad)}")

    # 好词
    good_keywords = [
        "official audio", "official music video", "lyrics", "audio",
        "mv", "ost", "soundtrack", "hq", "hd",
        "官方", "歌词", "音频", "无损", "高音质",
    ]
    found_good = [kw for kw in good_keywords if kw in title_lower]
    if found_good:
        score += 10
        reasons.append(f"标题含音乐关键词: {', '.join(found_good)}")

    # 官方频道
    if "vevo" in channel_norm or "official" in channel_norm or "topic" in channel_norm:
        score += 10
        reasons.append(f"官方频道: {channel}")

    if score >= 70:
        verdict = "OK"
    elif score >= 40:
        verdict = "SUSPECT"
    else:
        verdict = "WRONG"

    return max(0, min(100, score)), verdict, reasons


# ==================== 单文件验证（类型安全版）====================

def verify_single_file(filepath):
    filename = os.path.basename(filepath)
    expected_song, expected_artist = parse_filename(filename)

    comment, url, err = extract_comment_and_url(filepath)

    # 统一返回结构，确保所有字段存在
    base_result = {
        "file": filename,
        "path": str(filepath),
        "expected_song": expected_song,
        "expected_artist": expected_artist,
        "comment": None,
        "url": None,
        "video_title": None,
        "video_channel": None,
        "duration": None,
        "score": 0,
        "verdict": "UNKNOWN",
        "reasons": [],
    }

    if err:
        base_result["verdict"] = "ERROR"
        base_result["reasons"] = [err]
        return base_result

    if not url:
        base_result["comment"] = comment[:200] if comment else None
        base_result["verdict"] = "NO_URL"
        base_result["reasons"] = ["未在 comment 中找到 YouTube URL"]
        return base_result

    # 获取视频信息（明确解包 tuple）
    video_result = get_video_info(url)

    # 安全检查：确保返回的是 tuple 且长度为 2
    if not isinstance(video_result, tuple) or len(video_result) != 2:
        base_result["verdict"] = "ERROR"
        base_result["reasons"] = [f"get_video_info 返回格式异常: {type(video_result).__name__}"]
        return base_result

    video_info, video_err = video_result

    # 再次安全检查：video_info 必须是 dict 或 None
    if video_info is not None and not isinstance(video_info, dict):
        base_result["verdict"] = "ERROR"
        base_result["reasons"] = [f"视频信息类型异常: {type(video_info).__name__} (期望 dict)"]
        return base_result

    # 如果 yt-dlp 没拿到时长，先用本地文件时长覆盖，再算分
    if video_info and not video_info.get("duration"):
        local_dur = get_local_duration(filepath)
        if local_dur:
            video_info["duration"] = local_dur

    score, verdict, reasons = calculate_match_score(expected_song, expected_artist, video_info, video_err)

    base_result["comment"] = comment[:200] if comment else None
    base_result["url"] = url

    # 安全地提取视频信息字段
    if isinstance(video_info, dict):
        base_result["video_title"] = video_info.get("title")
        base_result["video_channel"] = video_info.get("channel")
        base_result["duration"] = video_info.get("duration")

    base_result["score"] = score
    base_result["verdict"] = verdict
    base_result["reasons"] = reasons
    return base_result


# ==================== 主流程 ====================

def main():
    scan_path = Path(SCAN_DIR)
    if not scan_path.exists():
        log(f"❌ 目录不存在: {SCAN_DIR}")
        return

    # 先测试 yt-dlp
    log("🔧 检测 yt-dlp 可用性...")
    try:
        test = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        if test.returncode == 0:
            log(f"✅ yt-dlp 版本: {test.stdout.strip()}")
        else:
            log(f"⚠️ yt-dlp 检测异常: {test.stderr[:200]}")
    except Exception as e:
        log(f"❌ yt-dlp 未安装或不可用: {e}")
        log("   请运行: pip install -U yt-dlp")
        return

    # Cookie 配置提示
    if COOKIES_FILE:
        if os.path.exists(COOKIES_FILE):
            log(f"✅ 使用 Cookie 文件: {COOKIES_FILE}")
        else:
            log(f"⚠️ Cookie 文件不存在: {COOKIES_FILE}")
            log("   导出方法: yt-dlp --cookies-from-browser chrome --cookies cookies.txt")
    else:
        log("⚠️ 未指定 Cookie 文件，使用浏览器 Cookie（可能被 YouTube 检测为 bot）")
        log("   建议导出: yt-dlp --cookies-from-browser chrome --cookies cookies.txt")
        log("   然后使用: --cookies cookies.txt")

    log(f"⏱️  查询间隔: {QUERY_DELAY}s | 并发: {MAX_WORKERS}")

    audio_files = []
    for ext in ('*.opus', '*.ogg', '*.m4a', '*.mp4', '*.mp3', '*.flac', '*.webm'):
        audio_files.extend(scan_path.glob(ext))

    total = len(audio_files)
    log(f"🔍 发现 {total} 个音频文件")
    log(f"📁 报告将保存到: {REPORT_PATH}")
    log(f"🚀 开始验证（并发: {MAX_WORKERS}）...")
    log("=" * 60)

    results = {"OK": [], "SUSPECT": [], "WRONG": [], "NO_URL": [], "ERROR": [], "UNKNOWN": []}

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {executor.submit(verify_single_file, str(fp)): fp for fp in audio_files}

        completed = 0
        for future in concurrent.futures.as_completed(future_to_file):
            completed += 1
            if completed % 10 == 0 or completed == total:
                log(f"[{completed}/{total}] 验证中...", end='\r')

            try:
                result = future.result()
                verdict = result.get("verdict", "UNKNOWN")
                results[verdict].append(result)

                if verdict in ("WRONG", "SUSPECT", "ERROR", "UNKNOWN"):
                    log(f"\n⚠️ [{verdict}] {result.get('file', '?')} | 分数: {result.get('score', 0)}")
                    for r in result.get('reasons', [])[:3]:
                        log(f"   - {r}")

            except Exception as e:
                fp = future_to_file[future]
                song, artist = parse_filename(fp.name)
                results["ERROR"].append({
                    "file": fp.name, "path": str(fp),
                    "expected_song": song, "expected_artist": artist,
                    "comment": None, "url": None,
                    "video_title": None, "video_channel": None, "duration": None,
                    "verdict": "ERROR", "score": 0,
                    "reasons": [f"验证过程异常: {e}"],
                })

    log(f"\n{'='*60}")
    log("📊 验证完成，生成报告...")

    generate_report(results, REPORT_PATH, total)
    log(f"✅ 报告已保存: {REPORT_PATH}")

    for verdict, items in results.items():
        if items:
            emoji = {"OK": "✅", "SUSPECT": "🔍", "WRONG": "❌",
                     "NO_URL": "⚠️", "ERROR": "💥", "UNKNOWN": "❓"}.get(verdict, "")
            log(f"   {emoji} {verdict}: {len(items)} 个")


def generate_report(results, output_path, total):
    emoji_map = {
        "OK": "✅", "SUSPECT": "🔍", "WRONG": "❌",
        "NO_URL": "⚠️", "ERROR": "💥", "UNKNOWN": "❓",
    }
    verdict_names = {
        "OK": "正常匹配",
        "SUSPECT": "存在疑问（需人工复核）",
        "WRONG": "明显错误（建议重新下载）",
        "NO_URL": "无来源URL（无法验证）",
        "ERROR": "验证出错",
        "UNKNOWN": "无法获取视频信息",
    }

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 音频文件下载验证报告\n\n")
        f.write(f"- **扫描目录**: `{SCAN_DIR}`\n")
        f.write(f"- **生成时间**: {datetime.now().isoformat()}\n")
        f.write(f"- **总文件数**: {total}\n\n")

        f.write("## 统计概览\n\n")
        f.write("| 等级 | 数量 | 占比 | 说明 |\n")
        f.write("|:-----|:-----|:-----|:-----|\n")
        for verdict in ["OK", "SUSPECT", "WRONG", "NO_URL", "ERROR", "UNKNOWN"]:
            items = results.get(verdict, [])
            pct = len(items) / total * 100 if total else 0
            advice = {
                "OK": "无需处理", "SUSPECT": "建议抽样试听", "WRONG": "建议删除重下",
                "NO_URL": "无法自动验证", "ERROR": "检查 yt-dlp/网络", "UNKNOWN": "检查 yt-dlp/网络",
            }.get(verdict, "")
            f.write(f"| {emoji_map.get(verdict, '❓')} {verdict_names.get(verdict, verdict)} | {len(items)} | {pct:.1f}% | {advice} |\n")
        f.write("\n")

        for verdict in ["WRONG", "SUSPECT", "UNKNOWN", "NO_URL", "ERROR", "OK"]:
            items = results.get(verdict, [])
            if not items:
                continue

            f.write(f"## {emoji_map.get(verdict, '❓')} {verdict_names.get(verdict, verdict)} ({len(items)} 个)\n\n")

            if verdict == "OK":
                f.write("| 序号 | 文件名 | 期望歌曲 | 期望歌手 | 视频标题 | 频道 | 时长 | 分数 |\n")
                f.write("|:----:|:-------|:---------|:---------|:---------|:-----|:-----|:-----|\n")
                for idx, item in enumerate(items, 1):
                    f.write(
                        f"| {idx} | {item.get('file', '-')} | {item.get('expected_song', '-')} | {item.get('expected_artist', '-')} | "
                        f"{item.get('video_title') or '-'} | {item.get('video_channel') or '-'} | "
                        f"{item.get('duration') or '-'}s | {item.get('score', 0)} |\n"
                    )
            else:
                f.write("| 序号 | 文件名 | 期望歌曲 | 期望歌手 | 视频标题 | 频道 | 时长 | 分数 | 问题原因 |\n")
                f.write("|:----:|:-------|:---------|:---------|:---------|:-----|:-----|:-----|:---------|\n")
                for idx, item in enumerate(items, 1):
                    reasons_str = "; ".join(item.get('reasons', []))
                    f.write(
                        f"| {idx} | {item.get('file', '-')} | {item.get('expected_song', '-')} | {item.get('expected_artist', '-')} | "
                        f"{item.get('video_title') or '-'} | {item.get('video_channel') or '-'} | "
                        f"{item.get('duration') or '-'}s | {item.get('score', 0)} | {reasons_str} |\n"
                    )
            f.write("\n")

        f.write("## 处理建议\n\n")
        wrong_count = len(results.get("WRONG", []))
        suspect_count = len(results.get("SUSPECT", []))
        unknown_count = len(results.get("UNKNOWN", []))
        error_count = len(results.get("ERROR", []))

        if unknown_count > 0 or error_count > 0:
            f.write(f"### yt-dlp / 网络问题排查\n\n")
            f.write(f"有 {unknown_count + error_count} 个文件无法获取视频信息，可能原因：\n\n")
            f.write("1. **yt-dlp 版本过旧**: 运行 `pip install -U yt-dlp` 更新\n")
            f.write("2. **YouTube 限流**: 降低并发 `--workers 1`，或暂停几小时再试\n")
            f.write("3. **IP 被封锁**: 尝试更换网络或使用代理\n")
            f.write("4. **Cookie 问题**: macOS 上 `--cookies-from-browser` 可能失效，尝试导出 cookie 文件\n")
            f.write("5. **视频已下架**: 原 YouTube 视频可能被删除或设为私密\n\n")

        if wrong_count > 0:
            f.write(f"1. **❌ 明显错误 ({wrong_count} 个)**: 建议删除后重新下载\n")
        if suspect_count > 0:
            f.write(f"2. **🔍 存在疑问 ({suspect_count} 个)**: 建议抽样试听确认\n")
        f.write("\n")

        f.write("## 附录：判断标准\n\n")
        f.write("### 分数计算\n")
        f.write("- 基础分 50\n")
        f.write("- 歌名匹配: +25（精确）/ +20（模糊）/ -30（不匹配）\n")
        f.write("- 歌手匹配: +20（匹配）/ -15（不匹配）\n")
        f.write("- 时长正常 (60-600s): +10\n")
        f.write("- 时长异常: -15 ~ -40\n")
        f.write("- 标题含非音乐关键词 (live/cover/remix/reaction 等): -25/个\n")
        f.write("- 标题含音乐关键词 (official audio/lyrics 等): +10\n")
        f.write("- 官方频道 (VEVO/Topic): +10\n\n")
        f.write("### 判定阈值\n")
        f.write("- **≥ 70 分**: ✅ 正常\n")
        f.write("- **40 ~ 69 分**: 🔍 疑问，建议复核\n")
        f.write("- **< 40 分**: ❌ 明显错误\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="已下载音频文件批量验证")
    parser.add_argument("--dir", "-d", default=SCAN_DIR, help=f"扫描目录 (默认: {SCAN_DIR})")
    parser.add_argument("--workers", "-w", type=int, default=1, help="并发数 (默认: 4，避免 bot 检测)")
    parser.add_argument("--cookies", "-c", default=None, help="Cookie 文件路径 (推荐: cookies.txt)")
    parser.add_argument("--delay", type=float, default=3.0, help="每次查询间隔秒数 (默认: 3.0)")
    parser.add_argument("--output", "-o", default=REPORT_PATH, help="报告输出路径")
    args = parser.parse_args()

    SCAN_DIR = args.dir
    REPORT_PATH = args.output
    MAX_WORKERS = args.workers
    COOKIES_FILE = args.cookies
    QUERY_DELAY = args.delay

    main()
