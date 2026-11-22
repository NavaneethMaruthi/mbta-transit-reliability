"""
Turn one day of Red Line snapshots into stop events: one row per train
per stop, with arrival, departure and dwell time.

Rules (from reading one train by eye in one_train.py):
  - Follow trains by vehicle ID, never by label (the label is the lead
    car, and it changes when a train reverses).
  - A "visit" is a run of snapshots where a train is at, or heading to,
    the same stop in the same direction. A new stop, a direction flip,
    or a gap over 60 seconds starts a new visit.
  - Arrival = first STOPPED_AT snapshot. Departure = the snapshot right
    after the last STOPPED_AT. Dwell = departure - arrival. With 15-second
    polling, each end is known to within 15 seconds.
  - A visit with no STOPPED_AT snapshot is "passed": the train stopped
    too briefly to be seen, or skipped the stop.

Turnarounds (version 2): detected from behaviour, not stop IDs, so they
work at permanent terminals (Alewife, Ashmont, Braintree) and temporary
ones during diversions (Broadway and North Quincy on 20-22 Nov 2026).
A visit is a turnaround if:
  - the train's NEXT visit is in the other direction (it reversed here), or
  - its PREVIOUS visit was in the other direction AT THE SAME STATION
    (it switched platforms at an end of the line).
Turnaround visits are layovers, so they are left out of dwell statistics.

Permanent terminals (version 3): behaviour misses trains that enter or
leave service at the end of the line, and Ashmont's platform switch. So
the stations where TYPICAL scheduled trips start and end (from the GTFS
timetable: route_patterns.txt, trips.txt, stop_times.txt) are also left
out of dwell statistics. Only typical patterns are used, because the
timetable also contains the 20-22 Nov diversion trips, which end at
Broadway and North Quincy.

Service days (version 4): a "day" is a Boston service day, 3:00 am to
3:00 am local time, not a UTC calendar day. The raw files are organised by
UTC date, so one service day is read from two folders and trimmed. Late
trains after midnight belong to the service day that started that morning,
the way riders and the MBTA count them. Daylight saving is handled by the
time-zone conversion.

Usage, from the project folder:
    python analysis/stop_events.py 2026-11-18
"""

import glob
import os
import sys
from datetime import date, timedelta

import duckdb

if len(sys.argv) != 2:
    sys.exit("usage: python analysis/stop_events.py YYYY-MM-DD")

