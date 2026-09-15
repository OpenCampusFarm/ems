"""Export 30 days of CoolBot room temperature to CSV (or --format json).

Timestamps use America/Detroit time with a daylight-saving-aware UTC offset.
uv run Loads/coolbot_export_hisotry_30d.py
"""

import argparse
import asyncio
import csv
import json
import math
import struct
import sys
import tempfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets

# Support both direct execution and `python -m Loads.coolbot_export_hisotry_30d`.
if __package__:
    from . import coolbot
else:
    import coolbot

# Websocket packet number
CMD_GRAPH = 60
MICHIGAN_TZ = ZoneInfo("America/Detroit")


def decode_graph(body: bytes) -> list[list[tuple[int, float]]]:
    """Mirror message.js:2087: compressed series of (value, timestamp_ms)."""
    data = zlib.decompress(body, wbits=32 + zlib.MAX_WBITS)
    if len(data) < 4:
        raise ValueError("Graph payload is missing its four-byte prefix")
    offset = 4  # The app skips this field; don't assume its meaning.
    series = []
    while offset < len(data):
        if len(data) - offset < 4:
            raise ValueError("Truncated graph series header")
        count = struct.unpack_from(">i", data, offset)[0]
        offset += 4
        if count < 0 or count > (len(data) - offset) // 16:
            raise ValueError("Invalid graph sample count or truncated samples")
        points = []
        for _ in range(count):
            value = struct.unpack_from(">d", data, offset)[0]
            # JS reads the lower six bytes of the eight-byte timestamp field.
            timestamp_ms = int.from_bytes(data[offset + 10 : offset + 16], "big")
            points.append((timestamp_ms, value))
            offset += 16
        series.append(points)
    return series


async def request(ws, packet: bytes, expected_command: int, timeout: float) -> bytes:
    """Match replies by ID, acknowledge server pings, and bound the whole wait."""
    msg_id = struct.unpack_from(">H", packet, 1)[0]
    async with asyncio.timeout(timeout):
        await ws.send(packet)
        while True:
            raw = await ws.recv()
            if not isinstance(raw, bytes) or len(raw) < 5:
                raise ValueError(
                    "Expected a binary Blynk packet with a five-byte header"
                )
            cmd, reply_id, field = struct.unpack_from(">BHH", raw)
            if cmd != coolbot.CMD_RESPONSE and len(raw) != 5 + field:
                raise ValueError("Blynk packet length does not match its header")
            if cmd == coolbot.CMD_PING:
                await ws.send(coolbot.build_response_packet(reply_id))
                continue
            if reply_id != msg_id:
                continue
            if cmd == coolbot.CMD_RESPONSE:
                if field != 200:
                    raise RuntimeError(
                        f"Request {msg_id} failed: server status {field}"
                    )
                if expected_command == coolbot.CMD_RESPONSE:
                    return raw
                continue  # An ACK alone isn't a graph/profile response.
            if cmd == expected_command:
                return raw


def select_device(profile, dashboard_id=None, device_id=None):
    dashboards = profile.get("dashBoards", [])
    if dashboard_id is not None:
        dashboards = [d for d in dashboards if d["id"] == dashboard_id]
    if len(dashboards) != 1:
        raise ValueError(
            "Select a dashboard with --dashboard-id; matching IDs: "
            + str([d["id"] for d in dashboards])
        )
    dashboard = dashboards[0]
    devices = dashboard.get("devices", [])
    if device_id is not None:
        devices = [d for d in devices if d["id"] == device_id]
    if len(devices) != 1:
        raise ValueError(
            "Select a device with --device-id; matching IDs: "
            + str([d["id"] for d in devices])
        )
    return dashboard["id"], devices[0]["id"]


def make_rows(points, start_ms, end_ms):
    # Pages may overlap. Keep the first value, from the newest page, per time.
    unique = {}
    for timestamp_ms, value in points:
        if start_ms <= timestamp_ms <= end_ms:
            unique.setdefault(timestamp_ms, value if math.isfinite(value) else None)
    return [
        {
            "timestamp_local": datetime.fromtimestamp(
                timestamp_ms / 1000, MICHIGAN_TZ
            ).isoformat(timespec="milliseconds"),
            "room_temp_f": value,
        }
        for timestamp_ms, value in sorted(unique.items())
    ]


