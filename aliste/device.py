from __future__ import annotations

from collections.abc import Callable

from .broker import AlisteBroker, BrokerMessage, CommandPayload
from .enums import DeviceType

DeviceCallback = Callable[[], None]


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
    ) -> None:
        self.deviceId = deviceId
        self.switchId = switchId
        self.name = name
        self.type = type
        self.switchState = switchState
        self.dimmable = dimmable
        self.wattage = wattage
        self.roomName = roomName
        self.broker = broker
        self.online = True

        self._callbacks: set[DeviceCallback] = set()
        self.broker.register_callback(self.on_message)
        self.broker.register_presence_callback(self.on_presence)

    @property
    def id(self) -> str:
        return self.deviceId

    @property
    def available(self) -> bool:
        """Whether the physical device is currently reachable (MQTT present)."""
        return self.online

    @property
    def is_on(self) -> bool:
        return self.switchState > 0

    def on_message(self, message: BrokerMessage) -> None:
        if (
            message["deviceId"] == self.deviceId
            and message["switchId"] == self.switchId
        ):
            # Any status message implies the device is reachable.
            self.online = True
            self.on_state_change(message["state"])

    def on_presence(self, device_id: str, online: bool) -> None:
        if device_id == self.deviceId:
            self.online = online
            self.refresh()

    def refresh(self) -> None:
        for callback in self._callbacks:
            callback()

    def register_callback(self, callback: DeviceCallback) -> None:
        self._callbacks.add(callback)

    def remove_callback(self, callback: DeviceCallback) -> None:
        self._callbacks.discard(callback)

    def on_state_change(self, state: float) -> None:
        self.switchState = float(state)
        self.refresh()

    def build_command(self, command: float) -> CommandPayload:
        return {
            "deviceId": self.deviceId,
            "switchId": self.switchId,
            "command": command,
        }

    async def turn_on(self) -> None:
        await self.broker.send_command(self.build_command(1))

    async def turn_off(self) -> None:
        await self.broker.send_command(self.build_command(0))

    async def dim(self, value: float) -> None:
        await self.broker.send_command(self.build_command(value))

    async def refresh_state(self) -> None:
        """Request the device's current state from the cloud/broker."""
        await self.broker.request_status([self.deviceId])
