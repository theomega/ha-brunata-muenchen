"""Fetch Brunata data and import it into a Home Assistant instance under test ids.

Runs the same fetch and spreading code as the integration, but writes the result
through the WebSocket API to `brunata_test:<cost_type>` statistics, so the result
can be checked in Home Assistant before installing the integration.

Credentials are only read from the environment, never from this repository:

    BRUNATA_USERNAME, BRUNATA_PASSWORD   portal login (or pass --env-file)
    HA_URL, HA_TOKEN                     Home Assistant URL and long-lived token

Usage:
    python3 tools/import_test.py --env-file ../.env.brunata [--dry-run]
    python3 tools/import_test.py --delete   # remove the test statistics again

Requires: httpx, pydantic, websockets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Import the HA-free modules of the integration without running its __init__.
PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "brunata"
_pkg = types.ModuleType("brunata")
_pkg.__path__ = [str(PKG_DIR)]
sys.modules["brunata"] = _pkg

from brunata.brunata_api import BrunataClient  # noqa: E402
from brunata.const import BASE_URL, SAP_CLIENT, STATISTIC_NAMES, UNIT_CLASSES  # noqa: E402
from brunata.fetch import fetch_meters  # noqa: E402
from brunata.series import hourly_sums  # noqa: E402

TEST_SOURCE = "brunata_test"


def load_env_file(path: str) -> None:
    """Add KEY=VALUE lines of a file to os.environ, without overriding."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(name: str) -> str:
    if not (value := os.environ.get(name)):
        sys.exit(f"Missing environment variable {name}")
    return value


class HomeAssistantWS:
    """Minimal Home Assistant WebSocket client."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
        self._token = token
        self._id = 0

    async def __aenter__(self) -> HomeAssistantWS:
        import websockets

        self._ws = await websockets.connect(self._url, max_size=None)
        await self._ws.recv()
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        if json.loads(await self._ws.recv())["type"] != "auth_ok":
            sys.exit("Home Assistant rejected the token")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._ws.close()

    async def call(self, **msg: object) -> object:
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, **msg}))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("id") == self._id:
                if not reply["success"]:
                    raise RuntimeError(reply["error"])
                return reply.get("result")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", help="file with BRUNATA_*/HA_* variables")
    parser.add_argument("--timezone", default="Europe/Berlin")
    parser.add_argument("--dry-run", action="store_true", help="only print the monthly values")
    parser.add_argument("--delete", action="store_true", help="remove the test statistics")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)

    if args.delete:
        async with HomeAssistantWS(require_env("HA_URL"), require_env("HA_TOKEN")) as ha:
            ids = [
                s["statistic_id"]
                for s in await ha.call(type="recorder/list_statistic_ids")
                if s["statistic_id"].startswith(f"{TEST_SOURCE}:")
            ]
            if ids:
                await ha.call(type="recorder/clear_statistics", statistic_ids=ids)
            print("Deleted:", ids or "nothing")
        return

    async with BrunataClient(
        base_url=BASE_URL,
        username=require_env("BRUNATA_USERNAME"),
        password=require_env("BRUNATA_PASSWORD"),
        sap_client=SAP_CLIENT,
    ) as client:
        await client.login()
        meters = await fetch_meters(client)

    for meter in meters.values():
        months = ", ".join(f"{m.year}-{m.month:02d}: {m.value}" for m in meter.months)
        print(f"{meter.cost_type} ({meter.kind}, {meter.unit}): {months}")
    if args.dry_run:
        return

    tz = ZoneInfo(args.timezone)
    async with HomeAssistantWS(require_env("HA_URL"), require_env("HA_TOKEN")) as ha:
        for meter in meters.values():
            rows = hourly_sums(meter.months, tz, datetime.now(UTC))
            statistic_id = f"{TEST_SOURCE}:{meter.cost_type.lower()}"
            await ha.call(
                type="recorder/import_statistics",
                metadata={
                    "statistic_id": statistic_id,
                    "source": TEST_SOURCE,
                    "name": f"TEST {STATISTIC_NAMES[meter.kind]} {meter.cost_type}",
                    "unit_of_measurement": meter.unit,
                    "unit_class": UNIT_CLASSES[meter.unit],
                    "has_sum": True,
                    "mean_type": 0,
                },
                stats=[
                    {"start": r.start.isoformat(), "state": r.sum, "sum": r.sum} for r in rows
                ],
            )
            print(f"Imported {len(rows)} hours into {statistic_id}")


if __name__ == "__main__":
    asyncio.run(main())