async def fetch_history(days=30, dashboard_id=None, device_id=None, timeout=30):
    if not coolbot.EMAIL or not coolbot.PASSWORD:
        raise ValueError(
            "Set SIT_EMAIL and SIT_PASSWORD in core/.env or the environment"
        )
    end = datetime.now(UTC)
    start_ms = int((end - timedelta(days=days)).timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    points = []
    async with websockets.connect(
        coolbot.BLYNK_URL, open_timeout=timeout, close_timeout=5
    ) as ws:
        await request(
            ws, coolbot.build_login_packet(coolbot.EMAIL), coolbot.CMD_RESPONSE, timeout
        )
        raw = await request(
            ws,
            coolbot.build_text_packet(coolbot.CMD_LOAD_PROFILE_GZIPPED, "", 2),
            coolbot.CMD_LOAD_PROFILE_GZIPPED,
            timeout,
        )
        parsed = coolbot.parse_packet(raw)
        if "profile" not in parsed:
            raise ValueError(
                f"Could not decode profile: {parsed.get('decompress_error')}"
            )
        dashboard_id, device_id = select_device(
            parsed["profile"], dashboard_id, device_id
        )
        print(
            f"Reading dashboard {dashboard_id}, device {device_id} (v0)",
            file=sys.stderr,
        )
        # DAY returns up to 1,440 stored minute samples, possibly spanning more
        # than a calendar day when there are gaps. Stop on the timestamp cutoff.
        # One extra page covers boundaries if the server aligns pages to buckets.
        # Don't stop at an empty page: there can be gaps between older readings.
        for page in range(days + 1):
            fields = [f"{dashboard_id}-{device_id}", "v0", "DAY"]
            if page:
                fields.append(str(page))
            raw = await request(
                ws,
                coolbot.build_text_packet(CMD_GRAPH, "\0".join(fields), 10 + page),
                CMD_GRAPH,
                timeout,
            )
            series = decode_graph(raw[5:])
            if len(series) > 1:
                raise ValueError("Expected one room-temperature series for v0")
            page_points = series[0] if series else []
            points.extend(page_points)
            print(
                f"Page {page + 1}/{days + 1}: {len(page_points)} samples",
                file=sys.stderr,
            )
            if page_points and min(t for t, _ in page_points) <= start_ms:
                break
    return make_rows(points, start_ms, end_ms)


def write_export(rows, output: Path, file_format: str):
    """Replace the output only after the entire export has been written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            if file_format == "csv":
                writer = csv.DictWriter(
                    stream, fieldnames=["timestamp_local", "room_temp_f"]
                )
                writer.writeheader()
                writer.writerows(rows)
            else:
                json.dump(rows, stream, indent=2, allow_nan=False)
                stream.write("\n")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def positive_days(value):
    days = int(value)
    if not 1 <= days <= 30:
        raise argparse.ArgumentTypeError("days must be between 1 and 30")
    return days


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=positive_days, default=30, help="1–30; default: 30"
    )
    parser.add_argument("--format", choices=["csv", "json"], dest="file_format")
    parser.add_argument(
        "-o", "--output", type=Path, help="Output path; .json selects JSON"
    )
    parser.add_argument("--dashboard-id", type=int)
    parser.add_argument("--device-id", type=int)
    args = parser.parse_args()
    suffix = args.output.suffix.lower() if args.output else ""
    file_format = args.file_format or ("json" if suffix == ".json" else "csv")
    if suffix in (".csv", ".json") and suffix != f".{file_format}":
        parser.error("--format conflicts with the output filename extension")
    output = args.output or Path(f"room_temperature_{args.days}d.{file_format}")
    try:
        rows = asyncio.run(fetch_history(args.days, args.dashboard_id, args.device_id))
        if not rows:
            raise RuntimeError(
                "No room-temperature history returned for the requested period"
            )
        write_export(rows, output, file_format)
    except TimeoutError:
        print(
            "Export failed: timed out waiting for the CoolBot server", file=sys.stderr
        )
        return 1
    except (
        OSError,
        ValueError,
        RuntimeError,
        zlib.error,
        websockets.exceptions.WebSocketException,
    ) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    valid = sum(row["room_temp_f"] is not None for row in rows)
    print(f"Saved {len(rows)} samples ({valid} valid temperatures) to {output}")
    print(
        f"America/Detroit range: {rows[0]['timestamp_local']} "
        f"to {rows[-1]['timestamp_local']}"
    )
    print("Missing readings are not filled in; server history may contain gaps.")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
