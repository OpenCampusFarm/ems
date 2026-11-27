"""Share the fan's last GPIO setting with the Portal."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

STATUS_PATH = Path("/run/campus-ems/fan-status.json")


def record_fan_status(mode, on, path=STATUS_PATH):
    with TemporaryDirectory(prefix=".fan-status-", dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_text(json.dumps({"pid": os.getpid(), "mode": mode, "on": on}))
        # The root-run fan publishes to the group-traversable runtime directory.
        temporary.chmod(0o644)
        temporary.replace(path)


def read_auto_fan_status(pid, path=STATUS_PATH):
    try:
        reading = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (isinstance(reading, dict) and reading.get("pid") == pid
            and reading.get("mode") == "auto" and isinstance(reading.get("on"), bool)):
        return reading["on"]
    return None
