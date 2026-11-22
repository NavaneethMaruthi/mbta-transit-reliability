"""
Turn stop IDs into station names, using the MBTA's GTFS schedule feed.

Prints two things:
  1. Which dates this downloaded schedule version covers (feed_info.txt).
  2. The names of the stops seen in analysis/one_train.py (stops.txt).

Usage, from the project folder:
    python analysis/stop_names.py
"""

import duckdb

GTFS = "data/gtfs/2026-11-22"

print("Schedule version:")
duckdb.sql(f"""
    SELECT feed_version, feed_start_date, feed_end_date
    FROM read_csv('{GTFS}/feed_info.txt', all_varchar = true)
""").show()

print("Stops from the one-train journey:")
duckdb.sql(f"""
    SELECT stop_id, stop_name, platform_name, parent_station
    FROM read_csv('{GTFS}/stops.txt', all_varchar = true)
    WHERE stop_id IN ('70098', '70099', '70100', '70101',
                      '70103', '70105', 'Braintree-02')
    ORDER BY stop_id
""").show()
