"""OpenClaw integration manager for xiaozhi.

Configuration (priority: env vars > config file > defaults):

    1. config.py:
        APP_CONFIG = {
            "openclaw": {
                "enabled": True,
                "url": "ws://localhost:4399",
                "token": "your_token",
                "session_key": "main",
                "tts_enabled": False,  # Enable Doubao TTS to play OpenClaw responses
                "blocking_playback": False,  # Non-blocking by default
                "ack_timeout": 30,  # Seconds to wait for accepted ack
                "response_timeout": 120,  # Seconds to wait for agent response
            }
        }

    2. Environment variables (override config):
        export OPENCLAW_ENABLED=1
        export OPENCLAW_URL=ws://localhost:4399
        export OPENCLAW_TOKEN=your_token
        export OPENCLAW_SESSION_KEY=main

Usage:
    from core.openclaw import OpenClawManager
    await OpenClawManager.send_message("Hello OpenClaw")
"""

import asyncio
import json
import uuid
from typing import Optional

import websockets

from core.utils.base import get_env
from core.utils.logger import logger


class OpenClawManager:
    """Manager for OpenClaw connection and messaging."""

    _instance = None
    _initialized = False

    # Connection
    _websocket = None
    _connected = False
    _receiver_task = None
    _pending: dict[str, asyncio.Future] = {}

    # Heartbeat (using OpenClaw tick events + WebSocket built-in ping/pong)
    _heartbeat_task = None
    _heartbeat_interval = 60  # seconds (how often to check connection health)
    _last_tick_time = 0  # last time we received any message/event from server
    _tick_timeout = 120  # seconds (max silence before considering connection dead)

    # Auto-reconnect
    _reconnect_task = None
    _reconnect_enabled = True
    _reconnect_delay = 1  # initial delay in seconds
    _reconnect_max_delay = 60  # max delay in seconds
    _reconnect_attempts = 0
    _should_reconnect = False  # flag to indicate intentional disconnect vs unexpected

    # Config
    _enabled = False
    _tts_enabled = False  # Enable Doubao TTS to play OpenClaw responses
    _blocking_playback = False  # Whether to use blocking playback (wait for audio to finish)
    _tts_speaker = None  # Custom speaker for OpenClaw TTS (uses tts.doubao.default_speaker if not set)
    _url = None
    _token = None
    _session_key = None
    last_error: str | None = None

    # Response tracking for TTS
    _response_events: dict[str, asyncio.Future] = {}
    _response_texts: dict[str, str] = {}
    _response_timeout = 120  # seconds to wait for agent response (configurable)
    _ack_timeout = 60  # seconds to wait for request accepted response

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        """Initialize the manager from config.

        Configuration priority (highest first):
        1. Environment variables (OPENCLAW_*)
        2. APP_CONFIG["openclaw"]
        3. Default values

        Args:
            enabled: Override enable flag. If None, use environment variable or config.
        """
        logger.info("[OpenClaw] Initializing from config...")
        if cls._initialized:
            return

        from config import APP_CONFIG

        # Get config from APP_CONFIG with defaults
        config = APP_CONFIG.get("openclaw", {})
        cfg_url = config.get("url", "ws://localhost:4399")
        cfg_token = config.get("token", "")
        cfg_session = config.get("session_key", "main")
        cfg_tts_enabled = config.get("tts_enabled", False)
        cfg_blocking_playback = config.get("blocking_playback", False)
        cfg_tts_speaker = config.get("tts_speaker", None)
        cfg_ack_timeout = config.get("ack_timeout", 30)
        cfg_response_timeout = config.get("response_timeout", 120)

        # Enable/disable: parameter > environment variable > default (False)
        if enabled is not None:
            cls._enabled = enabled
        else:
            env_enabled = get_env("OPENCLAW_ENABLED")
            if env_enabled is not None:
                cls._enabled = env_enabled.lower() in ("1", "true", "yes")
            else:
                cls._enabled = False  # Default to disabled if no env var set

        # TTS config: only from config file
        cls._tts_enabled = cfg_tts_enabled
        cls._blocking_playback = cfg_blocking_playback
        cls._tts_speaker = cfg_tts_speaker
        cls._ack_timeout = cfg_ack_timeout
        cls._response_timeout = cfg_response_timeout

        cls._url = get_env("OPENCLAW_URL", cfg_url)
        cls._token = get_env("OPENCLAW_TOKEN", cfg_token)
        cls._session_key = get_env("OPENCLAW_SESSION_KEY", cfg_session)

        if cls._enabled:
            logger.info(f"[OpenClaw] Enabled, will connect to {cls._url}")
            if cls._tts_enabled:
                mode = "blocking" if cls._blocking_playback else "non-blocking"
                logger.info(f"[OpenClaw] TTS playback enabled ({mode} mode) - OpenClaw responses will be played via Doubao TTS")

        cls._initialized = True

    @classmethod
    def initialize(cls, enabled: bool | None = None):
        """Deprecated: Use initialize_from_config instead."""
        cls.initialize_from_config(enabled=enabled)

    @classmethod
    async def connect(cls) -> bool:
        """Connect to OpenClaw gateway."""
        if not cls._initialized:
            cls.initialize_from_config()

        if not cls._enabled:
            return False

        if cls._connected and cls._websocket:
            return True

        try:
            logger.info(f"[OpenClaw] Connecting to {cls._url}...")
            cls._websocket = await websockets.connect(cls._url)
            logger.info(f"[OpenClaw] WebSocket connected, sending handshake...")
            cls._receiver_task = asyncio.create_task(cls._receiver())

            # Send connect request
            res = await cls._request(
                "connect",
                {
                    "minProtocol": 3,
                    "maxProtocol": 3,
                    "client": {
                        "id": "gateway-client",
                        "displayName": "Xiaoai Bridge",
                        "version": "1.0.0",
                        "platform": "python",
                        "mode": "backend",
                        "instanceId": f"xiaoai-{uuid.uuid4().hex[:8]}",
                    },
                    "locale": "zh-CN",
                    "userAgent": "xiaoai-bridge/1.0.0",
                    "role": "operator",
                    "scopes": ["operator.read", "operator.write"],
                    "caps": [],
                    "auth": {"token": cls._token},
                },
                timeout=10,
            )

            if res.get("ok"):
                cls._connected = True
                cls._should_reconnect = True
                cls._reconnect_attempts = 0
                cls._last_tick_time = asyncio.get_event_loop().time()
                logger.info(f"[OpenClaw] Connected to {cls._url}")
                # Start heartbeat monitor task
                cls._heartbeat_task = asyncio.create_task(cls._heartbeat())
                return True
            else:
                error = (res.get("error") or {}).get("message") or "connect failed"
                logger.error(f"[OpenClaw] Connection failed: {error}")
                cls._trigger_reconnect()
                return False

        except Exception as e:
            import traceback
            logger.error(f"[OpenClaw] Connection error: {type(e).__name__}: {e}")
            logger.debug(f"[OpenClaw] Connection error traceback: {traceback.format_exc()}")
            cls._connected = False
            cls._websocket = None
            cls._trigger_reconnect()
            return False

    @classmethod
    async def close(cls):
        """Close the connection."""
        cls._should_reconnect = False
        cls._connected = False
        # Cancel reconnect task
        if cls._reconnect_task:
            cls._reconnect_task.cancel()
            cls._reconnect_task = None
        # Cancel heartbeat task
        if cls._heartbeat_task:
            cls._heartbeat_task.cancel()
            cls._heartbeat_task = None
        if cls._receiver_task:
            cls._receiver_task.cancel()
            cls._receiver_task = None
        if cls._websocket:
            await cls._websocket.close()
            cls._websocket = None

    @classmethod
    async def send_message(cls, text: str, wait_response: bool = False) -> bool | str | None:
        """Send a message to OpenClaw.

        Args:
            text: The message text to send
            wait_response: Whether to wait for agent's text response synchronously.
                          - False (default): Send and return immediately, returns True/False
                          - True: Wait for response and return response text

        Returns:
            - False: Message was not accepted by OpenClaw
            - True: Message was accepted (wait_response=False)
            - str: Response text from OpenClaw (wait_response=True)
            - None: Timeout or error waiting for response (wait_response=True)
        """
        if not cls._initialized:
            cls.initialize_from_config()

        if not cls._enabled:
            logger.warning("[OpenClaw] send_message called but OpenClaw is disabled")
            return False

        if not cls._connected:
            logger.info("[OpenClaw] send_message called but not connected, trying to connect...")
            # Try to connect if not connected
            if not await cls.connect():
                return False

        try:
            idem = str(uuid.uuid4())
            logger.info(f"[OpenClaw] Sending message: {text}")

            # Always set up response tracking (for background receiving)
            loop = asyncio.get_running_loop()
            response_waiter = loop.create_future()
            cls._response_events[idem] = response_waiter
            cls._response_texts[idem] = ""
            logger.info(f"[OpenClaw] Created response tracking: idem={idem}")

            request_params = {
                "message": text,
                "sessionKey": cls._session_key,
                "deliver": False,
                "idempotencyKey": idem,
            }

            if wait_response:
                # wait_response=True: keep strict request/ack flow and return response text.
                logger.info("[OpenClaw] Calling _request for agent method (sync mode)...")
                agent_res = await cls._request("agent", request_params, timeout=10)
                logger.debug(f"[OpenClaw] Agent response: {agent_res}")

                if not agent_res.get("ok"):
                    err = (agent_res.get("error") or {}).get("message") or str(agent_res)
                    cls.last_error = err
                    logger.error(f"[OpenClaw] agent call failed: {agent_res}")
                    cls._response_events.pop(idem, None)
                    cls._response_texts.pop(idem, None)
                    return False

                run_id = (agent_res.get("payload") or {}).get("runId") or idem
                logger.info(f"[OpenClaw] Message accepted, runId: {run_id}")

                # Link run_id to tracking map.
                cls._response_events[run_id] = response_waiter
                if run_id not in cls._response_texts:
                    cls._response_texts[run_id] = cls._response_texts.get(idem, "")
                if idem != run_id:
                    cls._response_events.pop(idem, None)
                    cls._response_texts.pop(idem, None)

                # wait_response=True: wait synchronously for the response text
                try:
                    await asyncio.wait_for(response_waiter, timeout=cls._response_timeout)
                    response_text = cls._response_texts.pop(run_id, "")
                    cls._response_events.pop(run_id, None)
                    return response_text
                except asyncio.TimeoutError:
                    logger.warning(f"[OpenClaw] Timeout waiting for response (runId: {run_id})")
                    cls._response_events.pop(run_id, None)
                    cls._response_texts.pop(run_id, None)
                    return None
            else:
                # wait_response=False: ensure request is accepted, then continue in background.
                req_id, ack_future = await cls._send_request_with_future("agent", request_params)
                logger.debug(f"[OpenClaw] Agent request sent (async mode), req_id={req_id}, idem={idem}")
                try:
                    res = await asyncio.wait_for(ack_future, timeout=cls._ack_timeout)
                    ok = res.get("ok") if isinstance(res, dict) else False
                    payload = res.get("payload") if isinstance(res, dict) else {}
                    run_id = payload.get("runId") if isinstance(payload, dict) else None
                    status = payload.get("status") if isinstance(payload, dict) else None

                    if not ok:
                        err = (res.get("error") or {}).get("message") if isinstance(res, dict) else "agent request rejected"
                        cls.last_error = err or "agent request rejected"
                        logger.error(f"[OpenClaw] Agent request rejected: req_id={req_id}, idem={idem}, error={cls.last_error}")
                        waiter = cls._response_events.pop(idem, None)
                        cls._response_texts.pop(idem, None)
                        if waiter and not waiter.done():
                            waiter.get_loop().call_soon_threadsafe(waiter.set_result, None)
                        return False

                    final_run_id = run_id or idem
                    logger.debug(
                        f"[OpenClaw] Agent request accepted: req_id={req_id}, runId={final_run_id}, status={status}"
                    )
                    if final_run_id != idem and idem in cls._response_events:
                        cls._response_events[final_run_id] = cls._response_events.pop(idem)
                        cls._response_texts[final_run_id] = cls._response_texts.pop(idem, "")

                    asyncio.create_task(cls._wait_and_play_response(final_run_id))
                    return True
                except asyncio.TimeoutError:
                    logger.warning(f"[OpenClaw] Agent ack timeout: req_id={req_id}, idem={idem}, timeout={cls._ack_timeout}s")
                    cls._response_events.pop(idem, None)
                    cls._response_texts.pop(idem, None)
                    return False
                finally:
                    cls._pending.pop(req_id, None)
        except Exception as e:
            import traceback
            logger.error(f"[OpenClaw] Failed to send message: {type(e).__name__}: {e}")
            logger.debug(f"[OpenClaw] Send message traceback: {traceback.format_exc()}")
            return False

    @classmethod
    async def _request(cls, method: str, params=None, timeout: float = 30):
        """Send a request and wait for response."""
        req_id, fut = await cls._send_request_with_future(method, params)
        logger.debug(f"[OpenClaw] _request waiting for response: req_id={req_id}, method={method}")
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            payload = result.get("payload") if isinstance(result, dict) else {}
            status = payload.get("status") if isinstance(payload, dict) else None
            logger.info(
                f"[OpenClaw] _request received response: req_id={req_id}, ok={result.get('ok') if isinstance(result, dict) else None}, status={status}"
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"[OpenClaw] Request timeout: method={method}, req_id={req_id}, timeout={timeout}s")
            raise
        finally:
            cls._pending.pop(req_id, None)
            logger.debug(f"[OpenClaw] _request cleaned up: req_id={req_id}")

    @classmethod
    async def _send_request_with_future(cls, method: str, params=None) -> tuple[str, asyncio.Future]:
        """Send request frame and return (request_id, response_future)."""
        if not cls._websocket:
            raise RuntimeError("OpenClaw websocket is not connected")

        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        cls._pending[req_id] = fut

        request_payload = {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        logger.debug(f"[OpenClaw] _request sending: req_id={req_id}, method={method}")
        await cls._websocket.send(json.dumps(request_payload))
        return req_id, fut

    @classmethod
    async def _receiver(cls):
        """Background task to receive responses and events."""
        try:
            async for message in cls._websocket:
                # Update last activity time for any message (including WebSocket ping/pong frames)
                cls._last_tick_time = asyncio.get_event_loop().time()

                if isinstance(message, bytes):
                    # WebSocket ping/pong frames are handled automatically by websockets library
                    continue

                if not isinstance(message, str):
                    continue

                try:
                    data = json.loads(message)
                    msg_type = data.get("type")

                    # Handle event messages (including tick events from OpenClaw)
                    if msg_type == "event":
                        event_name = data.get("event", "")
                        logger.debug(f"[OpenClaw] Received event: {event_name}")
                        if event_name == "tick":
                            logger.debug("[OpenClaw] Tick event received")
                        # Handle agent response events for TTS playback
                        elif event_name in ("run.completed", "run.output", "run.text", "agent"):
                            logger.debug(f"[OpenClaw] Agent event received: {event_name}")
                            # Do not block receiver loop on event handling; keep processing res frames promptly.
                            asyncio.create_task(cls._handle_agent_event(data))
                        else:
                            logger.debug(f"[OpenClaw] Other event received: {event_name}, data: {data}")
                        # Any event counts as server activity
                        continue

                    if msg_type != "res":
                        logger.debug(f"[OpenClaw] Non-res message type: {msg_type}, data: {data}")
                        continue

                    req_id = data.get("id")
                    if not req_id:
                        continue

                    future = cls._pending.get(req_id)
                    logger.debug(
                        f"[OpenClaw] _receiver processing res: req_id={req_id}, has_future={future is not None}, future_done={future.done() if future else None}"
                    )
                    if future and not future.done():
                        logger.debug(f"[OpenClaw] _receiver setting result for req_id={req_id}")
                        # Complete the Future on its owner loop to avoid cross-loop wakeup delays.
                        fut_loop = future.get_loop()
                        fut_loop.call_soon_threadsafe(future.set_result, data)
                        # Note: OpenClaw may send a second res for agent method (completed status)
                        # We keep the entry in _pending to handle potential second response
                    elif future and future.done():
                        # OpenClaw sends second res for agent method, keep logs lightweight.
                        payload = data.get("payload") if isinstance(data, dict) else {}
                        status = payload.get("status") if isinstance(payload, dict) else None
                        summary = payload.get("summary") if isinstance(payload, dict) else None
                        logger.debug(
                            f"[OpenClaw] _receiver ignoring second res for req_id={req_id}, status={status}, summary={summary}"
                        )
                except json.JSONDecodeError:
                    logger.warning(f"[OpenClaw] Failed to decode message: {message[:200]}")
        except asyncio.CancelledError:
            logger.debug("[OpenClaw] Receiver task cancelled")
            raise
        except Exception as e:
            logger.warning(f"[OpenClaw] Receiver error: {type(e).__name__}: {e}")
        finally:
            cls._connected = False
            cls._trigger_reconnect()

    @classmethod
    def _trigger_reconnect(cls):
        """Trigger reconnection if enabled and not manually closed."""
        if not cls._should_reconnect or not cls._reconnect_enabled or not cls._enabled:
            return
        if cls._reconnect_task is None or cls._reconnect_task.done():
            cls._reconnect_task = asyncio.create_task(cls._reconnect())

    @classmethod
    async def _reconnect(cls):
        """Background task to reconnect with exponential backoff."""
        while cls._should_reconnect and cls._enabled and not cls._connected:
            cls._reconnect_attempts += 1
            delay = min(
                cls._reconnect_delay * (2 ** (cls._reconnect_attempts - 1)),
                cls._reconnect_max_delay
            )
            logger.info(f"[OpenClaw] Reconnecting in {delay}s (attempt #{cls._reconnect_attempts})...")
            await asyncio.sleep(delay)

            if cls._connected:
                break

            try:
                success = await cls.connect()
                if success:
                    logger.info(f"[OpenClaw] Reconnected successfully after {cls._reconnect_attempts} attempts")
                    break
            except Exception as e:
                logger.warning(f"[OpenClaw] Reconnect attempt failed: {e}")

    @classmethod
    async def _heartbeat(cls):
        """Background task to monitor connection health.

        Uses WebSocket built-in ping/pong (handled automatically by websockets library)
        and monitors server activity (tick events or any messages).
        """
        try:
            while cls._connected and cls._websocket:
                await asyncio.sleep(cls._heartbeat_interval)

                if not cls._connected or not cls._websocket:
                    break

                # Check if we've heard from the server recently
                current_time = asyncio.get_event_loop().time()
                silence_duration = current_time - cls._last_tick_time

                if silence_duration > cls._tick_timeout:
                    logger.warning(
                        f"[OpenClaw] Connection appears dead (no activity for {silence_duration:.0f}s), "
                        f"triggering reconnect"
                    )
                    cls._connected = False
                    cls._trigger_reconnect()
                    break

                logger.debug(f"[OpenClaw] Connection healthy (last activity {silence_duration:.0f}s ago)")
        except asyncio.CancelledError:
            logger.debug("[OpenClaw] Heartbeat task cancelled")
            raise
        except Exception as e:
            logger.error(f"[OpenClaw] Heartbeat error: {e}")
            cls._connected = False
            cls._trigger_reconnect()

    @classmethod
    def is_connected(cls) -> bool:
        """Check if connected to OpenClaw."""
        return cls._connected and cls._websocket is not None

    @classmethod
    def is_enabled(cls) -> bool:
        """Check if OpenClaw is enabled."""
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._enabled

    @classmethod
    def is_tts_enabled(cls) -> bool:
        """Check if TTS playback is enabled."""
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._tts_enabled

    @classmethod
    def _signal_response_ready(cls, run_id: str):
        """Mark response waiter as ready in a thread-safe way."""
        waiter = cls._response_events.get(run_id)
        if not waiter or waiter.done():
            return
        fut_loop = waiter.get_loop()
        fut_loop.call_soon_threadsafe(waiter.set_result, None)

    @classmethod
    async def _handle_agent_event(cls, event_data: dict):
        """Handle agent events to extract response text for TTS playback."""
        try:
            event_name = event_data.get("event", "")
            payload = event_data.get("payload", {})
            run_id = payload.get("runId")

            # Debug: log all agent events
            logger.debug(f"[OpenClaw] Received event '{event_name}' for runId={run_id}")
            logger.debug(f"[OpenClaw] Event payload: {payload}")

            if event_name == "run.completed":
                # Final response
                output = payload.get("output", {})
                response_text = output.get("text", "")

                if run_id and response_text:
                    if run_id in cls._response_events:
                        logger.info(f"[OpenClaw] ✅ Final response for {run_id}: {response_text[:100]}...")
                    cls._response_texts[run_id] = response_text
                    cls._signal_response_ready(run_id)

            elif event_name == "run.output":
                # Streaming output
                output = payload.get("output", {})
                chunk_text = output.get("text", "")

                if run_id and chunk_text:
                    logger.debug(f"[OpenClaw] Output chunk for {run_id}: {chunk_text[:50]}...")
                    # Accumulate text chunks
                    current_text = cls._response_texts.get(run_id, "")
                    cls._response_texts[run_id] = current_text + chunk_text

            elif event_name == "run.text":
                # Text event
                text = payload.get("text", "")

                if run_id and text:
                    if run_id in cls._response_events:
                        logger.info(f"[OpenClaw] ✅ Text event for {run_id}: {text[:100]}...")
                    cls._response_texts[run_id] = text
                    cls._signal_response_ready(run_id)

            elif event_name == "agent":
                # Agent event from OpenClaw (stream: lifecycle or assistant)
                stream = payload.get("stream", "")
                data = payload.get("data", {})

                if stream == "assistant":
                    # Streaming text from assistant
                    text = data.get("text", "")
                    delta = data.get("delta", "")

                    if run_id and text:
                        logger.debug(
                            f"[OpenClaw] Agent assistant stream for {run_id}: text len={len(text)}, delta_len={len(delta) if delta else 0}"
                        )
                        # Accumulate or store the text
                        cls._response_texts[run_id] = text
                        # Don't set event yet, wait for lifecycle end

                elif stream == "lifecycle":
                    phase = data.get("phase", "")

                    if phase == "end":
                        # Agent run completed
                        response_text = cls._response_texts.get(run_id, "")
                        if run_id and response_text and run_id in cls._response_events:
                            logger.info(f"[OpenClaw] ✅ Agent completed for {run_id}: {response_text}")
                        cls._signal_response_ready(run_id)

        except Exception as e:
            logger.error(f"[OpenClaw] Error handling agent event: {e}")
            logger.debug(f"[OpenClaw] Event data: {event_data}")

    @classmethod
    async def _wait_and_play_response(cls, run_id: str):
        """Wait for agent response and optionally play via TTS."""
        try:
            logger.debug(f"[OpenClaw] Waiting for response (runId: {run_id})...")

            event = cls._response_events.get(run_id)
            if not event:
                logger.warning(f"[OpenClaw] No event found for run {run_id}")
                return

            # Wait for response with timeout
            try:
                await asyncio.wait_for(event, timeout=cls._response_timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[OpenClaw] Timeout waiting for response (runId: {run_id})")
                # Still try to play whatever we got

            response_text = cls._response_texts.get(run_id, "")

            # Clean up
            cls._response_events.pop(run_id, None)
            cls._response_texts.pop(run_id, None)

            if response_text:
                if cls._tts_enabled:
                    logger.debug(f"[OpenClaw] Playing response via TTS: {response_text[:100]}...")
                    await cls._play_response_with_tts(response_text)
                else:
                    logger.info(f"[OpenClaw] Received response (TTS disabled): {response_text[:100]}...")
            else:
                logger.warning(f"[OpenClaw] No response text received for run {run_id}")

        except Exception as e:
            logger.error(f"[OpenClaw] Error waiting/playing response: {e}")
            # Clean up on error
            cls._response_events.pop(run_id, None)
            cls._response_texts.pop(run_id, None)

    @classmethod
    async def _play_response_with_tts(cls, text: str):
        """Synthesize text using Doubao TTS and play through speaker."""
        try:
            from config import APP_CONFIG
            from core.services.tts.doubao import DoubaoTTS
            from core.ref import get_speaker

            # Get TTS config
            tts_config = APP_CONFIG.get("tts", {}).get("doubao", {})
            app_id = tts_config.get("app_id")
            access_key = tts_config.get("access_key")

            if not app_id or not access_key:
                logger.error("[OpenClaw] Doubao TTS credentials not configured, cannot play response")
                # Fallback to native TTS
                speaker = get_speaker()
                if speaker:
                    await speaker.play(text=text, blocking=cls._blocking_playback)
                return

            # Use custom speaker if configured, otherwise fall back to default
            speaker_id = cls._tts_speaker or tts_config.get("default_speaker", "zh_female_xiaohe_uranus_bigtts")

            # Create TTS instance
            tts = DoubaoTTS(
                app_id=app_id,
                access_key=access_key,
                speaker=speaker_id,
            )

            # Synthesize in thread pool to not block
            loop = asyncio.get_event_loop()
            audio_data = await loop.run_in_executor(
                None, lambda: tts.synthesize(text, speed=1.0)
            )

            if not audio_data:
                logger.error("[OpenClaw] TTS synthesis returned empty audio")
                return

            logger.debug(f"[OpenClaw] TTS synthesized: {len(audio_data)} bytes")

            # Play through speaker
            speaker = get_speaker()
            if not speaker:
                logger.error("[OpenClaw] Speaker not available")
                return

            # Play audio buffer (blocking or non-blocking based on config)
            blocking = cls._blocking_playback
            await speaker.play(buffer=audio_data, blocking=blocking)
            if blocking:
                logger.debug("[OpenClaw] Response playback completed")
            else:
                logger.debug("[OpenClaw] Response playback started (non-blocking)")

        except Exception as e:
            logger.error(f"[OpenClaw] Error playing response with TTS: {e}")
            # Fallback to native TTS on error
            try:
                from core.ref import get_speaker
                speaker = get_speaker()
                if speaker:
                    await speaker.play(text=text, blocking=cls._blocking_playback)
            except Exception as fallback_error:
                logger.error(f"[OpenClaw] Fallback TTS also failed: {fallback_error}")


# No auto-initialization - call OpenClawManager.initialize_from_config() explicitly
