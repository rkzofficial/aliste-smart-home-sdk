from __future__ import annotations

from dataclasses import dataclass

from .device import Device


@dataclass(slots=True)
class Home:
    id: str
    name: str
    devices: list[Device]

    def get_device(self, device_id: str) -> Device | None:
        for device in self.devices:
            if device.deviceId == device_id:
                return device
        return None
