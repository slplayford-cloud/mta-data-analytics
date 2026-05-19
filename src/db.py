#!/usr/bin/env python3

import duckdb
from src.models import Train

def get_connection(db_name: str):
    return duckdb.connect(db_name)

def create_tables(conn: duckdb.DuckDBPyConnection):
    """
    This function creates the tables for our database
    General flow is as follows:
        this is the most common data pipeline used throughout the project
        routes -> trips -> stop_times

        stops -> transfers (almost a disconnected section **not fully**)
    """

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS calendar(
        service_id          VARCHAR PRIMARY KEY,
        monday              BOOLEAN,
        tuesday             BOOLEAN,
        wednesday           BOOLEAN,
        thursday            BOOLEAN,
        friday              BOOLEAN,
        saturday            BOOLEAN,
        sunday              BOOLEAN,
        start_date          DATE,
        end_date            DATE
    );
    """)

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS routes(
        route_id            VARCHAR PRIMARY KEY,
        short_name          VARCHAR,
        long_name           VARCHAR,
        route_color         VARCHAR,
        route_text_color    VARCHAR
    );
    """)

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS stops(
        stop_id             VARCHAR PRIMARY KEY,
        stop_name           VARCHAR NOT NULL,
        stop_lat            DOUBLE,
        stop_lon            DOUBLE,
        location_type       TINYINT,
        parent_station      VARCHAR
    );
    """)

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS trips(
        trip_id             VARCHAR PRIMARY KEY,
        route_id            VARCHAR NOT NULL,
        service_id          VARCHAR NOT NULL,
        headsign            VARCHAR NOT NULL,
        direction_id        TINYINT,
        shape_id            VARCHAR,
        rt_trip_key         VARCHAR
    );
    """)
    _ = conn.execute("CREATE INDEX IF NOT EXISTS idx_rt_trip_key ON trips(rt_trip_key);")

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS transfers(
        from_stop           VARCHAR NOT NULL,
        to_stop             VARCHAR NOT NULL,
        transfer_type       TINYINT,
        transfer_time       INTEGER,
        PRIMARY KEY (from_stop, to_stop)
    );
    """)

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS stop_times(
        trip_id             VARCHAR NOT NULL,
        stop_id             VARCHAR NOT NULL,
        parent_station      VARCHAR NOT NULL,
        arrival_seconds     INTEGER NOT NULL,
        departure_seconds   INTEGER NOT NULL,
        stop_sequence       SMALLINT NOT NULL,
        PRIMARY KEY (trip_id, stop_sequence)
    );
    """)
    _ = conn.execute("CREATE INDEX IF NOT EXISTS idx_st_stop   ON stop_times(stop_id);")
    _ = conn.execute("CREATE INDEX IF NOT EXISTS idx_st_parent ON stop_times(parent_station);")

    _ = conn.execute(
    """
    CREATE TABLE IF NOT EXISTS _meta(
        key     VARCHAR PRIMARY KEY,
        value   VARCHAR
    );
    """)
    

def initialize(conn: duckdb.DuckDBPyConnection):

    result = conn.execute("SELECT value FROM _meta WHERE key = 'static_loaded'").fetchone()
    if result:
        return

    _ = conn.execute(
    """
    INSERT INTO calendar SELECT * FROM read_csv_auto('static/calendar.txt', dateformat='%Y%m%d')
    """)
    _ = conn.execute(
    """
    INSERT INTO routes
    SELECT route_id, route_short_name AS short_name, route_long_name AS long_name, route_color, route_text_color
    FROM read_csv_auto('static/routes.txt')
    """)
    _ = conn.execute(
    """
    INSERT INTO stops SELECT * FROM read_csv_auto('static/stops.txt')
    """)

    # MUST USE A REGEX TO EXTRACT THE TRIP KEY
    _ = conn.execute(
    r"""
    INSERT INTO trips
    SELECT
        trip_id,
        route_id,
        service_id,
        trip_headsign AS headsign,
        direction_id,
        shape_id,
        regexp_extract(trip_id, '_(\d+_.+)$', 1) AS rt_trip_key
    FROM read_csv_auto('static/trips.txt')
    """)
    _ = conn.execute(
    """
    INSERT INTO transfers SELECT * FROM read_csv_auto('static/transfers.txt')
    """)
    _ = conn.execute(
    """
    INSERT INTO stop_times 
    SELECT
        trip_id,
        stop_id,
        stop_id[:-1] AS parent_station,
        CAST(split_part(arrival_time, ':', 1) AS INTEGER) * 3600 +
        CAST(split_part(arrival_time, ':', 2) AS INTEGER) * 60 +
        CAST(split_part(arrival_time, ':', 3) AS INTEGER) AS arrival_seconds,
        CAST(split_part(departure_time, ':', 1) AS INTEGER) * 3600 +
        CAST(split_part(departure_time, ':', 2) AS INTEGER) * 60 +
        CAST(split_part(departure_time, ':', 3) AS INTEGER) AS departure_seconds,
        stop_sequence
    FROM read_csv_auto('static/stop_times.txt')
    """)

    _ = conn.execute("INSERT INTO _meta VALUES ('static_loaded', 'true')")

def get_scheduled_arrival(conn, trains: list[Train]):
    pass


# MAIN EXECUTION

def main():
    conn = get_connection("mta.duckdb")
    create_tables(conn)
    initialize(conn)

    counts = {
        "calendar":   conn.execute("SELECT COUNT(*) FROM calendar").fetchone()[0],
        "routes":     conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
        "stops":      conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0],
        "trips":      conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0],
        "transfers":  conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0],
        "stop_times": conn.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0],
    }
    for table, count in counts.items():
        print(f"{table:<12} {count:>7} rows")

    max_arrival = conn.execute("SELECT MAX(arrival_seconds) FROM stop_times").fetchone()[0]
    print(f"\nmax arrival_seconds: {max_arrival}  (expect > 86400)")

if __name__ == '__main__':
    main()

