#!/usr/bin/env python3
"""
Test Doubao TTS - supports compare mode and normal test mode (MP3 playback)

Usage:
  python test_tts.py              # Normal mode: synthesize MP3 and play
  python test_tts.py --compare    # Compare mode: compare ogg_opus vs mp3
  python test_tts.py --no-play    # Normal mode without playback
  python test_tts.py --speaker-id zh_male_lengkugege_emo_v2_mars_bigtts  # Override speaker
  python test_tts.py --resource-id seed-tts-1.0  # Override resource_id
"""
import sys
import os
import wave

# Add project root to path so imports work when running from any directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import subprocess
import tempfile
from config import APP_CONFIG
from core.services.tts.doubao import DoubaoTTS


SAMPLE_RATE = 24000  # synthesize() always returns 24000Hz 16-bit mono PCM


def play_audio(audio_data: bytes):
    """Play raw PCM audio data (16-bit signed, 24000Hz, mono) via WAV wrapper."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
    try:
        # Wrap raw PCM in a WAV container so afplay/aplay can handle it
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data)

        if sys.platform == "darwin":
            subprocess.run(["afplay", tmp_path], check=True)
        else:
            subprocess.run(["aplay", tmp_path], check=True)
    except FileNotFoundError as e:
        print(f"  [!] Player not found: {e}")
    except subprocess.CalledProcessError as e:
        print(f"  [!] Playback failed: {e}")
    finally:
        os.unlink(tmp_path)


def get_tts():
    """Initialize TTS from config."""
    tts_config = APP_CONFIG.get("tts", {}).get("doubao", {})
    app_id = tts_config.get("app_id")
    access_key = tts_config.get("access_key")
    speaker = tts_config.get("default_speaker", "zh_female_cancan_mars_bigtts")

    if not app_id or not access_key:
        print("错误: 请先配置豆包 API 凭证")
        sys.exit(1)

    # Allow overriding speaker_id via --speaker-id <id> for debugging
    if "--speaker-id" in sys.argv:
        idx = sys.argv.index("--speaker-id")
        if idx + 1 < len(sys.argv):
            speaker = sys.argv[idx + 1]

    # Allow overriding resource_id via --resource-id <id> for debugging
    resource_id = None
    if "--resource-id" in sys.argv:
        idx = sys.argv.index("--resource-id")
        if idx + 1 < len(sys.argv):
            resource_id = sys.argv[idx + 1]

    return DoubaoTTS(app_id=app_id, access_key=access_key, speaker=speaker, resource_id=resource_id), tts_config


# TEXT = '央视财经频道《经济半小时》两会特别节目《中国经济向新行：智能经济活力奔涌》播出，聚焦我国人工智能大模型已进入全球第一梯队，而阿里千问APP作为AI助手的典型代表，正以"AI办事"的创新模式，深刻重塑大众的日常生活。'
TEXT = "你好，我是Moos, 检测到室内温度已升至 28℃，当前室外温度 32℃。建议开启空调制冷模式，是否立即执行？您也可以通过语音指令调整目标温度。"


def run_normal(play: bool = True):
    """Normal mode: synthesize MP3 and optionally play."""
    tts, tts_config = get_tts()

    print(f"\n{'='*60}")
    print("Doubao TTS - Normal Mode")
    print(f"{'='*60}")
    print(f"Speaker : {tts.speaker}")
    print(f"Text    : {TEXT[:60]}...")

    start = time.time()
    audio_data = tts.synthesize(TEXT)
    elapsed = time.time() - start

    if audio_data:
        print(f"\n✅ Success! Size: {len(audio_data):,} bytes, Time: {elapsed:.2f}s")
        if play:
            print("Playing audio...")
            play_audio(audio_data)
    else:
        print("\n❌ Failed!")

    print(f"\n{'='*60}")
    print("Done")
    print(f"{'='*60}")


def run_compare(play: bool = True):
    """Compare mode: ogg_opus vs mp3, play both if requested."""
    tts, tts_config = get_tts()

    print(f"\n{'='*60}")
    print("Doubao TTS - Compare Mode (ogg_opus vs mp3)")
    print(f"{'='*60}")
    print(f"Speaker : {tts.speaker}")
    print(f"Text    : {TEXT[:60]}...")

    results = {}
    for fmt in ("ogg_opus", "mp3"):
        print(f"\n--- {fmt} ---")
        start = time.time()
        data = tts.synthesize(TEXT, format=fmt)
        elapsed = time.time() - start
        if data:
            print(f"✅ Size: {len(data):,} bytes, Time: {elapsed:.2f}s")
            results[fmt] = data
        else:
            print(f"❌ Failed!")
            results[fmt] = None

    print(f"\n--- Comparison ---")
    if results.get("ogg_opus") and results.get("mp3"):
        ogg_size = len(results["ogg_opus"])
        mp3_size = len(results["mp3"])
        ratio = mp3_size / ogg_size
        print(f"ogg_opus: {ogg_size:,} bytes")
        print(f"mp3     : {mp3_size:,} bytes")
        print(f"Ratio mp3/ogg_opus: {ratio:.2f}x", end="")
        if ratio > 1:
            print(f"  (ogg_opus saves {(1 - 1/ratio) * 100:.1f}%)")
        else:
            print(f"  (mp3 saves {(1 - ratio) * 100:.1f}%)")

    if play:
        for fmt, data in results.items():
            if data:
                print(f"\nPlaying {fmt}...")
                play_audio(data)

    print(f"\n{'='*60}")
    print("Done")
    print(f"{'='*60}")


if __name__ == "__main__":
    compare_mode = "--compare" in sys.argv
    no_play = "--no-play" in sys.argv

    if compare_mode:
        run_compare(play=not no_play)
    else:
        run_normal(play=not no_play)
