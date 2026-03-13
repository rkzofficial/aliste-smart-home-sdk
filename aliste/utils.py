from __future__ import annotations

from .enums import DeviceType


def parse_device_type(device_type: int) -> DeviceType:
    if device_type in {2, 6}:
        return DeviceType.LIGHT
    if device_type == 0:
        return DeviceType.FAN
    return DeviceType.SWITCH
