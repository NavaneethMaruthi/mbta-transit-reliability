#!/usr/bin/env python3
"""
MBTA vehicle position collector.

Polls the MBTA V3 /vehicles endpoint on a fixed interval and appends every
response, unmodified, to an hourly gzipped JSON-lines file.

Design rules, in priority order:
  1. Never crash. A dead collector loses data permanently.
  2. Never modify the payload. Raw means raw. Parsing happens later, offline,
     where you can fix your mistakes and re-run.
  3. Never block on anything slow. One poll must not delay the next.

Usage:
    export MBTA_API_KEY=your_key_here
    python3 collect.py

Environment:
    MBTA_API_KEY       API key from https://api-v3.mbta.com (free, instant)
    MBTA_DATA_DIR      where to write (default: ./data/raw)
    MBTA_POLL_SECONDS  seconds between polls (default: 15)
    MBTA_ROUTE_TYPES   GTFS route types (default: 0,1 = light rail + subway)
"""

import gzip
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api-v3.mbta.com/vehicles"
API_KEY = os.environ.get("MBTA_API_KEY", "").strip()
DATA_DIR = Path(os.environ.get("MBTA_DATA_DIR", "data/raw"))
POLL_SECONDS = float(os.environ.get("MBTA_POLL_SECONDS", "15"))
ROUTE_TYPES = os.environ.get("MBTA_ROUTE_TYPES", "0,1")
HTTP_TIMEOUT = 10

_running = True


def _stop(signum, frame):
    global _running
    _running = False
    log(f"signal {signum} received, finishing current poll then exiting")


def log(message):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


def fetch():
    """One request. Returns (payload, error_string). Never raises."""
    url = f"{API_URL}?filter%5Broute_type%5D={ROUTE_TYPES}"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.api+json")
    if API_KEY:
        request.add_header("x-api-key", API_KEY)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        return None, f"url_error:{exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"network:{exc}"
    except json.JSONDecodeError:
        return None, "bad_json"
    except Exception as exc:  # last resort — the loop must survive
        return None, f"unexpected:{type(exc).__name__}:{exc}"


def write_record(record, moment):
    """Append one JSON line to this hour's gzip file. Never raises."""
    folder = DATA_DIR / moment.strftime("%Y-%m-%d")
    path = folder / f"{moment.strftime('%H')}.jsonl.gz"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write(line)
        return True
    except Exception as exc:
        log(f"WRITE FAILED {path}: {type(exc).__name__}: {exc}")
        return False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if not API_KEY:
        log("WARNING: no MBTA_API_KEY set. Anonymous access is capped at about "
            "20 requests/minute and you will start getting 429s. Get a free key "
            "at https://api-v3.mbta.com and restart.")

    log(f"starting: every {POLL_SECONDS}s, route_type={ROUTE_TYPES}, "
        f"writing to {DATA_DIR.resolve()}")

    polls = 0
    failures = 0
    consecutive_failures = 0
    last_heartbeat = time.monotonic()

    while _running:
        cycle_start = time.monotonic()
        moment = datetime.now(timezone.utc)

        payload, error = fetch()
        polls += 1

        record = {
            "fetched_at": moment.isoformat(timespec="milliseconds"),
            "ok": error is None,
        }
        if error is None:
            record["vehicles"] = payload.get("data", [])
            consecutive_failures = 0
        else:
            record["error"] = error
            failures += 1
            consecutive_failures += 1
            if consecutive_failures in (1, 5, 20) or consecutive_failures % 100 == 0:
                log(f"fetch failed ({consecutive_failures} in a row): {error}")

        write_record(record, moment)

        # Heartbeat every 5 minutes so you can tail the log and see life.
        if time.monotonic() - last_heartbeat >= 300:
            count = len(record.get("vehicles", []))
            log(f"alive: {polls} polls, {failures} failures, "
                f"{count} vehicles in last good poll")
            last_heartbeat = time.monotonic()

        # Back off when the API is clearly unhappy, but never give up.
        delay = POLL_SECONDS
        if consecutive_failures > 3:
            delay = min(POLL_SECONDS * 4, 120)

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0.0, delay - elapsed)
        # Sleep in slices so SIGTERM is handled promptly.
        while sleep_for > 0 and _running:
            nap = min(1.0, sleep_for)
            time.sleep(nap)
            sleep_for -= nap

    log(f"stopped cleanly after {polls} polls ({failures} failures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
