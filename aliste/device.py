from .broker import AlisteBroker
from .enums import DeviceType


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

        self._callbacks = set()
        self.broker.register_callback(self.on_message)

    def on_message(self, message):
        if (
            message["deviceId"] == self.deviceId
            and message["switchId"] == self.switchId
        ):
            self.on_state_change(message["state"])

    def refresh(self):
        for callback in self._callbacks:
            callback()

    def register_callback(self, callback) -> None:
        """Register callback, called when Roller changes state."""
        self._callbacks.add(callback)

    def remove_callback(self, callback) -> None:
        """Remove previously registered callback."""
        self._callbacks.discard(callback)

    def on_state_change(self, state):
        self.switchState = float(state)
        self.refresh()

    def build_command(self, command: float):
        return {
            "deviceId": self.deviceId,
            "switchId": self.switchId,
            "command": command,
        }

    async def turn_on(self):
        await self.broker.send_command(self.build_command(1))

    async def turn_off(self):
        await self.broker.send_command(self.build_command(0))

    async def dim(self, value: float):
        await self.broker.send_command(self.build_command(value))
