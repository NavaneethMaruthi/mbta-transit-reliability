#!/usr/bin/env python3
"""
Sanity-check what the collector has captured so far.

Run this an hour after starting, and again tomorrow morning. It answers the
only three questions that matter in week one:
  - Is data arriving?
  - Are there gaps?
  - Does the payload contain what I think it contains?

Usage:
    python3 peek.py              # summarise everything collected
    python3 peek.py 2026-11-01   # summarise one day
"""

import gzip
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data/raw")


def read_lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def main():
    if not DATA_DIR.exists():
        print(f"No data directory at {DATA_DIR.resolve()} — is the collector running?")
        return 1

    day_filter = sys.argv[1] if len(sys.argv) > 1 else None
    files = sorted(DATA_DIR.glob("*/*.jsonl.gz"))
    if day_filter:
        files = [f for f in files if f.parent.name == day_filter]
    if not files:
        print("No files yet. Give it a minute, then try again.")
        return 1

    polls = 0
    good = 0
    errors = Counter()
    statuses = Counter()
    routes = Counter()
    vehicle_counts = []
    timestamps = []
    total_bytes = 0

    for path in files:
        total_bytes += path.stat().st_size
        for record in read_lines(path):
            polls += 1
            timestamps.append(record["fetched_at"])
            if not record.get("ok"):
                errors[record.get("error", "unknown")] += 1
                continue
            good += 1
            vehicles = record.get("vehicles", [])
            vehicle_counts.append(len(vehicles))
            for vehicle in vehicles:
                attrs = vehicle.get("attributes", {})
                statuses[attrs.get("current_status")] += 1
                route = (vehicle.get("relationships", {})
                         .get("route", {}).get("data") or {}).get("id")
                routes[route] += 1

    print(f"files:        {len(files)}")
    print(f"disk:         {total_bytes / 1_048_576:.1f} MB")
    print(f"polls:        {polls}  ({good} ok, {polls - good} failed)")
    if timestamps:
        print(f"first poll:   {min(timestamps)}")
        print(f"last poll:    {max(timestamps)}")
    if vehicle_counts:
        print(f"vehicles:     min {min(vehicle_counts)}, "
              f"max {max(vehicle_counts)}, "
              f"avg {sum(vehicle_counts) / len(vehicle_counts):.0f}")
    if errors:
        print("\nerrors:")
        for error, count in errors.most_common():
            print(f"  {count:>6}  {error}")

    print("\ncurrent_status seen:")
    for status, count in statuses.most_common():
        print(f"  {count:>8}  {status}")

    print("\ntop routes:")
    for route, count in routes.most_common(12):
        print(f"  {count:>8}  {route}")

    # Gap detection — the thing you actually care about.
    if len(timestamps) > 2:
        parsed = sorted(datetime.fromisoformat(t) for t in timestamps)
        gaps = []
        for earlier, later in zip(parsed, parsed[1:]):
            seconds = (later - earlier).total_seconds()
            if seconds > 90:
                gaps.append((earlier, seconds))
        if gaps:
            print(f"\ngaps over 90s: {len(gaps)}")
            for when, seconds in gaps[-10:]:
                print(f"  {when.isoformat(timespec='seconds')}  "
                      f"{seconds / 60:.1f} min")
        else:
            print("\ngaps over 90s: none — collector has been continuous")

    return 0


if __name__ == "__main__":
    sys.exit(main())
