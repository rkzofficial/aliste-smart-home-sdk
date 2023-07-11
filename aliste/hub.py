import aiohttp

from .utils import *
from . import constants
from .user import User
from .device import Device
from .home import Home
from .broker import AlisteBroker


class AlisteHub:
    home: Home

    def __init__(self):
        self.http = aiohttp.ClientSession()
        self.broker = AlisteBroker()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *excinfo):
        await self.http.close()
        await self.broker.disconnect()

    async def init(self, username: str, password: str):
        await self.authenticate(username, password)
        await self.get_home_details()
        await self.broker.connect(self.user.homeId, self.user.mobile)

    async def authenticate(self, mobile: str, password: str):
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
            )
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
