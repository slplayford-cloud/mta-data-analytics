#!/usr/bin/env python3
"""
Collects data from MTA realtime feed and adds observations into the realtime database
Should be run consistently to collect data feeds
"""
from nyct_gtfs import NYCTFeed
from src.db import get_connection, get_scheduled_arrival, write_observations
import logging

TRAIN_LINES=["A"]

def get_feed(line: str) -> NYCTFeed | None:
    try:
        return NYCTFeed(feed_specifier=line)
    except:
        logging.error("get_feed(): could not fetch train feed")
        return None

def _all_lines():
    return ("1", "N", "A", "B", "L", "J", "G", "SI")

def main():
    # FEED MAP
    # 1-7, S 
    # N, Q, R, W 
    # A, C, E, S^r (rockaway shuttle)
    # B, D, F, M, S^f 
    # L 
    # J, Z
    # G
    # SIR


    db_connection = get_connection("mta.duckdb")
    total = 1

    for line in _all_lines():
        feed = get_feed(line)
        if not feed:
           continue 

        train_line = feed.filter_trips(underway=True)
        for trip in train_line:
            # TODO: Analyze each trip
            arrival_time = get_scheduled_arrival(
                   conn=db_connection,
                   rt_trip_id=trip.trip_id,
                   stop_id=trip.location,
                   service_date=trip.start_date
                )

            if arrival_time is None:
                print(f'Error #: {total}')
                print(f'Trip id: {trip.trip_id}') 
                print(f'Stop id: {trip.location}')
                print(f'Date: {trip.start_date}')
                #print(f'Route: {trip.route_id} Status: {trip.location_status} {trip.location} Arrival: {arrival_time}')
                print()
                total += 1 
            # print(f'trip: {trip.trip_id} ETA: {arrival_time}')


    print(total)
    


if __name__ == "__main__":
    main()
