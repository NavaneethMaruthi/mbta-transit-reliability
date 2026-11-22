"""
First SQL look at the collected data, using DuckDB.

DuckDB reads the gzipped JSON Lines files directly: no database server,
no loading step. This script only counts, so its numbers can be checked
against peek.py. If both agree, SQL is reading the data correctly and
the real analysis (arrivals, headways, dwell times) can build on it.

Usage, from the project folder:
    python analysis/check_counts.py
"""

import duckdb

DATA = "data/server/raw/*/*.jsonl.gz"

result = duckdb.sql(f"""
    SELECT
        count(*)                                AS polls,
        count(*) FILTER (WHERE NOT ok)          AS failed_polls,
        sum(json_array_length(vehicles))        AS vehicle_records,
        min(fetched_at)                         AS first_poll,
        max(fetched_at)                         AS last_poll
    FROM read_json(
        '{DATA}',
        format = 'newline_delimited',
        columns = {{
            'fetched_at': 'VARCHAR',
            'ok': 'BOOLEAN',
            'vehicles': 'JSON',
            'error': 'VARCHAR'
        }}
    )
""")

print(result)
