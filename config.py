import asyncio
import socket
import subprocess
import sys
import time

import requests


async def before_wakeup(speaker, text, source, xiaozhi, xiaoai, app):
    """
    处理收到的用户消息，并决定是否唤醒小智 AI

    - source: 唤醒来源
        - 'kws': 关键字唤醒
        - 'xiaoai': 小爱同学收到用户指令
    """
    if source == "kws":
        # 播放唤醒提示语
        await speaker.play(url="http://127.0.0.1:8080/hello.wav")

        # 给小智发送打招呼消息
        # await xiaozhi.send_text("小智在吗")
        # # 等待小智服务器回复（延长到 5 秒）
        # time.sleep(5)

        return True

    if source == "xiaoai":
        # 打断原来的小爱同学
        await speaker.abort_xiaoai()
        if text == "召唤小智":
            # 停止连续对话
            xiaoai.stop_conversation()
            # 等待 2 秒，让小爱 TTS 恢复可用
            time.sleep(2)
            # 播放唤醒提示语（如果你不使用自带的小爱 TTS，可以去掉上面的延时）
            await speaker.play(url="http://127.0.0.1:8080/hello.wav")
            # 唤醒小智 AI
            return True
        if text.startswith("让龙虾"):
            await app.send_to_openclaw(text.replace("让龙虾", ""))
            return False
            

async def after_wakeup(speaker):
    """
    退出唤醒状态
    """
    await speaker.play(url="http://127.0.0.1:8080/bye.wav")

APP_CONFIG = {
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        "keywords": [
            "你好小智",
            "小智小智",
            "贾维斯"
            "hi 贾维斯"
            "嘿 贾维斯"
            "你好贾维斯"
        ],
        # 静音多久后自动退出唤醒（秒）
        "timeout": 20,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    "vad": {
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.10,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 500,
    },
    "xiaozhi": {
        "OTA_URL": "http://127.0.0.1:8003/xiaozhi/ota/",
        "WEBSOCKET_URL": "ws://127.0.0.1:8000/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "DEVICE_ID": "6c:1f:f7:8d:61:b0", #（可选）默认自动生成
        "VERIFICATION_CODE": "", # 首次登陆时，验证码会在这里更新
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧"],
        "max_listening_retries": 2,  # 最多连续重新唤醒次数
        "exit_prompt": "再见，主人",
        "continuous_conversation_keywords": ["开启连续对话", "启动连续对话", "我想跟你聊天"]
    },
    # TTS (Text-to-Speech) Configuration
    "tts": {
        "doubao": {
            # 豆包语音合成 API 配置
            # 文档地址: https://www.volcengine.com/docs/6561/1598757?lang=zh
            # 产品地址: https://www.volcengine.com/docs/6561/1871062
            "app_id": "xxxxx",           # 你的 App ID
            "access_key": "xxxxxx",       # 你的 Access Key
            "default_speaker": "zh_female_xiaohe_uranus_bigtts",  # 音色 https://www.volcengine.com/docs/6561/1257544?lang=zh
            "audio_format": "ogg_opus",   # 音频格式: ogg_opus, mp3, pcm (默认 ogg_opus，数据量更小下载更快)
        }
    },
    # OpenClaw Configuration
    "openclaw": {
        "url": "ws://localhost:18789",  # OpenClaw WebSocket 地址
        "token": "",  # OpenClaw 认证令牌
        "session_key": "main",  # 会话标识
        "tts_enabled": False,  # 启用 Doubao TTS 播放 OpenClaw 回复 (需要配置 tts.doubao)
        "blocking_playback": True,  # TTS 播放是否阻塞等待完成 (默认 True)
        # "tts_speaker": "zh_female_cancan_mars_bigtts",  # 可选：自定义音色，不设置则使用 tts.doubao.default_speaker
    },
}