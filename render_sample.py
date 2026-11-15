#!/usr/bin/env python3
"""Render the Weekend Dispatch from fictional sample data.

No network, no email, no API calls: every live source is swapped for sample
data and outbound connections are blocked, so the output shows the layout and
mechanics only. Writes the hosted newsletter, the companion events page and the
envelope email (plus its photo) into sample/.

    python3 render_sample.py
"""
import datetime
import os
import socket

import requests


def _blocked(*args, **kwargs):
    raise requests.exceptions.ConnectionError("network blocked for the sample render")


os.environ.setdefault("WEEKEND_ASSET_BASE_URL", "../assets")   # sample pages load images from the repo

socket.socket.connect = _blocked
socket.create_connection = _blocked
requests.Session.request = _blocked

import weekend_dispatch as wd  # noqa: E402  (import after the network is blocked)

today = datetime.date.today()
saturday = today + datetime.timedelta(days=(5 - today.weekday()) % 7)
sunday = saturday + datetime.timedelta(days=1)


def sample_weather():
    days = [saturday + datetime.timedelta(days=i) for i in range(2)]
    hourly_t, hourly_temp, hourly_rain = [], [], []
    for d, base in zip(days, (52, 56)):
        for h in range(24):
            hourly_t.append(f"{d.isoformat()}T{h:02d}:00")
            hourly_temp.append(round(base + 9 * max(0, 1 - abs(h - 14) / 8), 1))
            hourly_rain.append(10 if h < 17 else 30)
    return {
        "daily": {
            "time": [d.isoformat() for d in days],
            "temperature_2m_max": [61, 65],
            "temperature_2m_min": [47, 50],
            "weathercode": [2, 3],
            "precipitation_probability_max": [10, 30],
        },
        "hourly": {
            "time": hourly_t,
            "temperature_2m": hourly_temp,
            "precipitation_probability": hourly_rain,
        },
    }


SAMPLE_ALERTS = [
    {"text": "Sample alert: G trains run every 12 minutes all weekend for track work.",
     "routes": ["G"], "stop_ids": [], "is_line_level": True},
    {"text": "Sample alert: Manhattan-bound C trains skip Lafayette Av, Sat 6am to Sun 10pm.",
     "routes": ["C"], "stop_ids": ["A43"], "is_line_level": False},
]

SAMPLE_HALL = [
    {"text": f"{saturday.strftime('%a %-m.%-d')} — Club — 10:00 pm @ Main Room — Sample DJ",
     "url": "https://example.com/sample-hall/", "date_str": saturday.strftime("%a %-m.%-d"), "type": "Club"},
    {"text": f"{sunday.strftime('%a %-m.%-d')} — Live — 7:30 pm @ Garden — Sample Band",
     "url": "https://example.com/sample-hall/", "date_str": sunday.strftime("%a %-m.%-d"), "type": "Live"},
]


def sample_rss(url, source_name):
    return [{
        "source": source_name,
        "title": f"Sample headline from {source_name}: a Brooklyn park reopens after a renovation",
        "url": "https://example.com/",
        "desc": "Sample summary text standing in for the real feed item.",
        "date": saturday.isoformat(),
        "is_brooklyn": True,
        "is_manhattan": False,
    }]


def sample_watchlist():
    quiet = {"slug": None, "events": [], "address": "", "state": "quiet",
             "note": "Sample entry: nothing listed in this demo."}
    return {"venues": {name: dict(quiet) for name in wd.DICE_TRACKED_VENUES},
            "artists": {name: dict(quiet) for name in wd.DICE_TRACKED_ARTISTS}}


def sample_narrative():
    return (
        "WEATHER + BISCUIT\n"
        "Sample text: a mild, mostly dry weekend. Saturday peaks near 61°F under high clouds, "
        "so the late-morning walk with Biscuit can run long. Sunday turns cloudier with a 30% "
        "chance of showers after 5pm; take the long walk before lunch.\n\n"
        "UNION SQUARE GREENMARKET\n"
        "Sample text: Go. Cool air and a dry window from 11am to 3:30pm make it an easy outing.\n\n"
        "AROUND BROOKLYN\n"
        "[Sample Source]\n"
        "A neighborhood park reopens — Sample summary standing in for a Claude-written item."
    )


wd.fetch_weekend_weather = sample_weather
wd.fetch_mta_alerts = lambda sat, sun: SAMPLE_ALERTS
wd.fetch_surface_transit_alerts = lambda: []
wd.fetch_market_data = lambda: [{"marketname": "Union Square Greenmarket", "daysoperation": "Mon, Wed, Fri, Sat"}]
wd.fetch_sample_hall_events = lambda: SAMPLE_HALL
wd.fetch_dice_watchlist = sample_watchlist
wd.fetch_dice_events = lambda dates: []
wd._parse_rss = sample_rss
wd.fetch_edible_picks = lambda today, limit=3: []
wd._placeholder_narrative = sample_narrative

os.makedirs("sample", exist_ok=True)
newsletter_html, _narrative, sat, sun, long_list_html = wd.assemble_newsletter(None)
open("sample/weekend_sample.html", "w", encoding="utf-8").write(newsletter_html)
open("sample/events_sample.html", "w", encoding="utf-8").write(long_list_html)

photo = wd.build_envelope_photo(sat)
open("sample/envelope_sample.jpg", "wb").write(photo)
envelope_html = wd.build_envelope_email(sat, sun, "weekend_sample.html", photo_src="envelope_sample.jpg")
open("sample/envelope_sample.html", "w", encoding="utf-8").write(envelope_html)
print("Wrote sample/weekend_sample.html, sample/events_sample.html, sample/envelope_sample.html")
