from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Route:
    route_id: str          # "A"
    short_name: str
    long_name: str
    color: str             # "0062CF"
    text_color: str

@dataclass
class Station:
    station_id: str        # parent stop_id, e.g. "101"
    name: str
    lat: float
    lon: float
    routes: list[Route] = field(default_factory=list)

@dataclass
class Trip:
    trip_id: str           # full static trip_id
    route_id: str
    service_id: str        # "Sunday" / "Saturday" / "Weekday"
    headsign: str
    direction_id: int      # 0 or 1
    shape_id: str

@dataclass
class StopTime:
    trip_id: str
    stop_id: str           # platform id e.g. "101S"
    parent_station: str    # stop_id[:-1]
    arrival_seconds: int   # seconds past midnight (handles >86400 for overnight)
    departure_seconds: int
    stop_sequence: int

@dataclass
class Train:
    """Live train observation from GTFS-RT."""
    trip_id: str                            # RT trip_id e.g. "000600_1..S03R"
    route_id: str
    direction: str                          # "N" or "S"
    headsign: str | None
    location_stop_id: str | None            # platform id
    location_parent_station: str | None     # stop_id[:-1]
    location_status: str | None             # STOPPED_AT / INCOMING_AT / IN_TRANSIT_TO
    last_position_update: datetime | None
    has_delay_alert: bool
    observed_at: datetime
    delay_seconds: int | None               # positive = late; None if no schedule match
