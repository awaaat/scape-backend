import csv
import os
from functools import lru_cache

CSV_PATH = os.path.join(os.path.dirname(__file__), "kenya_towns_final.csv")


@lru_cache(maxsize=1)
def load_towns():
    """Load the reference town list once per process, cached in memory."""
    towns = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                towns.append({
                    "name": row["name"].strip(),
                    "county": row["county"].strip(),
                    "rank": int(row["rank"]),
                    "lat": float(row["latitude"]),
                    "lng": float(row["longitude"]),
                })
            except (ValueError, KeyError):
                continue  # skip malformed rows rather than crashing
    return towns


def find_nearest_town(lat, lng, haversine_fn, tie_margin_m=500):
    """
    Find the nearest reference town to (lat, lng).
    haversine_fn: the existing _haversine_m(lat1, lng1, lat2, lng2) function.
    tie_margin_m: if two towns are within this many meters of each other,
                  prefer the one with the lower (more senior) rank.
    Returns dict: {name, county, distance_m, rank} or None if list is empty.
    """
    towns = load_towns()
    if not towns:
        return None

    best = None
    for town in towns:
        dist = haversine_fn(lat, lng, town["lat"], town["lng"])
        if best is None:
            best = {**town, "distance_m": dist}
            continue
        if dist < best["distance_m"] - tie_margin_m:
            best = {**town, "distance_m": dist}
        elif abs(dist - best["distance_m"]) <= tie_margin_m:
            if town["rank"] < best["rank"]:
                best = {**town, "distance_m": dist}

    return {
        "name": best["name"],
        "county": best["county"],
        "distance_m": best["distance_m"],
        "rank": best["rank"],
    }


def find_nearest_towns(lat, lng, haversine_fn, n=5, tie_margin_m=500):
    """
    Find the n nearest reference towns to (lat, lng), sorted nearest-first.
    Unlike find_nearest_town(), keeps lat/lng in each result -- callers that
    need a real drive-time call (Routes API) need the coordinates, not just
    the name. Towns within tie_margin_m of each other are ordered by rank
    (lower = more senior/populous) rather than by noise-level distance
    differences.
    """
    towns = load_towns()
    if not towns:
        return []

    scored = []
    for town in towns:
        dist = haversine_fn(lat, lng, town["lat"], town["lng"])
        if dist is None:
            continue
        scored.append({**town, "distance_m": dist})

    scored.sort(key=lambda t: (round(t["distance_m"] / tie_margin_m), t["rank"]))
    top = scored[:n]
    top.sort(key=lambda t: t["distance_m"])  # final display order: true distance

    return [
        {
            "name": t["name"], "county": t["county"], "rank": t["rank"],
            "distance_m": t["distance_m"], "lat": t["lat"], "lng": t["lng"],
        }
        for t in top
    ]
