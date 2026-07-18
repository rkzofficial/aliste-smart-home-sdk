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
        self.commandTopics: list[str] = []
        self._client: Client | None = None
        self._http_session = http_session
        self._command_token: str = ""
        self._command_user: str = ""

    def attach_http_session(self, session: ClientSession) -> None:
        self._http_session = session

    def set_command_auth(self, token: str, user: str) -> None:
        """Provide the credentials used for authenticated REST commands."""
        self._command_token = token
        self._command_user = user

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

    def set_topics(self, topics: list[str]) -> None:
        self.commandTopics = topics

    async def on_connect(self) -> None:
        self.connected = True

        for topic in self.commandTopics:
            if self._client is None:
                raise ApiError("Broker client is not available during subscription.")
            await self._client.subscribe(topic)

    def on_message(self, message: Message) -> None:
        topic = getattr(message.topic, "value", str(message.topic))
        device_id = topic.split("/")[1]
        parsed = json.loads(message.payload.decode("utf-8"))
        data: BrokerMessage = {
            "deviceId": device_id,
            "switchId": int(parsed["sid"] or 0),
            "state": float(parsed["s"] or 0) / 100,
        }
        for callback in self.callbacks:
            callback(data)

    def message(self, data: BrokerMessage) -> None:
        for callback in self.callbacks:
            callback(data)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def send_command(self, payload: CommandPayload) -> None:
        """Deliver an on/off/dim command via the authenticated control endpoint.

        Commands are delivered over HTTP to ``/v3/device/control`` (the same
        mechanism the mobile app uses). MQTT is used only for receiving status
        updates, not for issuing commands.
        """
        if self._http_session is None:
            raise ApiError(
                "No HTTP session is attached to the broker for command delivery."
            )

        # The control endpoint expects the command level on a 0-100 scale
        # (100 = on, 0 = off), matching the mobile app; the SDK works with a
        # normalised 0.0-1.0 value internally.
        body = {
            "deviceId": payload["deviceId"],
            "switchId": payload["switchId"],
            "command": int(round(payload["command"] * 100)),
        }
        url = f"{constants.commandUrl}?user={self._command_user}"
        headers: dict[str, str] = {}
        if self._command_token:
            headers = {
                "accesstoken": self._command_token,
                "Authorization": f"Bearer {self._command_token}",
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

        self.message(
            {
                "deviceId": payload["deviceId"],
                "switchId": payload["switchId"],
                "state": float(payload["command"]),
            }
        )
