from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlencode

import socketio  # type: ignore[import-untyped]

from . import constants

logger = logging.getLogger(__name__)

StateCallback = Callable[[str, int, float], None]
PresenceCallback = Callable[[str, bool], None]


def _command_id() -> str:
    # The app uses String(Date.now()).slice(5,13); any short token works.
    import time

    return str(int(time.time() * 1000))[5:13]


class AlisteSocket:
    """Realtime control/status over the app's socket.io server (a2).

    Mirrors the mobile app: connects to ``wss://a2.alistetechnologies.com``,
    joins the house on connect, emits ``message`` to control, and receives
    ``message``/``conn_update``/``resync_states`` for live state. Falls back to
    REST at the broker level when a device is not present in the socket roster.
    """

    def __init__(self) -> None:
        self.sio = socketio.AsyncClient(
            reconnection=True, logger=False, engineio_logger=False
        )
        self.connected_devices: set[str] = set()
        self._house_id: str = ""
        self._email: str = ""
        self._username: str = ""
        self._state_cb: StateCallback | None = None
        self._presence_cb: PresenceCallback | None = None
        self._register_handlers()

    def set_callbacks(
        self, state_cb: StateCallback, presence_cb: PresenceCallback
    ) -> None:
        self._state_cb = state_cb
        self._presence_cb = presence_cb

    @property
    def connected(self) -> bool:
        return bool(self.sio.connected)

    def has_device(self, device_id: str) -> bool:
        return device_id in self.connected_devices

    async def connect(self, house_id: str, email: str, username: str) -> None:
        self._house_id = house_id
        self._email = email
        self._username = username or "app"
        query = urlencode(
            {
                "node_mcu_id": "auto1001",
                "device_type": "phone",
                "house_access_code": house_id,
                "user": email,
            }
        )
        url = f"{constants.wssUrl.rstrip('/?')}?{query}"
        await self.sio.connect(
            url, socketio_path="/socket.io", transports=["websocket"]
        )

    async def close(self) -> None:
        try:
            await self.sio.disconnect()
        except Exception:
            logger.debug("Socket disconnect error", exc_info=True)

    async def send_command(
        self, device_id: str, switch_id: int, level: int
    ) -> None:
        """Emit a control ``message`` (level is 0-100, 100 = on)."""
        await self.sio.emit(
            "message",
            {
                "deviceId": device_id,
                "device_type": "phone",
                "command": level,
                "switchId": switch_id,
                "user": self._email,
                "username": self._username,
                "id": _command_id(),
            },
        )

    def _register_handlers(self) -> None:
        sio = self.sio

        @sio.event
        async def connect() -> None:  # noqa: ANN202
            logger.debug("Socket connected; joining house %s", self._house_id)
            await sio.emit(
                "device_connection_update", {"houseAccessCode": self._house_id}
            )

        @sio.event
        async def disconnect() -> None:  # noqa: ANN202
            self.connected_devices.clear()

        @sio.on("device_connection_update")
        async def _roster(data: object) -> None:  # noqa: ANN202
            devices = data.get("devices") if isinstance(data, dict) else None
            if isinstance(devices, list):
                self.connected_devices = {
                    d.get("deviceId") if isinstance(d, dict) else d for d in devices
                }
                self.connected_devices.discard(None)

        @sio.on("message")
        async def _message(data: object) -> None:  # noqa: ANN202
            if not isinstance(data, dict):
                return
            device_id = data.get("deviceId")
            switch_id = data.get("switchId")
            state = data.get("state", data.get("s"))
            if device_id is None or switch_id is None or state is None:
                return
            self._emit_state(str(device_id), int(switch_id), float(state))

        @sio.on("conn_update")
        async def _conn(data: object) -> None:  # noqa: ANN202
            # payload: "{deviceId}-{online|offline}"
            text = str(data)
            if "-" in text:
                device_id, _, status = text.rpartition("-")
                online = status.strip().lower() == "online"
                if online:
                    self.connected_devices.add(device_id)
                else:
                    self.connected_devices.discard(device_id)
                if self._presence_cb:
                    self._presence_cb(device_id, online)

        @sio.on("resync_states")
        async def _resync(data: object) -> None:  # noqa: ANN202
            device_id = data.get("deviceId") if isinstance(data, dict) else None
            states = data.get("states") if isinstance(data, dict) else None
            if device_id is None:
                return
            values: list[object]
            if isinstance(states, str):
                values = list(states.split(","))
            elif isinstance(states, list):
                values = states
            else:
                return
            for switch_id, level in enumerate(values):
                if level in (None, ""):
                    continue
                self._emit_state(str(device_id), switch_id, float(level))

    def _emit_state(self, device_id: str, switch_id: int, level: float) -> None:
        # Socket state values follow the 0-100 scale; normalise to 0.0-1.0.
        normalised = level / 100.0 if level > 1 else level
        if self._state_cb:
            self._state_cb(device_id, switch_id, normalised)
