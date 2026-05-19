from __future__ import annotations

import networkx as nx
import duckdb
from typing import cast
from src.models import Train, Station


class SubwayGraph:
    graph: nx.DiGraph[str]
    _conn: duckdb.DuckDBPyConnection
    _stations: dict[str, Station]
    _trains: dict[str, list[Train]]

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.graph = nx.DiGraph()
        self._conn = conn
        self._stations = {}
        self._trains = {}

    def build(self) -> None:
        self._load_nodes()
        self._load_route_edges()
        self._load_transfer_edges()

    def _load_nodes(self) -> None:
        rows = cast(
            list[tuple[str, str, float, float, list[str]]],
            self._conn.execute("""
                SELECT
                    s.stop_id,
                    s.stop_name,
                    s.stop_lat,
                    s.stop_lon,
                    COALESCE(LIST(DISTINCT t.route_id), []) AS routes
                FROM stops s
                LEFT JOIN stop_times st ON s.stop_id = st.parent_station
                LEFT JOIN trips t ON st.trip_id = t.trip_id
                WHERE s.location_type = 1
                GROUP BY s.stop_id, s.stop_name, s.stop_lat, s.stop_lon
            """).fetchall(),
        )
        for stop_id, name, lat, lon, routes in rows:
            self._stations[stop_id] = Station(
                station_id=stop_id,
                name=name,
                lat=lat,
                lon=lon,
                routes=routes,
            )
            self._trains[stop_id] = []
            self.graph.add_node(stop_id)

    def _load_route_edges(self) -> None:
        rows = cast(
            list[tuple[str, str, list[str], float]],
            self._conn.execute("""
                SELECT
                    st1.parent_station                                   AS from_station,
                    st2.parent_station                                   AS to_station,
                    LIST(DISTINCT t.route_id)                           AS routes,
                    MEDIAN(st2.arrival_seconds - st1.departure_seconds) AS weight
                FROM stop_times st1
                JOIN stop_times st2
                    ON  st1.trip_id       = st2.trip_id
                    AND st2.stop_sequence = st1.stop_sequence + 1
                JOIN trips t ON st1.trip_id = t.trip_id
                WHERE st1.parent_station != st2.parent_station
                GROUP BY st1.parent_station, st2.parent_station
            """).fetchall(),
        )
        for from_station, to_station, routes, weight in rows:
            _ = self.graph.add_edge(
                from_station,
                to_station,
                routes=routes,
                weight=float(weight),
                edge_type="route",
            )

    def _load_transfer_edges(self) -> None:
        rows = cast(
            list[tuple[str, str, int]],
            self._conn.execute("""
                SELECT from_stop, to_stop, transfer_time
                FROM transfers
                WHERE from_stop != to_stop
            """).fetchall(),
        )
        for from_stop, to_stop, transfer_time in rows:
            _ = self.graph.add_edge(
                from_stop, to_stop,
                edge_type="transfer",
                weight=transfer_time,
            )
            _ = self.graph.add_edge(
                to_stop, from_stop,
                edge_type="transfer",
                weight=transfer_time,
            )

    def update_trains(self, trains: list[Train]) -> None:
        for station_id in self._trains:
            self._trains[station_id] = []
        for train in trains:
            station_id = train.location_parent_station
            if station_id and station_id in self._trains:
                self._trains[station_id].append(train)

    def get_station(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    def get_trains_at(self, station_id: str) -> list[Train]:
        return self._trains.get(station_id, [])






