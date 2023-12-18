import asyncio
import ssl
import json
import certifi
import aws_signv4_mqtt
from urllib.parse import urlparse
from aiohttp import ClientSession
from aiomqtt import Client, Message, MqttError

from . import constants


def isAsync(someFunc):
    return asyncio.iscoroutinefunction(someFunc)


class AlisteBroker:
    reconnect_interval = 5  # In seconds
    connected = False

    def __init__(self):
        self.http = ClientSession()

        self.callbacks = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *excinfo):
        await self.disconnect()

    async def connect(self, get_credentials):
        while True:
            credentials = await get_credentials()

            ws_url = aws_signv4_mqtt.generate_signv4_mqtt(
                f"{constants.iotId}.iot.{constants.region}.amazonaws.com",
                constants.region,
                credentials["AccessKeyId"],
                credentials["SecretKey"],
                session_token=credentials["SessionToken"],
            )

            urlparts = urlparse(ws_url)

            # Host header needs to be set, port is not included in signed host header so should not be included here.
            # No idea what it defaults to but whatever that it seems to be wrong.
            headers = {
                "Host": "{0:s}".format(urlparts.netloc),
            }

            self.client = Client(
                hostname=urlparts.netloc,
                port=443,
                transport="websockets",
                websocket_path="{}?{}".format(urlparts.path, urlparts.query),
                websocket_headers=headers,
                tls_context=ssl.create_default_context(cafile=certifi.where()),
            )
            try:
                async with self.client:
                    async with self.client.messages() as messages:
                        await self.on_connect()

                        async for message in messages:
                            self.on_message(message)

            except MqttError as error:
                self.connected = False

                print(
                    f'Error "{error}". Reconnecting in {self.reconnect_interval} seconds.'
                )

                await asyncio.sleep(self.reconnect_interval)

    async def disconnect(self):
        await self.http.close()

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def set_topics(self, topics: list[str]):
        self.commandTopics = topics

    async def on_connect(self):
        self.connected = True

        for topic in self.commandTopics:
            # print("Subscribing to topic: ", topic)
            await self.client.subscribe(topic)

    def on_message(self, message: Message):
        deviceId = message.topic.value.split("/")[1]
        parsed = json.loads(message.payload.decode("utf-8"))
        data = {
            "deviceId": deviceId or 0,
            "switchId": parsed["sid"] or 0,
            "state": (parsed["s"] or 0) / 100,
        }
        for callback in self.callbacks:
            callback(data)

    def message(self, data):
        for callback in self.callbacks:
            callback(data)

    @property
    def is_connected(self):
        return self.connected

    async def send_command(self, payload: dict):
        if self.is_connected:
            deviceId = payload["deviceId"]
            switchId = payload["switchId"]
            command = payload["command"]
            await self.client.publish(
                f"control/{deviceId}", f"{switchId},{command * 100}"
            )
        else:
            await self.http.post(constants.commandUrl, json=payload)
            payload["state"] = payload["command"]
            self.message(payload)
