from .enums import DeviceType


def parse_device_type(type: int):
    if type == 6 or type == 2:
        return DeviceType.LIGHT
    if type == 0:
        return DeviceType.FAN
    else:
        return DeviceType.SWITCH
