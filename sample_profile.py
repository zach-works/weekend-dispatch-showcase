"""
Sample reader profile for the Weekend Dispatch showcase.

Every value here is FICTIONAL. The real dispatch was written for one reader;
this public copy swaps in a made-up reader ("Alex Sample") living near
Fort Greene Park in Brooklyn, a small dog named Biscuit, sample subway
stations, sample interests, and placeholder venues and artists, so the
mechanics can be read without anyone's personal details.

Station stop_ids and walking estimates are illustrative, not verified.
"""

READER_NAME = "Alex Sample"          # fictional
NEIGHBORHOOD = "Fort Greene"

# Center of Fort Greene Park (public landmark), used as the sample "home".
LAT = 40.6914
LON = -73.9759
HOME_LATLON = (LAT, LON)

# Streets used for walk routing (azimuth in degrees, canopy share 0-1).
CORRIDORS = {
    "DeKalb Ave / Lafayette Ave (E-W)": {"azimuth": 100, "canopy": 0.50},
    "Washington Park (N-S)":            {"azimuth": 10,  "canopy": 0.45},
    "Clermont Ave (N-S)":               {"azimuth": 10,  "canopy": 0.40},
}

# Home stations to watch, keyed by GTFS stop_id (sample values).
HOME_STOPS = {
    "A43": "Lafayette Av (C)",
    "A44": "Clinton–Washington Avs (C)",
    "G36": "Fulton St (G)",
    "G35": "Clinton–Washington Avs (G)",
}

# Lines the sample reader rides. GTFS route_id is the bare line letter/number.
HOME_LINES = ["C", "G"]

# The home-station status banner is built from exactly these stations.
BANNER_STATIONS = [
    {"stop_id": "A43", "name": "Lafayette Av", "line": "C"},
    {"stop_id": "G36", "name": "Fulton St",    "line": "G"},
]

# Alternates for the banner's "go to ___ instead" line, nearest first.
ALTERNATE_STATIONS = [
    {"stop_id": "A43", "name": "Lafayette Av",            "lines": ["C"],      "walk": "~0.3 mi south"},
    {"stop_id": "G36", "name": "Fulton St",               "lines": ["G"],      "walk": "~0.4 mi southeast"},
    {"stop_id": "A44", "name": "Clinton–Washington Avs",  "lines": ["C"],      "walk": "~0.6 mi east"},
    {"stop_id": "G35", "name": "Clinton–Washington Avs",  "lines": ["G"],      "walk": "~0.6 mi east"},
    {"stop_id": "A42", "name": "Hoyt–Schermerhorn Sts",   "lines": ["C", "G"], "walk": "~0.8 mi west"},
]

# What the reader likes, used by Claude to filter the culture news.
INTEREST_PROFILE = """
Sample reader interests (fictional):
Fiction: contemporary literary fiction and a few classics.
NYC: neighborhood history, architecture, parks and public space.
Science: space, climate and the natural world, written for a general reader.
Food: neighborhood restaurants and markets.
Professional: technology, design and civic life in Brooklyn.
"""

# DICE watchlist: venue and artist profile pages to follow (placeholders).
DICE_TRACKED_VENUES = {
    "Sample Venue A": "sample-venue-a",
    "Sample Venue B": "sample-venue-b",
    "Sample Venue C": "sample-venue-c",
}

DICE_TRACKED_ARTISTS = {
    "Sample Artist": "sample-artist",
}
