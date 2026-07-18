from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import TypedDict
from urllib.parse import urlparse

import aws_signv4_mqtt  # type: ignore[import-untyped]
import certifi
from aiohttp import ClientSession
from aiomqtt import Client, Message, MqttError

from . import constants
from .errors import ApiError

logger = logging.getLogger(__name__)

CredentialProvider = Callable[[], Awaitable[dict[str, str]]]
MessageCallback = Callable[["BrokerMessage"], None]
PresenceCallback = Callable[[str, bool], None]


class BrokerMessage(TypedDict):
    deviceId: str
    switchId: int
    state: float


class CommandPayload(TypedDict):
    deviceId: str
    switchId: int
    command: float


class AlisteBroker:
    def __init__(
        self,
        *,
        http_session: ClientSession | None = None,
        reconnect_interval: float = 5.0,
    ) -> None:
        self.reconnect_interval = reconnect_interval
        self._closing = False
        self.connected = False
        self.callbacks: set[MessageCallback] = set()
        self.presence_callbacks: set[PresenceCallback] = set()
        self._device_ids: list[str] = []
        self._client: Client | None = None
        self._http_session = http_session
        self._command_token: str = ""
        self._command_user: str = ""
        self._controller_id: str = "app"
        self._socket: object | None = None

    def attach_http_session(self, session: ClientSession) -> None:
        self._http_session = session

    def set_socket(self, socket: object | None) -> None:
        """Attach the realtime socket used for control when available."""
        self._socket = socket

    def set_command_auth(
        self, token: str, user: str, controller_id: str = "app"
    ) -> None:
        """Provide the identity used for authenticated commands.

        ``controller_id`` is the "name(email)" string the app tags commands with.
        """
        self._command_token = token
        self._command_user = user
        self._controller_id = controller_id or "app"

    def set_devices(self, device_ids: list[str]) -> None:
        """Register the devices whose status this broker should track."""
        self._device_ids = list(dict.fromkeys(device_ids))

    def _subscribe_topics(self) -> list[str]:
        topics: list[str] = []
        for device_id in self._device_ids:
            topics.append(f"message/{device_id}")
            topics.append(f"e/sync/{device_id}")
            topics.append(f"e/conn/{device_id}")
            topics.append(f"$aws/events/presence/connected/{device_id}")
            topics.append(f"$aws/events/presence/disconnected/{device_id}")
        return topics

    async def connect(self, get_credentials: CredentialProvider) -> None:
        self._closing = False

        while not self._closing:
            try:
                credentials = await get_credentials()

                ws_url = aws_signv4_mqtt.generate_signv4_mqtt(
                    f"{constants.iotId}.iot.{constants.region}.amazonaws.com",
                    constants.region,
                    credentials["AccessKeyId"],
                    credentials["SecretKey"],
                    session_token=credentials["SessionToken"],
                )

                urlparts = urlparse(ws_url)

                # The signed Host header must match the websocket endpoint exactly.
                headers = {
                    "Host": f"{urlparts.netloc:s}",
                }
                # Building the SSL context reads the CA bundle from disk, which is
                # blocking; run it in the default executor to avoid stalling the
                # event loop (HA flags this as a blocking call otherwise).
                loop = asyncio.get_running_loop()
                tls_context = await loop.run_in_executor(
                    None, lambda: ssl.create_default_context(cafile=certifi.where())
                )
                async with Client(
                    hostname=urlparts.netloc,
                    port=443,
                    transport="websockets",
                    websocket_path=f"{urlparts.path}?{urlparts.query}",
                    websocket_headers=headers,
                    tls_context=tls_context,
                ) as client:
                    self._client = client
                    await self.on_connect()

                    async for message in client.messages:
                        if self._closing:
                            break
                        self.on_message(message)
            except asyncio.CancelledError:
                self.connected = False
                self._client = None
                raise
            except MqttError as error:
                self.connected = False
                self._client = None
                if self._closing:
                    break
                logger.warning(
                    'MQTT error "%s". Reconnecting in %s seconds.',
                    error,
                    self.reconnect_interval,
                )
                await asyncio.sleep(self.reconnect_interval)
            except Exception:
                self.connected = False
                self._client = None
                if self._closing:
                    break
                logger.exception(
                    "Unexpected broker failure. Reconnecting in %s seconds.",
                    self.reconnect_interval,
                )
                await asyncio.sleep(self.reconnect_interval)
            finally:
                self.connected = False
                self._client = None

    async def close(self) -> None:
        self._closing = True
        self.connected = False
        client = self._client
        if client is None:
            return

        try:
            await client.__aexit__(None, None, None)
        except Exception:
            logger.debug(
                "Ignoring broker shutdown error during disconnect.",
                exc_info=True,
            )
        finally:
            self._client = None

    def register_callback(self, callback: MessageCallback) -> None:
        self.callbacks.add(callback)

    def register_presence_callback(self, callback: PresenceCallback) -> None:
        self.presence_callbacks.add(callback)

    async def on_connect(self) -> None:
        self.connected = True

        if self._client is None:
            raise ApiError("Broker client is not available during subscription.")

        for topic in self._subscribe_topics():
            await self._client.subscribe(topic)

        # Ask every device to report its current state now that we are listening.
        await self.request_status()

    async def request_status(self, device_ids: list[str] | None = None) -> None:
        """Publish an empty message to ``status/{deviceId}`` to pull live state."""
        if self._client is None or not self.connected:
            return
        for device_id in device_ids or self._device_ids:
            try:
                await self._client.publish(f"status/{device_id}", "{}")
            except MqttError:
                logger.debug("Failed to request status for %s", device_id)

    def on_message(self, message: Message) -> None:
        topic = getattr(message.topic, "value", str(message.topic))
        try:
            self._dispatch(topic, message.payload)
        except Exception:
            logger.debug("Failed to handle message on %s", topic, exc_info=True)

    def _dispatch(self, topic: str, raw: bytes | bytearray | str | None) -> None:
        # Presence events: $aws/events/presence/{connected|disconnected}/{deviceId}
        if topic.startswith("$aws"):
            parts = topic.split("/")
            state, device_id = parts[-2], parts[-1]
            self._emit_presence(device_id, state == "connected")
            return

        payload = self._decode(raw)

        # Strip the "e/" prefix used for sync/conn topics.
        normalized = topic[2:] if topic.startswith("e/") else topic
        parts = normalized.split("/")
        kind, device_id = parts[0], (parts[1] if len(parts) > 1 else "")

        if kind == "message":
            if isinstance(payload, dict):
                self._emit_state(
                    device_id, payload.get("sid"), payload.get("s")
                )
        elif kind in ("sync", "conn"):
            if isinstance(payload, dict):
                self._emit_sync(device_id, payload.get("ls"))

    @staticmethod
    def _decode(raw: bytes | bytearray | str | None) -> object:
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _emit_state(self, device_id: str, sid: object, s: object) -> None:
        if sid is None or s is None:
            return
        data: BrokerMessage = {
            "deviceId": device_id,
            "switchId": int(sid),
            "state": float(s) / 100.0,
        }
        for callback in self.callbacks:
            callback(data)

    def _emit_sync(self, device_id: str, ls: object) -> None:
        """Handle a bulk state list from a sync/conn message.

        ``ls`` is a comma-separated string of levels indexed by switchId, e.g.
        ``"0,100,0,100"`` → switch 0 = 0, switch 1 = 100, … (matching the app's
        ``ls.split(',')[switchId]``). A list is also tolerated defensively.
        """
        if isinstance(ls, str):
            values: list[object] = [v for v in ls.split(",")]
        elif isinstance(ls, list):
            values = ls
        else:
            return
        for switch_id, level in enumerate(values):
            if level is None or level == "":
                continue
            self._emit_state(device_id, switch_id, level)

    def _emit_presence(self, device_id: str, online: bool) -> None:
        for callback in self.presence_callbacks:
            callback(device_id, online)

    def message(self, data: BrokerMessage) -> None:
        for callback in self.callbacks:
            callback(data)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def send_command(self, payload: CommandPayload) -> None:
        """Send an on/off/dim command via the authenticated control endpoint.

        Verified against a live device: the app's ``POST /v3/device/control``
        with the full body (``controllerType``/``controllerId``/
        ``controllerDetails``) and the ``accesstoken`` header actuates the
        device. Commands use a 0-100 level scale (100 = on); the SDK works with a
        normalised 0.0-1.0 value internally.
        """
        if self._http_session is None:
            raise ApiError(
                "No HTTP session is attached to the broker for command delivery."
            )

        device_id = payload["deviceId"]
        switch_id = payload["switchId"]
        level = int(round(payload["command"] * 100))

        # Control goes over the authenticated REST endpoint (the proven-reliable
        # path). The realtime socket is used only as a status source.
        url = f"{constants.commandUrl}?user={self._command_user}"
        headers: dict[str, str] = {}
        if self._command_token:
            headers = {
                "accesstoken": self._command_token,
                "Authorization": f"Bearer {self._command_token}",
            }
        body = {
            "deviceId": device_id,
            "switchId": switch_id,
            "command": level,
            "controllerType": "app",
            "controllerId": self._controller_id,
            "controllerDetails": {},
        }
        async with self._http_session.post(
            url, json=body, headers=headers
        ) as response:
            if response.status >= 400:
                text = await response.text()
                raise ApiError(
                    f"Command delivery failed with status {response.status}: "
                    f"{text[:200]}"
                )

        # Reflect the requested state immediately; the device echoes the real
        # value back over MQTT shortly after.
        self.message(
            {
                "deviceId": device_id,
                "switchId": switch_id,
                "state": float(payload["command"]),
            }
        )
