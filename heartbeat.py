#!/usr/bin/env python3
"""
Heartbeat for the MBTA collector.

Meant to run every few minutes (from a systemd timer). It checks that the
collector is producing fresh, successful data and that the disk has room,
then reports to healthchecks.io:

  healthy     -> ping HC_PING_URL          (check stays green)
  unhealthy   -> ping HC_PING_URL + /fail  (healthchecks.io alerts right away)
  server down -> no ping at all            (healthchecks.io alerts after grace)

Why check the data instead of the process: the collector can be "running"
while every poll fails (for example, a revoked API key). It still writes
records in that case, marked ok=false. Only the content of the latest record
tells you whether collection is actually working.

Usage:
    python3 heartbeat.py            # check and ping
    python3 heartbeat.py --dry-run  # check and print, no ping

Environment:
    HC_PING_URL     healthchecks.io ping URL (required unless --dry-run)
    MBTA_DATA_DIR   collector output folder (default: data/raw)
"""

import gzip
import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("MBTA_DATA_DIR", "data/raw"))
PING_URL = os.environ.get("HC_PING_URL", "").strip().rstrip("/")
MAX_AGE_SECONDS = 300   # last poll must be within 5 minutes
MIN_FREE_GB = 2.0       # alert well before the disk is actually full


def newest_file():
    files = list(DATA_DIR.glob("*/*.jsonl.gz"))
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def last_record(path):
    """Return the last complete JSON record in a gzip file, or None."""
    last_line = None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_line = line
    except (EOFError, OSError):
        # The collector may be mid-write. Keep the last full line we read.
        pass
    if last_line is None:
        return None
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return None


def assess():
    """Return (problems, details). No problems means healthy."""
    problems = []
    details = {}

    disk_root = DATA_DIR if DATA_DIR.exists() else Path("/")
    free_gb = shutil.disk_usage(disk_root).free / 1e9
    details["free_gb"] = round(free_gb, 1)
    if free_gb < MIN_FREE_GB:
        problems.append(f"low disk: {free_gb:.1f} GB free")

    path = newest_file()
    if path is None:
        problems.append(f"no data files in {DATA_DIR}")
        return problems, details

    record = last_record(path)
    if record is None or "fetched_at" not in record:
        problems.append(f"could not read last record in {path.name}")
        return problems, details

    fetched = datetime.fromisoformat(record["fetched_at"])
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    details["last_poll_age_s"] = int(age)
    if age > MAX_AGE_SECONDS:
        problems.append(f"stale: last poll {age / 60:.1f} min ago")

    if record.get("ok"):
        # Zero vehicles overnight (about 1am-5am) is normal, not a failure.
        details["vehicles"] = len(record.get("vehicles", []))
    else:
        problems.append(f"last poll failed: {record.get('error', 'unknown')}")

    return problems, details


def ping(url, body):
    request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    with urllib.request.urlopen(request, timeout=10):
        pass


def main():
    dry_run = "--dry-run" in sys.argv
    problems, details = assess()

    summary = " ".join(f"{key}={value}" for key, value in details.items())
    if problems:
        message = "UNHEALTHY: " + "; ".join(problems)
        if summary:
            message += f" ({summary})"
    else:
        message = f"OK: {summary}"
    print(message)

    if dry_run:
        return 1 if problems else 0

    if not PING_URL:
        print("HC_PING_URL is not set; cannot report", file=sys.stderr)
        return 2

    url = PING_URL + ("/fail" if problems else "")
    try:
        ping(url, message)
    except Exception as exc:
        print(f"ping failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
