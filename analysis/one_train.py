"""
Follow one Red Line train through 30 minutes of Friday morning rush hour.

Picks the first Red Line vehicle seen at 13:00 UTC on 2026-11-20 (8:00 am
in Boston) and prints every snapshot of it until 13:30 UTC: time, status
and stop. Reading this by eye shows what an arrival, a dwell and a
departure look like in raw snapshots, before any code tries to detect
them automatically.

Usage, from the project folder:
    python analysis/one_train.py
"""

import duckdb

DAY_FILES = "data/server/raw/2026-11-20/*.jsonl.gz"
START = "2026-11-20T13:00"
END = "2026-11-20T13:30"

query = f"""
WITH polls AS (
    SELECT fetched_at, vehicles
    FROM read_json(
        '{DAY_FILES}',
        format = 'newline_delimited',
        columns = {{
            'fetched_at': 'VARCHAR',
            'ok': 'BOOLEAN',
            'vehicles': 'JSON',
            'error': 'VARCHAR'
        }}
    )
    WHERE ok
),
snapshots AS (
    -- One row per vehicle per poll, instead of one row per poll.
    SELECT fetched_at, unnest(from_json(vehicles, '["JSON"]')) AS v
    FROM polls
),
red AS (
    SELECT
        fetched_at,
        v->>'$.id'                              AS vehicle_id,
        v->>'$.attributes.label'                AS label,
        v->>'$.attributes.current_status'       AS status,
        v->>'$.relationships.stop.data.id'      AS stop_id,
        v->>'$.attributes.direction_id'         AS direction
    FROM snapshots
    WHERE v->>'$.relationships.route.data.id' = 'Red'
),
chosen AS (
    SELECT vehicle_id
    FROM red
    WHERE fetched_at >= '{START}'
    ORDER BY fetched_at
    LIMIT 1
)
SELECT
    substr(fetched_at, 12, 8)   AS time_utc,
    label,
    direction,
    status,
    stop_id
FROM red
WHERE vehicle_id = (SELECT vehicle_id FROM chosen)
  AND fetched_at >= '{START}'
  AND fetched_at <  '{END}'
ORDER BY fetched_at
"""

duckdb.sql(query).show(max_rows=150)
