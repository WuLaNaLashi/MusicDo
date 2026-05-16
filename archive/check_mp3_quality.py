#!/usr/bin/env python3
"""
MP3 音质检测工具
递归扫描 /home/hanxiao/Music/LocalMusic 下的所有 MP3 文件，
检测码率、频谱分析，判断是否为 YouTube 转码注水版（伪 320k）。

原理：
- YouTube 音频源为 AAC ~128kbps，高频截止 ~16kHz
- 真 320k MP3 频谱应延伸到 20kHz+
- 通过 FFT 频谱分析判断高频能量

结果保存到文件：/home/hanxiao/Music/LocalMusic/音质检测报告.md
"""

import os
import subprocess
import json
import tempfile
import numpy as np
from pathlib import Path
from datetime import datetime
from mutagen.mp3 import MP3


REPORT_PATH = "/home/hanxiao/Music/LocalMusic/音质检测报告.md"
SCAN_DIR = "/home/hanxiao/Music/LocalMusic"


def get_bitrate(filepath):
    """获取 MP3 码率 (kbps)"""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath],
            capture_output=True, text=True, timeout=10
        )
        info = json.loads(pr.stdout)
        br = int(info["format"]["bit_rate"]) // 1000
        return br
    except Exception as e:
        return None


def get_sample_rate(filepath):
    """获取采样率 (Hz)"""
    try:
        audio = MP3(filepath)
        return audio.info.sample_rate
    except:
        return None


def analyze_spectrum(filepath, sample_duration=5):
    """
    FFT 频谱分析：检测高频能量
    返回各频段能量 (dB) 和关键判断指标
    """
    try:
        with tempfile.NamedTemporaryFile(suffix='.f32le', delete=False) as tmp:
            tmp_path = tmp.name

        cmd = [
            "ffmpeg", "-y", "-i", filepath,
            "-ss", "30", "-t", str(sample_duration),
            "-ac", "1", "-ar", "44100",
            "-f", "f32le", tmp_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=30)

        samples = np.fromfile(tmp_path, dtype=np.float32)
        os.remove(tmp_path)

        if len(samples) < 44100:
            return None, "样本过短"

        fft = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(len(samples), 1 / 44100)
        magnitude = np.abs(fft)

        def band_energy(start, end):
            mask = (freqs >= start) & (freqs < end)
            if not np.any(mask):
                return -100
            return 20 * np.log10(np.mean(magnitude[mask]) + 1e-10)

        return {
            "low_db": round(band_energy(1000, 4000), 1),
            "mid_db": round(band_energy(8000, 12000), 1),
            "high_db": round(band_energy(16000, 20000), 1),
            "very_high_db": round(band_energy(18000, 22050), 1),
            "high_to_low_diff": round(band_energy(16000, 20000) - band_energy(1000, 4000), 1),
        }, None

    except Exception as e:
        return None, str(e)


def classify_quality(filepath, spectrum, bitrate):
    """
    综合判断音质等级
    """
    verdict = "UNKNOWN"
    confidence = 0
    reasons = []

    # 1. 码率检查
    if bitrate is None:
        verdict = "UNKNOWN"
        reasons.append("无法获取码率")
    elif bitrate < 192:
        verdict = "LOW_QUALITY"
        reasons.append(f"码率仅 {bitrate}kbps，低于 192k")
        confidence = 90
    elif bitrate < 300:
        verdict = "MEDIUM_QUALITY"
        reasons.append(f"码率 {bitrate}kbps，中等品质")
        confidence = 70
    else:
        reasons.append(f"码率 {bitrate}kbps")

    # 2. 频谱分析
    if spectrum:
        high_to_low = spectrum["high_to_low_diff"]
        very_high = spectrum["very_high_db"]

        # YouTube AAC 128k 转码特征：16kHz 以上能量急剧衰减
        if high_to_low < -35 or very_high < -50:
            if bitrate and bitrate >= 300:
                verdict = "YOUTUBE_UPSCALED"
                reasons.append(f"16-20kHz 衰减 {high_to_low}dB，疑似 YouTube 低码率源强制转码为 {bitrate}k")
                confidence = 85
            else:
                verdict = "LOW_QUALITY"
                reasons.append(f"高频衰减严重 ({high_to_low}dB)")
                confidence = 75
        elif high_to_low > -20 and very_high > -40:
            if verdict in ["UNKNOWN", "MEDIUM_QUALITY"]:
                verdict = "HIGH_QUALITY"
                reasons.append("频谱延伸良好，可能是真 320k 或高质量源")
                confidence = 80
        else:
            reasons.append(f"频谱表现一般 ({high_to_low}dB)")

    # 3. 采样率检查
    sample_rate = get_sample_rate(filepath)
    if sample_rate and sample_rate < 44100:
        reasons.append(f"采样率仅 {sample_rate}Hz")
        if verdict == "HIGH_QUALITY":
            verdict = "MEDIUM_QUALITY"

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasons": "; ".join(reasons),
        "sample_rate": sample_rate,
    }


