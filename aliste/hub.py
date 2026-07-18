import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from aiohttp import ClientSession

from . import constants
from .broker import AlisteBroker
from .device import Device
from .socket import AlisteSocket
from .errors import AlisteError, ApiError, AuthenticationError
from .home import Home
from .user import User
from .utils import parse_device_type

logger = logging.getLogger(__name__)


def _normalise_level(value: Any) -> float:
    """Return a switch level normalised to 0.0-1.0.

    The cloud reports levels on a 0-100 scale; the SDK works with 0.0-1.0 to
    match the live MQTT updates (which are divided by 100).
    """
    level = float(value)
    return level / 100.0 if level > 1 else level


class AlisteHub:
    def __init__(self, poll_interval: float = 30.0) -> None:
        self.broker = AlisteBroker()
        self.socket: AlisteSocket | None = None
        self.home: Home | None = None
        self.http: ClientSession | None = None
        self.user: User | None = None
        self._broker_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_interval = poll_interval
        self._devices_by_key: dict[tuple[str, int], Device] = {}
        self.username: str | None = None
        self.password: str | None = None

    async def __aenter__(self) -> "AlisteHub":
        return self

    async def __aexit__(self, *excinfo: object) -> None:
        await self.close()

    async def connect(self, username: str, password: str) -> None:
        await self.close()
        self.http = ClientSession()
        self.broker = AlisteBroker(http_session=self.http)
        try:
            credentials = await self._authenticate_cognito()
            await self._authenticate(username, password, credentials)
            if self.user is not None:
                self.broker.set_command_auth(
                    self.user.accesstoken,
                    self.user.userId or self.user.mobile,
                    f"{self.user.name}({self.user.email})",
                )
            await self.get_home_details()
            await self._init_broker()
            await self._init_socket()
            self._poll_task = asyncio.create_task(self._poll_states())
        except Exception:
            await self.close()
            raise

    async def _init_socket(self) -> None:
        """Connect the realtime socket used by the app (best-effort)."""
        if self.user is None:
            return
        socket = AlisteSocket()
        socket.set_callbacks(self._on_socket_state, self.broker._emit_presence)
        try:
            await socket.connect(
                self.user.homeId, self.user.email, self.user.name
            )
        except Exception:
            logger.debug("Realtime socket unavailable; using REST", exc_info=True)
            return
        self.socket = socket
        self.broker.set_socket(socket)

    def _on_socket_state(self, device_id: str, switch_id: int, state: float) -> None:
        self.broker.message(
            {"deviceId": device_id, "switchId": switch_id, "state": state}
        )

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close()
            self.socket = None
            self.broker.set_socket(None)

        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        if self._broker_task is not None:
            await self.broker.close()
            try:
                await asyncio.wait_for(self._broker_task, timeout=5)
            except TimeoutError:
                self._broker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._broker_task
            self._broker_task = None

        if self.http is not None and not self.http.closed:
            await self.http.close()
        self.http = None

    async def _init_broker(self) -> None:
        if self.home is None:
            raise AlisteError("Home details must be loaded before starting the broker.")

        device_ids = list(dict.fromkeys(d.deviceId for d in self.home.devices))
        self.broker.set_devices(device_ids)
        self._broker_task = asyncio.create_task(
            self.broker.connect(self.get_credentials)
        )

    async def _poll_states(self) -> None:
        """Periodically refresh device states from the cloud.

        MQTT/socket status delivery is not guaranteed for every change source
        (physical switch, official app, other cloud actions), so poll the house
        snapshot as a reliable source of truth and push any diffs to entities.
        """
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                payload = await self._fetch_home_payload()
                self._apply_states(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("State poll failed", exc_info=True)

    def _apply_states(self, json_data: dict[str, Any]) -> None:
        """Update existing Device objects from a fresh house payload."""
        for room in json_data.get("rooms", []):
            for device in room.get("devices", []):
                device_id = device.get("deviceId")
                for switch in device.get("switches", []):
                    key = (device_id, int(switch["switchId"]))
                    dev = self._devices_by_key.get(key)
                    if dev is None:
                        continue
                    level = _normalise_level(switch["switchState"])
                    if level != dev.switchState:
                        dev.on_state_change(level)

    async def get_credentials(self) -> dict[str, str]:
        if self.user is None:
            raise AuthenticationError("Authenticate before requesting credentials.")

        try:
            self.user.credentials = await self._authenticate_cognito()
        except AuthenticationError:
            logger.exception("Failed to refresh Cognito credentials.")
            raise
        return self.user.credentials

    async def _authenticate_cognito(self) -> dict[str, str]:
        if self.http is None:
            raise AlisteError("Hub is closed. Call connect() before using the SDK.")

        payload = {"IdentityId": constants.identityId}

        headers = {
            "x-amz-target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
            "cache-control": "no-store",
            "x-amz-user-agent": "aws-amplify/5.3.8 framework/201",
            "host": "cognito-identity.ap-south-1.amazonaws.com",
            "connection": "Keep-Alive",
            "user-agent": "okhttp/4.9.2",
            "content-type": "application/x-amz-json-1.1",
        }

        async with self.http.post(
            constants.cognitoUrl, data=json.dumps(payload), headers=headers
        ) as response:
            if response.status != 200:
                raise AuthenticationError(
                    f"Cognito credential exchange failed with status {response.status}."
                )

            data = await response.json(content_type=None)
            credentials = data.get("Credentials")
            if not isinstance(credentials, dict):
                raise AuthenticationError(
                    "Cognito response did not include credentials."
                )
            return {key: str(value) for key, value in credentials.items()}

    async def _authenticate(
        self, mobile: str, password: str, credentials: dict[str, str]
    ) -> None:
        if self.http is None:
            raise AlisteError("Hub is closed. Call connect() before using the SDK.")

        payload = {
            "mobile": mobile,
            "password": password,
        }

        async with self.http.post(constants.loginUrl, json=payload) as response:
            if response.status != 200:
                raise AuthenticationError(
                    f"Aliste login failed with status {response.status}."
                )

            payload_data = await response.json()
            data = payload_data["data"]
            profile = data["profile"]
            # The control endpoint is scoped by the account's user id (?user=),
            # which is the profile's Mongo _id; fall back to the mobile number.
            user_id = (
                profile.get("_id")
                or data.get("_id")
                or profile.get("userId")
                or data.get("userId")
                or str(profile["mobile"])
            )
            self.user = User(
                accesstoken=str(data["accesstoken"]),
                email=str(profile["email"]),
                name=str(profile["name"]),
                homeId=str(profile["selectedHouse"]),
                mobile=str(profile["mobile"]),
                credentials=credentials,
                userId=str(user_id),
            )
            self.username = mobile
            self.password = password

    async def _fetch_home_payload(self) -> dict[str, Any]:
        if self.http is None or self.user is None:
            raise AuthenticationError("Authenticate before requesting home details.")

        async with self.http.get(
            f"{constants.homeDetailsUrl}/{self.user.homeId}/{self.user.mobile}"
        ) as response:
            if response.status != 200:
                raise ApiError(
                    f"Fetching home details failed with status {response.status}."
                )
            return await response.json()

    async def get_home_details(self) -> Home:
        payload = await self._fetch_home_payload()
        self.home = self.process_home_details(payload)
        return self.home

    def process_home_details(self, json_data: dict[str, Any]) -> Home:
        devices: list[Device] = []
        self._devices_by_key = {}

        for room in json_data["rooms"]:
            for device in room["devices"]:
                for switch in device["switches"]:
                    item = Device(
                        deviceId=device["deviceId"],
                        switchId=int(switch["switchId"]),
                        name=switch["switchName"],
                        type=parse_device_type(switch["deviceType"]),
                        switchState=_normalise_level(switch["switchState"]),
                        dimmable=switch["dimmable"],
                        wattage=int(switch["wattage"]),
                        roomName=room["roomName"],
                        broker=self.broker,
                    )
                    devices.append(item)
                    self._devices_by_key[(item.deviceId, item.switchId)] = item

        return Home(id=json_data["_id"], name=json_data["houseName"], devices=devices)
