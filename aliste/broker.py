import socketio
import urllib
import asyncio
from aiohttp import ClientSession

from . import constants


def isAsync(someFunc):
    return asyncio.iscoroutinefunction(someFunc)


class AlisteBroker:
    def __init__(self):
        self.sio = socketio.AsyncClient()
        self.http = ClientSession()

        self.sio.on("message", self.message)

        self.callbacks = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *excinfo):
        await self.disconnect()

    async def connect(self, homeId, mobile):
        params = {
            "node_mcu_id": "auto1001",
            "device_type": "phone",
            "house_access_code": homeId,
            "user": mobile,
        }

        url = constants.wssUrl + urllib.parse.urlencode(params)
        # await self.sio.connect(url=url, transports=["websocket"])

    async def disconnect(self):
        await self.sio.disconnect()
        await self.http.close()

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def message(self, data):
        for callback in self.callbacks:
            callback(data)

    @property
    def is_connected(self):
        return self.sio.connected

    async def send_command(self, command: int):
        # if self.is_connected:
        #     await self.sio.emit("message", command)
        # else:
        await self.http.post(constants.commandUrl, json=command)
        command["state"] = command["command"]
        self.message(command)