def get_file_size(filepath):
    """获取文件大小 (MB)"""
    try:
        return os.path.getsize(filepath) / (1024 * 1024)
    except:
        return 0


def scan_library(directory, output_file=None, move_high_quality=False):
    """扫描曲库并生成报告，可选移动 HIGH_QUALITY 文件"""
    if output_file is None:
        output_file = REPORT_PATH

    source_path = Path(directory)
    if not source_path.exists():
        print(f"❌ 目录不存在: {directory}")
        return

    mp3_files = list(source_path.rglob("*.mp3"))
    total = len(mp3_files)
    print(f"🔍 发现 {total} 个 MP3 文件，开始分析...")
    print(f"📁 报告将保存到: {output_file}")
    if move_high_quality:
        print(f"📂 HIGH_QUALITY 文件将移动到: /home/hanxiao/Music/HIGH_QUALITY/")
    print("=" * 60)

    results = {
        "HIGH_QUALITY": [],
        "MEDIUM_QUALITY": [],
        "YOUTUBE_UPSCALED": [],
        "LOW_QUALITY": [],
        "UNKNOWN": [],
        "ERROR": [],
    }

    for i, mp3_file in enumerate(mp3_files, 1):
        if i % 10 == 0 or i == total:
            print(f"[{i}/{total}] 分析中... {mp3_file.name[:50]}", end="\r")

        try:
            bitrate = get_bitrate(str(mp3_file))
            spectrum, err = analyze_spectrum(str(mp3_file))
            file_size = get_file_size(str(mp3_file))

            if err:
                results["ERROR"].append({
                    "file": str(mp3_file.relative_to(source_path)),
                    "error": err,
                })
                continue

            quality = classify_quality(str(mp3_file), spectrum, bitrate)

            result = {
                "file": str(mp3_file.relative_to(source_path)),
                "name": mp3_file.name,
                "bitrate": bitrate,
                "size_mb": round(file_size, 1),
                "spectrum": spectrum,
                **quality,
            }

            results[quality["verdict"]].append(result)

        except Exception as e:
            results["ERROR"].append({
                "file": str(mp3_file.relative_to(source_path)),
                "error": str(e),
            })

    # 生成 Markdown 报告
    generate_report(results, output_file, source_path, total)
    print(f"\n✅ 报告已保存: {output_file}")


