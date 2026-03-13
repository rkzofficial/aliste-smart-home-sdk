#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from aliste import AlisteHub


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live smoke test for the Aliste SDK against a real account."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("ALISTE_USERNAME"),
        help="Aliste mobile number. Defaults to ALISTE_USERNAME.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("ALISTE_PASSWORD"),
        help="Aliste password. Defaults to ALISTE_PASSWORD.",
    )
    parser.add_argument(
        "--action",
        choices=("list", "on", "off", "toggle"),
        default="list",
        help="Safe default is 'list'. Device actions require --device-id.",
    )
    parser.add_argument(
        "--device-id",
        help="Target device ID for on/off/toggle actions.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of devices to print in list mode.",
    )
    parser.add_argument(
        "--toggle-delay",
        type=float,
        default=2.0,
        help="Seconds to wait between on/off when using toggle.",
    )
    return parser


def require_credentials(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    if not args.username:
        parser.error("Missing username. Pass --username or set ALISTE_USERNAME.")
    if not args.password:
        parser.error("Missing password. Pass --password or set ALISTE_PASSWORD.")
    if args.action != "list" and not args.device_id:
        parser.error(f"--device-id is required when --action={args.action!r}.")


async def run_live_test(args: argparse.Namespace) -> None:
    async with AlisteHub() as hub:
        await hub.connect(args.username, args.password)

        if hub.home is None:
            raise RuntimeError(
                "Connected successfully but no home details were loaded."
            )

        print(f"Home: {hub.home.name} ({hub.home.id})")
        print(f"Devices: {len(hub.home.devices)}")

        if args.action == "list":
            for device in hub.home.devices[: args.limit]:
                print(
                    " | ".join(
                        [
                            device.deviceId,
                            device.name,
                            device.type.value,
                            f"switch={device.switchId}",
                            f"state={device.switchState}",
                            f"room={device.roomName}",
                        ]
                    )
                )
            return

        device = hub.home.get_device(args.device_id)
        if device is None:
            raise RuntimeError(f"Device {args.device_id!r} was not found in this home.")

        print(f"Target: {device.name} ({device.deviceId}) in {device.roomName}")

        if args.action == "on":
            await device.turn_on()
            print("Command sent: on")
            return

        if args.action == "off":
            await device.turn_off()
            print("Command sent: off")
            return

        await device.turn_on()
        print("Command sent: on")
        await asyncio.sleep(args.toggle_delay)
        await device.turn_off()
        print("Command sent: off")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    require_credentials(args, parser)
    asyncio.run(run_live_test(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
