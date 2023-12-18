import aiohttp
import asyncio
import json

from .utils import parse_device_type
from . import constants
from .user import User
from .device import Device
from .home import Home
from .broker import AlisteBroker


class AlisteHub:
    home: Home

    def __init__(self):
        self.http = aiohttp.ClientSession()
        self.background_tasks = set()
        self.broker = AlisteBroker()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *excinfo):
        await self.http.close()
        await self.broker.disconnect()

    async def init(self, username: str, password: str):
        await self._authenticate(username, password)
        await self.get_home_details()
        await self._init_broker()

    async def _init_broker(self):
        topics_set = set()

        for device in self.home.devices:
            topics_set.add(f"message/{device.deviceId}")

        self.broker.set_topics(list(topics_set))
        loop = asyncio.get_event_loop()
        # Listen for mqtt messages in an (unawaited) asyncio task
        task = loop.create_task(self.broker.connect(self.get_credentials))
        # Save a reference to the task so it doesn't get garbage collected
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.remove)

    async def get_credentials(self):
        try:
            await self._authenticate_cognito()
        except:  # noqa: E722
            print("Failed to fetch credentials")
        return self.user.credentials

    async def _authenticate_cognito(self):
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

        response = await self.http.post(
            constants.cognitoUrl, data=json.dumps(payload), headers=headers
        )

        if response.status != 200:
            raise Exception("Authentication failed")

        data = await response.json(content_type=None)
        credentials = data["Credentials"]
        return credentials

    async def _authenticate(self, mobile: str, password: str):
        credentials = await self._authenticate_cognito()

        payload = {
            "mobile": mobile,
            "password": password,
        }

        response = await self.http.post(constants.loginUrl, json=payload)

        if response.status == 200:
            json = await response.json()
            data = json["data"]
            self.user = User(
                accesstoken=data["accesstoken"],
                email=data["profile"]["email"],
                name=data["profile"]["name"],
                homeId=data["profile"]["selectedHouse"],
                mobile=data["profile"]["mobile"],
                credentials=credentials,
            )
            self.username = mobile
            self.password = password
        else:
            raise Exception("Authentication failed")

    # Get home details
    async def get_home_details(self):
        response = await self.http.get(
            f"{constants.homeDetailsUrl}/{self.user.homeId}/{self.user.mobile}"
        )
        resp = await response.json()
        await self.process_home_details(resp)

    async def process_home_details(self, json_data):
        devices: list[Device] = []

        for room in json_data["rooms"]:
            for device in room["devices"]:
                for switch in device["switches"]:
                    item = Device(
                        deviceId=device["deviceId"],
                        switchId=switch["switchId"],
                        name=switch["switchName"],
                        type=parse_device_type(switch["deviceType"]),
                        switchState=switch["switchState"],
                        dimmable=switch["dimmable"],
                        wattage=switch["wattage"],
                        roomName=room["roomName"],
                        broker=self.broker,
                    )
                    devices.append(item)

        self.home = Home(
            id=json_data["_id"], name=json_data["houseName"], devices=devices
        )