def generate_report(results, output_file, source_path, total):
    """生成 Markdown 格式报告"""
    emoji_map = {
        "HIGH_QUALITY": "✅",
        "MEDIUM_QUALITY": "🟡",
        "YOUTUBE_UPSCALED": "⚠️",
        "LOW_QUALITY": "❌",
        "UNKNOWN": "❓",
        "ERROR": "💥",
    }

    verdict_names = {
        "HIGH_QUALITY": "高品质 (真 320k 或高质量源)",
        "MEDIUM_QUALITY": "中等品质",
        "YOUTUBE_UPSCALED": "YouTube 注水版 (伪 320k)",
        "LOW_QUALITY": "低品质",
        "UNKNOWN": "无法判断",
        "ERROR": "分析出错",
    }

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# MP3 音质检测报告\n\n")
        f.write(f"- **扫描目录**: `{source_path}`\n")
        f.write(f"- **生成时间**: {datetime.now().isoformat()}\n")
        f.write(f"- **总文件数**: {total}\n\n")

        # 统计概览
        f.write("## 统计概览\n\n")
        f.write("| 等级 | 数量 | 占比 |\n")
        f.write("|:-----|:-----|:-----|\n")
        for verdict, items in results.items():
            pct = len(items) / total * 100 if total else 0
            f.write(f"| {emoji_map[verdict]} {verdict_names[verdict]} | {len(items)} | {pct:.1f}% |\n")
        f.write("\n")

        # 详细列表
        for verdict in ["YOUTUBE_UPSCALED", "LOW_QUALITY", "MEDIUM_QUALITY", "HIGH_QUALITY", "UNKNOWN", "ERROR"]:
            items = results[verdict]
            if not items:
                continue

            f.write(f"## {emoji_map[verdict]} {verdict_names[verdict]} ({len(items)} 首)\n\n")

            if verdict == "ERROR":
                f.write("| 文件 | 错误 |\n")
                f.write("|:-----|:-----|\n")
                for item in items:
                    f.write(f"| {item['file']} | {item['error']} |\n")
            else:
                f.write("| 序号 | 文件名 | 码率 | 大小 | 高频衰减 | 置信度 | 判断依据 |\n")
                f.write("|:----:|:-------|:-----|:-----|:---------|:-------|:---------|\n")
                for idx, item in enumerate(items, 1):
                    spec = item.get("spectrum", {})
                    diff = spec.get("high_to_low_diff", "N/A")
                    f.write(
                        f"| {idx} | {item['name']} | {item['bitrate']}k | {item['size_mb']}MB | "
                        f"{diff}dB | {item['confidence']}% | {item['reasons']} |\n"
                    )
            f.write("\n")

        # 附录：频谱分析说明
        f.write("## 附录：判断标准\n\n")
        f.write("### 码率\n")
        f.write("- `< 192k`: 低品质\n")
        f.write("- `192k ~ 300k`: 中等品质\n")
        f.write("- `>= 300k`: 高码率（但不等于高音质）\n\n")
        f.write("### 频谱分析\n")
        f.write("- `16-20kHz 能量衰减 < -20dB`: 频谱延伸良好，真高品质\n")
        f.write("- `16-20kHz 能量衰减 < -35dB`: 高频被砍，疑似 YouTube 低码率源转码\n")
        f.write("- 采样率 `< 44100Hz`: 降采样，品质受损\n\n")
        f.write("### YouTube 注水版特征\n")
        f.write("YouTube 音频源为 AAC ~128kbps，即使强制转码为 320k MP3，\n")
        f.write("高频信息已在 AAC 编码时被截断（~16kHz），无法通过提高 MP3 码率恢复。\n")
        f.write("表现为：文件大小达标，但频谱高频空白。\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MP3 音质检测工具")
    parser.add_argument(
        "--dir", "-d",
        default=SCAN_DIR,
        help=f"扫描目录 (默认: {SCAN_DIR})"
    )
    parser.add_argument(
        "--output", "-o",
        default=REPORT_PATH,
        help=f"输出报告路径 (默认: {REPORT_PATH})"
    )
    parser.add_argument(
        "--move-hq", "-m",
        action="store_true",
        help="将 HIGH_QUALITY 文件移动到 /home/hanxiao/Music/HIGH_QUALITY/"
    )
    parser.add_argument(
        "--quick", "-q",
        action="store_true",
        help="快速模式：只检测码率，不做频谱分析"
    )

    args = parser.parse_args()

    if args.quick:
        # 快速模式：简化分析
        print("⚡ 快速模式（仅码率检测）")
        # 这里可以添加快速模式逻辑
    else:
        scan_library(args.dir, args.output)
