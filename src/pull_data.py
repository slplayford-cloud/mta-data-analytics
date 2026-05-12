#!/usr/bin/env python3
'''
This module will collect realtime mta data from MTA servers
'''

import os
import requests
from google.transit import gtfs_realtime_pb2


def pull_line_data(line: str):
    api_key = os.environ.get('MTA_API_KEY', '')
    response = requests.get(
        f'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-{line}',
        headers={'x-api-key': api_key},
    )
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    for entity in feed.entity:
        print(entity)
        '''
        if entity.HasField('trip_update'):
            print(entity.trip_update)
        elif entity.HasField('vehicle'):
            print(entity.vehicle)
        elif entity.HasField('alert'):
            print(entity.alert)
        '''


def main():
    pull_line_data('ace')

if __name__ == '__main__':
    main()