DAY = sys.argv[1]
NEXT_DAY = (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()
ROUTE = "Red"

# A service day spans two UTC folders: this one and the next.
FILES = sorted(
    glob.glob(f"data/server/raw/{DAY}/*.jsonl.gz")
    + glob.glob(f"data/server/raw/{NEXT_DAY}/*.jsonl.gz")
)
if not FILES:
    sys.exit(f"no raw files found for {DAY}")
RAW = "[" + ", ".join(f"'{f}'" for f in FILES) + "]"
SERVICE_START = f"(TIMESTAMP '{DAY} 03:00:00' AT TIME ZONE 'America/New_York')"
SERVICE_END = f"(TIMESTAMP '{NEXT_DAY} 03:00:00' AT TIME ZONE 'America/New_York')"
GTFS = "data/gtfs/2026-11-22"
STOPS = f"{GTFS}/stops.txt"
OUT_DIR = "data/derived"
OUT = f"{OUT_DIR}/red_events_{DAY}.parquet"

con = duckdb.connect()
con.sql("SET TimeZone = 'UTC'")

con.sql(f"""
CREATE TABLE stops AS
SELECT stop_id, stop_name, platform_name,
       coalesce(parent_station, stop_id) AS station
FROM read_csv('{STOPS}', all_varchar = true, quote = '"', escape = '"')
""")

con.sql(f"""
CREATE TABLE terminal_stations AS
WITH typical_patterns AS (
    SELECT route_pattern_id
    FROM read_csv('{GTFS}/route_patterns.txt', all_varchar = true, quote = '"', escape = '"')
    WHERE route_id = '{ROUTE}' AND route_pattern_typicality = '1'
),
typical_trips AS (
    SELECT trip_id
    FROM read_csv('{GTFS}/trips.txt', all_varchar = true, quote = '"', escape = '"')
    WHERE route_id = '{ROUTE}'
      AND route_pattern_id IN (SELECT route_pattern_id FROM typical_patterns)
),
trip_stops AS (
    SELECT st.trip_id, st.stop_id, CAST(st.stop_sequence AS INTEGER) AS seq
    FROM read_csv('{GTFS}/stop_times.txt', all_varchar = true, quote = '"', escape = '"') st
    WHERE st.trip_id IN (SELECT trip_id FROM typical_trips)
),
trip_ends AS (
    SELECT arg_min(stop_id, seq) AS first_stop,
           arg_max(stop_id, seq) AS last_stop
    FROM trip_stops
    GROUP BY trip_id
),
end_stops AS (
    SELECT first_stop AS stop_id FROM trip_ends
    UNION
    SELECT last_stop FROM trip_ends
)
SELECT DISTINCT s.station, any_value(s.stop_name) AS stop_name
FROM end_stops e
JOIN stops s USING (stop_id)
GROUP BY s.station
""")

print("Permanent terminals, from the typical timetable:")
con.sql("SELECT stop_name, station FROM terminal_stations ORDER BY stop_name").show()

con.sql(f"""
CREATE TABLE events AS
WITH polls AS (
    SELECT fetched_at, vehicles
    FROM read_json(
        {RAW},
        format = 'newline_delimited',
        columns = {{
            'fetched_at': 'VARCHAR',
            'ok': 'BOOLEAN',
            'vehicles': 'JSON',
            'error': 'VARCHAR'
        }}
    )
    WHERE ok
      AND fetched_at::TIMESTAMPTZ >= {SERVICE_START}
      AND fetched_at::TIMESTAMPTZ <  {SERVICE_END}
),
snapshots AS (
    SELECT
        fetched_at::TIMESTAMPTZ                      AS ts,
        unnest(from_json(vehicles, '["JSON"]'))      AS v
    FROM polls
),
route_rows AS (
    SELECT
        ts,
        v->>'$.id'                                   AS vehicle_id,
        v->>'$.attributes.label'                     AS label,
        CAST(v->>'$.attributes.direction_id' AS INTEGER) AS direction,
        v->>'$.attributes.current_status'            AS status,
        v->>'$.relationships.stop.data.id'           AS stop_id,
        v->>'$.relationships.trip.data.id'           AS trip_id
    FROM snapshots
    WHERE v->>'$.relationships.route.data.id' = '{ROUTE}'
      AND v->>'$.relationships.stop.data.id' IS NOT NULL
),
ordered AS (
    SELECT
        *,
        lag(stop_id)   OVER w AS prev_stop,
        lag(direction) OVER w AS prev_direction,
        lag(ts)        OVER w AS prev_ts,
        lead(ts)       OVER w AS next_ts
    FROM route_rows
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts)
),
numbered AS (
    SELECT
        *,
        sum(CASE
                WHEN prev_stop IS NULL
                  OR stop_id <> prev_stop
                  OR direction <> prev_direction
                  OR ts - prev_ts > INTERVAL 60 SECOND
                THEN 1 ELSE 0
            END) OVER (
                PARTITION BY vehicle_id ORDER BY ts
                ROWS UNBOUNDED PRECEDING
            ) AS visit_no
    FROM ordered
),
visits AS (
    SELECT
        vehicle_id,
        visit_no,
        any_value(stop_id)                                   AS stop_id,
        any_value(direction)                                 AS direction,
        arg_min(label, ts)                                   AS label,
        arg_min(trip_id, ts)                                 AS trip_id,
        count(*) FILTER (WHERE status = 'STOPPED_AT')        AS stopped_polls,
        min(ts) FILTER (WHERE status = 'STOPPED_AT')         AS first_stopped,
        arg_max(next_ts, ts) FILTER (WHERE status = 'STOPPED_AT') AS after_last_stopped,
        arg_max(next_ts, ts)                                 AS after_visit
    FROM numbered
    GROUP BY vehicle_id, visit_no
),
with_station AS (
    SELECT v.*, coalesce(s.station, v.stop_id) AS station
    FROM visits v
    LEFT JOIN stops s USING (stop_id)
),
neighbours AS (
    SELECT
        *,
        lag(direction)  OVER vw AS prev_visit_direction,
        lag(station)    OVER vw AS prev_visit_station,
        lead(direction) OVER vw AS next_visit_direction
    FROM with_station
    WINDOW vw AS (PARTITION BY vehicle_id ORDER BY visit_no)
)
SELECT
    vehicle_id,
    label,
    trip_id,
    direction,
    stop_id,
    station,
    CASE
        WHEN stopped_polls > 0 THEN 'stopped'
        WHEN after_visit IS NOT NULL THEN 'passed'
        ELSE 'incomplete'
    END                                                      AS kind,
    CASE WHEN stopped_polls > 0 THEN first_stopped
         ELSE after_visit END                                AS arrival_ts,
    CASE WHEN stopped_polls > 0 THEN after_last_stopped
         ELSE after_visit END                                AS departure_ts,
    CASE WHEN stopped_polls > 0
         THEN date_diff('second', first_stopped, after_last_stopped)
    END                                                      AS dwell_s,
    coalesce(next_visit_direction <> direction, false)
      OR coalesce(prev_visit_direction <> direction
                  AND prev_visit_station = station, false)  AS is_turnaround,
    station IN (SELECT station FROM terminal_stations)       AS is_terminal_station
FROM neighbours
""")

os.makedirs(OUT_DIR, exist_ok=True)
con.sql(f"COPY events TO '{OUT}' (FORMAT parquet)")

print(f"Visits found, {ROUTE} Line, service day {DAY} (3 am to 3 am Boston):")
con.sql("""
    SELECT kind, is_turnaround, is_terminal_station, count(*) AS visits
    FROM events
    GROUP BY ALL
    ORDER BY kind, is_turnaround, is_terminal_station
""").show()

if DAY == "2026-11-20":
    print("Check: the train read by eye, 13:00 to 13:30 UTC:")
    con.sql(f"""
        WITH chosen AS (
            SELECT vehicle_id
            FROM events
            WHERE label = '1861'
              AND arrival_ts >= '{DAY}T12:55:00'::TIMESTAMPTZ
            ORDER BY arrival_ts
            LIMIT 1
        )
        SELECT
            strftime(e.arrival_ts, '%H:%M:%S')   AS arrived,
            strftime(e.departure_ts, '%H:%M:%S') AS departed,
            e.dwell_s,
            e.kind,
            e.is_turnaround,
            e.label,
            e.direction,
            e.stop_id,
            s.stop_name,
            s.platform_name
        FROM events e
        LEFT JOIN stops s USING (stop_id)
        WHERE e.vehicle_id = (SELECT vehicle_id FROM chosen)
          AND e.arrival_ts >= '{DAY}T12:55:00'::TIMESTAMPTZ
          AND e.arrival_ts <  '{DAY}T13:30:00'::TIMESTAMPTZ
        ORDER BY e.arrival_ts
    """).show(max_width=250)

print("Longest typical dwell, excluding turnarounds and terminals:")
con.sql("""
    SELECT
        s.stop_name,
        s.platform_name                        AS toward,
        count(*)                               AS stops,
        median(e.dwell_s)                      AS median_dwell_s,
        round(quantile_cont(e.dwell_s, 0.9))   AS p90_dwell_s
    FROM events e
    JOIN stops s USING (stop_id)
    WHERE e.kind = 'stopped'
      AND NOT e.is_turnaround
      AND NOT e.is_terminal_station
    GROUP BY ALL
    ORDER BY median_dwell_s DESC, stops DESC
    LIMIT 10
""").show()

print(f"Saved {OUT}")
