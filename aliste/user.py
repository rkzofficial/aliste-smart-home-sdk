from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class User:
    accesstoken: str
    email: str
    name: str
    homeId: str
    mobile: str
    credentials: dict[str, str]
