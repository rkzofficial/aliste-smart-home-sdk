from .device import Device


class Home:
    def __init__(self, id: str, name: str, devices: list[Device]):
        self.id = id
        self.name = name
        self.devices = devices

    def get_device(self, device_id: str):
        for device in self.devices:
            if device.id == device_id:
                return device
