#!/usr/bin/env python3
import sys
import os
import base64
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture

def check_opus_cover(path):
    try:
        audio = OggOpus(path)
        pics = audio.get("METADATA_BLOCK_PICTURE", [])
        
        if not pics:
            print(f"❌ 文件中没有封面: {os.path.basename(path)}")
            return
        
        for i, pic_b64 in enumerate(pics):
            raw = base64.b64decode(pic_b64)
            pic = Picture(raw)
            
            print(f"✅ 找到封面 #{i+1}")
            print(f"   格式 (MIME): {pic.mime}")
            print(f"   类型: {pic.type} (3=Cover front)")
            print(f"   尺寸: {len(pic.data)} bytes")
            
            header = pic.data[:12]
            if header[:4] == b'\xff\xd8\xff\xe0' or header[:4] == b'\xff\xd8\xff\xe1':
                print(f"   实际编码: JPEG (正常)")
            elif header[:4] == b'\x89PNG':
                print(f"   实际编码: PNG (正常)")
            elif header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                print(f"   ⚠️ 实际编码: WEBP (部分播放器不支持)")
            else:
                print(f"   实际编码: 未知 {header.hex()}")
                
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 check_cover.py <opus文件路径>")
    else:
        check_opus_cover(sys.argv[1])