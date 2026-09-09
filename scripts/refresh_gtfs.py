#!/usr/bin/env python3
"""
Refresh the static GTFS bundle in static/.

The MTA republishes the subway schedule every few weeks. Each bundle carries a
feed_end_date, and once that date passes the timetable no longer describes what
is actually running — arrival baselines built from it silently go stale.

Run:  python -m scripts.refresh_gtfs
      python -m scripts.refresh_gtfs --check     # report only, change nothing

Exits non-zero if the feed on disk expires within EXPIRY_WARNING_DAYS, so this
can be wired into a cron job or CI check.
"""

import argparse
import csv
import io
import shutil
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests

GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"
STATIC_DIR = Path(__file__).parent.parent / "static"
SHAPE_INDEX = STATIC_DIR / "_shape_stop_index.json"

# Bundles the app cannot run without. shapes.txt drives the map polylines,
# stop_times.txt the timetable baseline, so a partial download is worse than none.
REQUIRED_FILES = {
    "agency.txt", "calendar.txt", "calendar_dates.txt", "feed_info.txt",
    "routes.txt", "shapes.txt", "stop_times.txt", "stops.txt",
    "transfers.txt", "trips.txt",
}

EXPIRY_WARNING_DAYS = 14


def _parse_gtfs_date(value: str) -> date:
    """GTFS dates are YYYYMMDD strings."""
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def read_feed_window(source: Path | zipfile.ZipFile) -> tuple[date, date, str]:
    """Return (start, end, version) from a feed_info.txt, on disk or in a zip."""
    if isinstance(source, zipfile.ZipFile):
        text = source.read("feed_info.txt").decode()
    else:
        text = (source / "feed_info.txt").read_text()

    row = next(csv.DictReader(io.StringIO(text)))
    return (
        _parse_gtfs_date(row["feed_start_date"]),
        _parse_gtfs_date(row["feed_end_date"]),
        row.get("feed_version", "").strip(),
    )


def describe_current() -> tuple[date, int] | None:
    """Print the state of the bundle on disk. Returns (end_date, days_left)."""
    if not (STATIC_DIR / "feed_info.txt").exists():
        print("[gtfs] no feed on disk")
        return None

    start, end, version = read_feed_window(STATIC_DIR)
    days_left = (end - date.today()).days
    state = "EXPIRED" if days_left < 0 else f"{days_left} days left"
    print(f"[gtfs] on disk: {start} → {end}  ({state})")
    print(f"[gtfs] version: {version}")
    return end, days_left


def download() -> zipfile.ZipFile:
    print(f"[gtfs] downloading {GTFS_URL}")
    response = requests.get(GTFS_URL, timeout=120)
    response.raise_for_status()

    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    missing = REQUIRED_FILES - set(bundle.namelist())
    if missing:
        raise RuntimeError(f"bundle is missing required files: {sorted(missing)}")

    print(f"[gtfs] downloaded {len(response.content) / 1e6:.1f} MB, "
          f"{len(bundle.namelist())} files")
    return bundle


def install(bundle: zipfile.ZipFile) -> None:
    """Extract over static/ and drop the derived index so it rebuilds."""
    start, end, version = read_feed_window(bundle)
    print(f"[gtfs] new feed:  {start} → {end}")
    print(f"[gtfs] version:   {version}")

    STATIC_DIR.mkdir(exist_ok=True)
    for name in sorted(REQUIRED_FILES):
        with bundle.open(name) as src, open(STATIC_DIR / name, "wb") as dst:
            shutil.copyfileobj(src, dst)
    print(f"[gtfs] extracted {len(REQUIRED_FILES)} files to {STATIC_DIR}")

    # The shape-stop index is derived from shapes/trips/stop_times. Deleting it
    # forces StaticDataCache to rebuild rather than serve indexes into geometry
    # that no longer exists.
    if SHAPE_INDEX.exists():
        SHAPE_INDEX.unlink()
        print(f"[gtfs] dropped stale {SHAPE_INDEX.name}")


def rebuild_index() -> None:
    """Rebuild derived caches by running the same code the server runs at boot."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.cache import StaticDataCache

    print("[gtfs] rebuilding derived caches …")
    StaticDataCache().build()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report the feed window and exit; download nothing")
    args = parser.parse_args()

    current = describe_current()

    if args.check:
        if current is None:
            return 1
        _, days_left = current
        if days_left < EXPIRY_WARNING_DAYS:
            print(f"[gtfs] FAIL: feed expires within {EXPIRY_WARNING_DAYS} days")
            return 1
        print("[gtfs] OK")
        return 0

    bundle = download()
    _, new_end, _ = read_feed_window(bundle)

    if current is not None and new_end <= current[0]:
        print(f"[gtfs] published feed ends {new_end}, no newer than the current "
              f"{current[0]} — nothing to do")
        return 0

    install(bundle)
    rebuild_index()
    print("[gtfs] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
