"""XiaoZhi protocol client - handles WebSocket connection to XiaoZhi server.

This module is responsible for:
- Connecting to XiaoZhi WebSocket server
- Sending/receiving audio and text messages
- Protocol-level message handling
- Audio codec management and stream control
- JSON message parsing (TTS/STT/LLM)

The main application flow is managed by app.py.
"""

import asyncio
import json
import threading

from core.services.protocols.protocol import Protocol
from core.services.protocols.typing import (
    AbortReason,
    DeviceState,
    ListeningMode,
)
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.wakeup_session import EventManager


class XiaoZhi:
    """XiaoZhi protocol client."""

    _instance = None

    @classmethod
    def instance(cls):
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = XiaoZhi()
        return cls._instance

    def __init__(self):
        """Initialize XiaoZhi client."""
        if XiaoZhi._instance is not None:
            raise Exception("XiaoZhi is singleton, use instance() to get instance")
        XiaoZhi._instance = self

        self.config = ConfigManager.instance()

        # Protocol
        self.protocol = None

        # References (set by MainApp)
        self._app = None
        self._audio_codec = None

    def set_app(self, app):
        """Set reference to main app."""
        self._app = app

    def set_audio_codec(self, codec):
        """Set audio codec reference."""
        self._audio_codec = codec

    @property
    def device_state(self):
        """Proxy device state from app."""
        if self._app:
            return self._app.device_state
        return None

    def set_device_state(self, state):
        """Set device state and manage audio streams accordingly."""
        if not self._app:
            return
        self._app.device_state = state

        from core.services.audio.vad import VAD
        VAD.pause()

        if self._audio_codec:
            self._audio_codec.stop_streams()

        if state == DeviceState.LISTENING:
            if self._audio_codec:
                if self._audio_codec.output_stream.is_active():
                    self._audio_codec.output_stream.stop_stream()
                if not self._audio_codec.input_stream.is_active():
                    self._audio_codec.input_stream.start_stream()
        elif state == DeviceState.SPEAKING:
            if self._audio_codec:
                if self._audio_codec.input_stream.is_active():
                    self._audio_codec.input_stream.stop_stream()
                if not self._audio_codec.output_stream.is_active():
                    self._audio_codec.output_stream.start_stream()

    # Connection management

    async def connect(self):
        """Connect to XiaoZhi server."""
        from core.services.protocols.websocket_protocol import WebsocketProtocol

        self.protocol = WebsocketProtocol()

        # Wire up callbacks directly
        self.protocol.on_network_error = self._on_network_error
        self.protocol.on_incoming_audio = self._on_incoming_audio
        self.protocol.on_incoming_json = self._on_incoming_json
        self.protocol.on_audio_channel_opened = self._on_audio_channel_opened
        self.protocol.on_audio_channel_closed = self._on_audio_channel_closed

        return await self.protocol.connect()

    async def disconnect(self):
        """Disconnect from XiaoZhi server."""
        if self.protocol:
            await self.protocol.close_audio_channel()

    def is_connected(self) -> bool:
        """Check if connected to XiaoZhi server."""
        return self.protocol is not None and self.protocol.is_audio_channel_opened()

    # Audio initialization

    def init_audio(self):
        """Initialize audio codec and start audio input trigger."""
        try:
            from core.services.audio.codec import AudioCodec

            self._audio_codec = AudioCodec()
        except Exception as e:
            logger.warning(f"[XiaoZhi] Failed to initialize audio: {e}")
            return

        threading.Thread(target=self._audio_input_trigger, daemon=True).start()

    # Protocol callbacks

    def _on_network_error(self, message):
        """Handle network error."""
        self.set_device_state(DeviceState.IDLE)
        if self.device_state != DeviceState.CONNECTING:
            self.set_device_state(DeviceState.IDLE)
            asyncio.run_coroutine_threadsafe(
                self.disconnect(), self._app.loop
            )

    def _on_incoming_audio(self, data):
        """Handle incoming audio."""
        if self.device_state == DeviceState.SPEAKING:
            self._audio_codec.write_audio(data)

    def _on_incoming_json(self, json_data):
        """Handle incoming JSON message."""
        try:
            if not json_data:
                return

            data = json.loads(json_data) if isinstance(json_data, str) else json_data
            msg_type = data.get("type", "")

            if msg_type == "tts":
                self._handle_tts_message(data)
            elif msg_type == "stt":
                self._handle_stt_message(data)
            elif msg_type == "llm":
                self._handle_llm_message(data)
        except Exception as exc:
            logger.error(
                f"[XiaoZhi] Failed to handle incoming json: {type(exc).__name__}: {exc}"
            )

    def _on_audio_channel_opened(self):
        """Handle audio channel opened."""
        self.set_device_state(DeviceState.IDLE)

    def _on_audio_channel_closed(self):
        """Handle audio channel closed."""
        self.set_device_state(DeviceState.IDLE)
        if self._audio_codec:
            self._audio_codec.stop_streams()

    # JSON message handlers

    def _handle_tts_message(self, data):
        """Handle TTS message."""
        state = data.get("state", "")
        if state == "start":
            EventManager.on_tts_start(data.get("session_id"))
            self._app.schedule(lambda: self._handle_tts_start())
        elif state == "stop":
            EventManager.on_tts_end(data.get("session_id"))
            self._app.schedule(lambda: self._handle_tts_stop())
        elif state == "sentence_start":
            text = data.get("text", "")
            if text:
                logger.ai_response(text, module="XiaoZhi")
                self._app.schedule(lambda: self._app.set_chat_message("assistant", text))

    def _handle_tts_start(self):
        """Handle TTS start."""
        if self.device_state in [DeviceState.IDLE, DeviceState.LISTENING]:
            self.set_device_state(DeviceState.SPEAKING)

    def _handle_tts_stop(self):
        """Handle TTS stop."""
        pass

    def _handle_stt_message(self, data):
        """Handle STT message."""
        text = data.get("text", "")
        if text:
            logger.user_speech(text, module="XiaoZhi")
            self._app.schedule(lambda: self._app.set_chat_message("user", text))

    def _handle_llm_message(self, data):
        """Handle LLM message."""
        emotion = data.get("emotion", "")
        if emotion:
            self._app.schedule(lambda: self._app.set_emotion(emotion))

    # Audio input handling

    def handle_input_audio(self):
        """Handle audio input - read and send to server."""
        if self.device_state != DeviceState.LISTENING:
            return

        encoded_data = self._audio_codec.read_audio()
        if encoded_data and self.is_connected():
            asyncio.run_coroutine_threadsafe(
                self.send_audio(encoded_data), self._app.loop
            )

    def _audio_input_trigger(self):
        """Trigger audio input event periodically."""
        while self._app and self._app.running:
            if self._audio_codec and self._audio_codec.input_stream.is_active():
                from core.services.protocols.typing import EventType
                self._app.events[EventType.AUDIO_INPUT_READY_EVENT].set()
            import time
            time.sleep(0.01)

    # User actions

    def start_listening(self):
        """Start listening."""
        self._app.schedule(self._start_listening_impl)

    def _start_listening_impl(self):
        """Start listening implementation."""
        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self.send_abort_speaking(AbortReason.ABORT),
            self._app.loop,
        )
        asyncio.run_coroutine_threadsafe(
            self.send_start_listening(ListeningMode.MANUAL), self._app.loop
        )
        self.set_device_state(DeviceState.LISTENING)

    def stop_listening(self):
        """Stop listening."""
        self._app.schedule(self._stop_listening_impl)

    def _stop_listening_impl(self):
        """Stop listening implementation."""
        asyncio.run_coroutine_threadsafe(
            self.send_stop_listening(), self._app.loop
        )
        self.set_device_state(DeviceState.IDLE)

    def abort_speaking(self, reason):
        """Abort speaking."""
        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self.send_abort_speaking(AbortReason.ABORT),
            self._app.loop,
        )

    # Send methods

    async def send_audio(self, frames):
        """Send audio frames."""
        if self.protocol:
            await self.protocol.send_audio(frames)

    async def send_text(self, text: str):
        """Send text message."""
        if self.protocol and self.protocol.is_audio_channel_opened():
            try:
                message = {
                    "type": "listen",
                    "mode": "manual",
                    "state": "detect",
                    "text": text,
                }
                await self.protocol.send_text(json.dumps(message))
                logger.info(f"[XiaoZhi] Text sent: {text}")
            except Exception as e:
                logger.error(f"[XiaoZhi] Failed to send text: {e}")
        else:
            logger.warning("[XiaoZhi] Not connected, cannot send text")

    async def send_start_listening(self, mode):
        """Send start listening command."""
        if self.protocol:
            await self.protocol.send_start_listening(mode)

    async def send_stop_listening(self):
        """Send stop listening command."""
        if self.protocol:
            await self.protocol.send_stop_listening()

    async def send_abort_speaking(self, reason):
        """Send abort speaking command."""
        if self.protocol:
            await self.protocol.send_abort_speaking(reason)

    # Shutdown

    def shutdown(self):
        """Shutdown XiaoZhi - close audio and disconnect."""
        if self._audio_codec:
            self._audio_codec.close()

        if self._app and self._app.loop:
            asyncio.run_coroutine_threadsafe(
                self.disconnect(), self._app.loop
            )
