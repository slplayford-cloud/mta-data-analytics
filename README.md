# MTA Data Analytics

This is a personal project which I am working on to explore solving a problem like NYC subway delays (something every new yorker has experienced) and build my technical skills. This is something I find very personally interesting because I feel like my trains are getting delayed every other week.

A personal project exploring NYC subway delays, something every New Yorker has
experienced, and building my technical skills along the way.

## Building Process
This project is a passion project of mine, however, I also want to note that this is the first time I have every tried a project like this. For this reason, I worked alongside a Claude agent to give me ONLY the boilerplate code. I wrote everything of substance in this project including ingestion, database structure, and more. 

I restarted work on this project because I felt I lost ownership of the core functions and wanted to ensure I used this as a learning experience.

## What I didn't write
I did not write most of the frontend of this project. That is not a passion of mine and to me is more of a means to an end. For that reason most of the frontend uses AI generated code

## What it does

- Polls all 8 MTA realtime feeds (1/2/3/4/5/6/7, A/C/E, B/D/F/M, G, J/Z, N/Q/R/W, L, SIR)
- Resolves each live train to its scheduled baseline, then records every departure
- Stores trips and stop visits in Supabase for later analysis
- Serves a live Mapbox map of every train in the system


## Running

```bash
pip install -r requirements.txt      # or: uv sync

cp .env.example .env                 # then fill in SUPABASE_* and MAPBOX_TOKEN

python scripts/refresh_gtfs.py       # static GTFS is not committed; fetch it
python run_server.py
```

The map is at http://localhost:8000.

## Scripts

```bash
python scripts/refresh_gtfs.py            # download the current GTFS bundle
python scripts/refresh_gtfs.py --check    # non-zero exit if it expires within 14 days
python scripts/check_match_rate.py        # tier coverage against the live feeds
pytest                                    # unit tests, no network needed
```
