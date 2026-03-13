# Aliste Smart Home SDK for Python

`aliste` 2.0 is an async-first SDK for discovering and controlling Aliste smart home devices from Python.

## Requirements

- Python 3.11 or newer
- An Aliste account with at least one configured home

## Install

```bash
uv add aliste
```

For a plain `pip` install:

```bash
pip install aliste
```

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run mypy aliste
uv build --no-sources
```

## Quickstart

```python
import asyncio

from aliste import AlisteHub


async def main() -> None:
    async with AlisteHub() as hub:
        await hub.connect("+91xxxxxxxxxx", "your-password")

        if hub.home is None:
            return

        print(f"Connected to {hub.home.name}")

        living_room_light = hub.home.get_device("device-id")
        if living_room_light is not None:
            await living_room_light.turn_on()


asyncio.run(main())
```

## Live Smoke Test

List the first few devices on a real account:

```bash
ALISTE_USERNAME="+91xxxxxxxxxx" ALISTE_PASSWORD="your-password" \
uv run python scripts/test_live.py --action list
```

Toggle a specific device:

```bash
ALISTE_USERNAME="+91xxxxxxxxxx" ALISTE_PASSWORD="your-password" \
uv run python scripts/test_live.py --action toggle --device-id your-device-id
```

## 2.0 Migration Notes

- The minimum supported Python version is now 3.11.
- Packaging and local development use `uv` instead of Poetry.
- `await hub.connect(username, password)` replaces `hub.init(...)`.
- `async with AlisteHub()` or `await hub.close()` now manages HTTP and MQTT lifecycle cleanly.
