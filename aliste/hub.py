import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from aiohttp import ClientSession

from . import constants
from .broker import AlisteBroker
from .device import Device
from .errors import AlisteError, ApiError, AuthenticationError
from .home import Home
from .user import User
from .utils import parse_device_type

logger = logging.getLogger(__name__)


class AlisteHub:
    def __init__(self) -> None:
        self.broker = AlisteBroker()
        self.home: Home | None = None
        self.http: ClientSession | None = None
        self.user: User | None = None
        self._broker_task: asyncio.Task[None] | None = None
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
                    self.user.accesstoken, self.user.userId or self.user.mobile
                )
            await self.get_home_details()
            await self._init_broker()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
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

        topics_set = set()

        for device in self.home.devices:
            topics_set.add(f"message/{device.deviceId}")

        self.broker.set_topics(sorted(topics_set))
        self._broker_task = asyncio.create_task(
            self.broker.connect(self.get_credentials)
        )

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
            # Diagnostic: surface the response shape so we can identify the
            # correct user identifier expected by the command endpoint.
            logger.warning(
                "ALISTE-DBG login data keys=%s profile keys=%s _id=%r userId=%r",
                sorted(data.keys()), sorted(profile.keys()),
                profile.get("_id"), profile.get("userId") or data.get("userId"),
            )
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

    async def get_home_details(self) -> Home:
        if self.http is None or self.user is None:
            raise AuthenticationError("Authenticate before requesting home details.")

        async with self.http.get(
            f"{constants.homeDetailsUrl}/{self.user.homeId}/{self.user.mobile}"
        ) as response:
            if response.status != 200:
                raise ApiError(
                    f"Fetching home details failed with status {response.status}."
                )
            payload = await response.json()

        self.home = self.process_home_details(payload)
        return self.home

    def process_home_details(self, json_data: dict[str, Any]) -> Home:
        devices: list[Device] = []

        for room in json_data["rooms"]:
            for device in room["devices"]:
                # Diagnostic: dump the raw device shape (keys + a sample) so we
                # can see gateway/mac fields the command endpoint may require.
                if str(device.get("deviceId", "")).startswith("S0308"):
                    logger.warning(
                        "ALISTE-DBG device keys=%s device=%s",
                        sorted(device.keys()),
                        {k: v for k, v in device.items() if k != "switches"},
                    )
                for switch in device["switches"]:
                    item = Device(
                        deviceId=device["deviceId"],
                        switchId=int(switch["switchId"]),
                        name=switch["switchName"],
                        type=parse_device_type(switch["deviceType"]),
                        switchState=float(switch["switchState"]),
                        dimmable=switch["dimmable"],
                        wattage=int(switch["wattage"]),
                        roomName=room["roomName"],
                        broker=self.broker,
                    )
                    devices.append(item)

        return Home(id=json_data["_id"], name=json_data["houseName"], devices=devices)
