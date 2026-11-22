"""
Headways: the time between one train leaving a platform and the next.

Reads the stop events saved by stop_events.py for one day, so run that
first for the same day. Turnarounds and terminal stations are left out:
the question here is how evenly trains reach riders along the line.

Unevenness is measured against each platform's own typical (median) gap,
so no schedule is assumed:
  - bunched:  a gap shorter than half the median
  - big gap:  a gap longer than 1.5 times the median

Times are Boston local time. A day is the service day saved by
stop_events.py (3 am to 3 am), so after-midnight hours are listed last.

Usage, from the project folder:
    python analysis/headways.py 2026-11-18
"""

import sys

import duckdb

if len(sys.argv) != 2:
    sys.exit("usage: python analysis/headways.py YYYY-MM-DD")

DAY = sys.argv[1]
EVENTS = f"data/derived/red_events_{DAY}.parquet"
STOPS = "data/gtfs/2026-11-22/stops.txt"
PEAK_START, PEAK_END = 7, 9      # morning rush, local hours [7, 9)

con = duckdb.connect()
con.sql("SET TimeZone = 'America/New_York'")

con.sql(f"""
CREATE TABLE headways AS
WITH ev AS (
    SELECT stop_id, departure_ts
    FROM read_parquet('{EVENTS}')
    WHERE kind IN ('stopped', 'passed')
      AND NOT is_turnaround
      AND NOT is_terminal_station
      AND departure_ts IS NOT NULL
),
gaps AS (
    SELECT
        stop_id,
        departure_ts,
        date_diff(
            'second',
            lag(departure_ts) OVER (PARTITION BY stop_id ORDER BY departure_ts),
            departure_ts
        ) AS headway_s
    FROM ev
)
SELECT
    g.*,
    hour(g.departure_ts)    AS local_hour,
    s.stop_name,
    s.platform_name         AS toward
FROM gaps g
JOIN read_csv('{STOPS}', all_varchar = true, quote = '"', escape = '"') s
  USING (stop_id)
WHERE g.headway_s IS NOT NULL
  AND g.headway_s < 3600          -- drop the overnight gap
""")

print(f"Morning rush ({PEAK_START}-{PEAK_END} am), {DAY}: most uneven platforms")
con.sql(f"""
WITH peak AS (
    SELECT * FROM headways
    WHERE local_hour >= {PEAK_START} AND local_hour < {PEAK_END}
),
typical AS (
    SELECT stop_id, median(headway_s) AS med
    FROM peak
    GROUP BY stop_id
)
SELECT
    p.stop_name,
    p.toward,
    count(*)                                              AS trains,
    round(t.med / 60, 1)                                  AS median_min,
    round(quantile_cont(p.headway_s, 0.9) / 60, 1)        AS p90_min,
    round(max(p.headway_s) / 60, 1)                       AS longest_min,
    round(100 * avg((p.headway_s < 0.5 * t.med)::INT))    AS pct_bunched,
    round(100 * avg((p.headway_s > 1.5 * t.med)::INT))    AS pct_big_gap
FROM peak p
JOIN typical t USING (stop_id)
GROUP BY p.stop_name, p.toward, t.med
HAVING count(*) >= 10
ORDER BY pct_bunched + pct_big_gap DESC, p90_min DESC
LIMIT 12
""").show()

print(f"Park Street toward Ashmont/Braintree, hour by hour, {DAY}:")
con.sql("""
SELECT
    local_hour                                   AS hour,
    count(*)                                     AS trains,
    round(median(headway_s) / 60, 1)             AS median_min,
    round(quantile_cont(headway_s, 0.9) / 60, 1) AS p90_min,
    round(max(headway_s) / 60, 1)                AS longest_min
FROM headways
WHERE stop_name = 'Park Street' AND toward = 'Ashmont/Braintree'
GROUP BY local_hour
ORDER BY local_hour < 3, local_hour
""").show(max_rows=30)
