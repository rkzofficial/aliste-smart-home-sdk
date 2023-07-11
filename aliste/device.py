from .enums import DeviceType
from .broker import AlisteBroker


class Device:
    def __init__(
        self,
        deviceId: str,
        switchId: int,
        name: str,
        type: DeviceType,
        switchState: float,
        dimmable: bool,
        wattage: int,
        roomName: str,
        broker: AlisteBroker,
    ):
        self.deviceId = deviceId
        self.switchId = switchId
        self.name = name
        self.type = type
        self.switchState = switchState
        self.dimmable = dimmable
        self.wattage = wattage
        self.roomName = roomName
        self.broker = broker

        self.broker.register_callback(self.on_message)

    def on_message(self, message):
        if (
            message["deviceId"] == self.deviceId
            and message["switchId"] == self.switchId
        ):
            self.on_state_change(message["state"])

    def on_state_change(self, state):
        self.switchState = float(state)

    def build_command(self, command: int):
        return {
            "deviceId": self.deviceId,
            "switchId": self.switchId,
            "command": command,
        }

    async def turn_on(self):
        await self.broker.send_command(self.build_command(1))

    async def turn_off(self):
        await self.broker.send_command(self.build_command(0))

    async def dim(self, value: int):
        await self.broker.send_command(self.build_command(value))
