#!/usr/bin/env python3
"""
Weekend Dispatch — Fort Greene
Sends every Saturday at 9:30am covering the full weekend.
Sections: MTA alerts · Weather + Biscuit walk windows · Farmers market · BPL events · Brooklyn culture news

Required environment variables:
  ANTHROPIC_API_KEY
  DISPATCH_EMAIL        (Gmail sender address)
  DISPATCH_APP_PASSWORD (Gmail App Password)
  DISPATCH_TO           (recipient address)

Changelog (June 2026):
  - FIX: hour-overflow crash in get_walk_windows when evening best hour is 8pm
    (best_hour + 4 = 24 → invalid datetime hour). Now uses timedelta, which
    correctly rolls into the next UTC day.
  - NEW: market_go_nogo now considers *sustained average* temperature across
    the outing window, not just the peak — a 2-hour round trip with a black
    small dog at 85°F+ average warrants caution even if no single
    hour hits 92°F. Sustained 90°F+ average is a no-go.
  - NEW: fetch_surface_transit_alerts() watches the B65/B43 buses, the JFK
    AirTrain (Jamaica + Howard Beach), and the Nostrand Av LIRR station.
    These are surfaced in the MTA section ONLY when a service is cut, rerouted,
    or suspended — otherwise they are omitted entirely and only train info shows.
  - CHANGE: envelope email redrawn as a single realistic SVG (folded-back flap,
    lit paper, pressed-wax seal, addressed "For Alex Sample"); overall
    envelope height reduced.
  - CHANGE: wax seal now carries the ZT monogram (shared with the Morning
    Dispatch footer) instead of "W.D." — lettering only, cream impression on
    the red wax, with the monogram's serif detailing.
"""

import os
import re
import json
import math
import time
import smtplib
import datetime
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from bs4 import BeautifulSoup
import anthropic

import dispatch_pages as dp
import dispatch_media as dm
import envelope_letter as el
import mta_weekender as mw
import culture_listings as cl
import recipes as rcp

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

from sample_profile import (   # fictional sample values for the public copy
    LAT, LON, TIMEZONE, CORRIDORS, HOME_STOPS, HOME_LINES, BANNER_STATIONS,
    ALTERNATE_STATIONS, INTEREST_PROFILE, DICE_TRACKED_VENUES, DICE_VENUE_NOTES,
    DICE_TRACKED_ARTISTS, DICE_ARTIST_LOCAL_ONLY, HOME_LATLON, READER_NAME, READER_DOG,
    NEIGHBORHOOD, CHCO_ENABLED,
)
DICE_CITY = "new_york-5bbf4db0f06331478e9b2c59"
DICE_BROWSE = f"https://dice.fm/browse/{DICE_CITY}"
DICE_CATEGORIES = [
    "music/gig", "music/dj", "music/party", "music/playback",
    "culture/comedy", "culture/theatre", "culture/art", "culture/film",
    "culture/social", "culture/talks", "culture/foodanddrink",
    "culture/workshop", "culture/wellbeing", "culture/sport", "culture/podcast",
]

# Public Records' shows are added to the On Sale list from its own page (public_records_as_picks), so they
# are dropped from the DICE picks here to avoid printing the same gig twice.
DICE_EXCLUDE_VENUES = {"public records"}

DICE_MAX_MILES = 5.0      # the reader's neighbourhood is the anchor; past this it's a trek
DICE_MAX_PER_VENUE = 2    # keep one busy venue from swallowing the section
DICE_MAX_PER_DAY = 6      # per day, so a busy Saturday can't crowd out Sunday

# The reader goes out on Fridays and Saturdays. Everything else is noise.
DICE_GOING_OUT_WEEKDAYS = (4, 5)   # Monday is 0

# The companion page rolls two weeks forward from the Saturday it's built on,
# so it always shows the next four going-out nights and never a stale tail.
DICE_WINDOW_DAYS = 14

# The email carries five picks total across the two dates; the companion page
# carries the rest.
DICE_HEADLINE_COUNT = 5


# GTFS-realtime Service Alerts, JSON flavor (no API key). Each alert carries an
# informed_entity[] list with route_id and/or stop_id, which is what lets us
# group alerts by line vs. specific stop.
MTA_ALERTS_FEED = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
)

# Public Records (venue)
PUBLIC_RECORDS_URL = "https://publicrecords.nyc/"

# Farmers market
GRAND_ARMY_MARKET_URL = "https://www.grownyc.org/greenmarket/sample-greenmarket"

# GrowNYC Open Data (Brooklyn markets)
GROWNYC_API = "https://data.cityofnewyork.us/resource/b7kx-qikm.json"



# ─────────────────────────────────────────────
# SOLAR + PAVEMENT PHYSICS (from morning dispatch)
# ─────────────────────────────────────────────

def solar_elevation(dt_utc):
    doy = dt_utc.timetuple().tm_yday
    hour_utc = dt_utc.hour + dt_utc.minute / 60
    b = math.radians((360 / 365) * (doy - 81))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    lstm = -75
    lon_correction = 4 * (LON - lstm)
    solar_time = hour_utc * 60 + lon_correction + eot - 240
    hour_angle = math.radians((solar_time / 60 - 12) * 15)
    decl = math.radians(23.45 * math.sin(math.radians((360 / 365) * (doy - 81))))
    lat_r = math.radians(LAT)
    elev = math.degrees(math.asin(
        math.sin(lat_r) * math.sin(decl) +
        math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
    ))
    return max(elev, 0)


def pavement_temp(air_c, solar_elev_deg, canopy=0.45):
    albedo = 0.30
    stefan = 5.67e-8
    solar_irr = max(0, 1000 * math.sin(math.radians(solar_elev_deg)))
    shaded_irr = solar_irr * (1 - canopy)
    absorbed = shaded_irr * (1 - albedo)
    air_k = air_c + 273.15
    net_rad = absorbed - stefan * air_k ** 4 * 0.1
    h = 16
    delta_t = net_rad / h
    return air_c + max(0, delta_t)


def c_to_f(c):
    return c * 9 / 5 + 32


def classify_walk(temp_f, pavement_f, rain_pct, is_winter):
    if temp_f >= 95 or pavement_f >= 125:
        return "🚫 NO WALK", "extreme heat"
    if temp_f <= 20:
        return "🚫 NO WALK", "dangerous cold"
    if rain_pct > 25:
        return "🚫 NO WALK", f"rain {rain_pct}%"
    if is_winter:
        if temp_f <= 32:
            return "⚠️  WIND CHILL", "very cold — booties recommended"
        if temp_f <= 45:
            return "⚠️  CAUTION", "cold — keep it brisk"
        return "✓  GOOD", f"{temp_f:.0f}°F"
    if temp_f >= 90 or pavement_f >= 115:
        return "🔴 HEAT ADVISORY", f"pavement {pavement_f:.0f}°F — go early or skip"
    if temp_f >= 82:
        return "⚠️  CAUTION", f"warm ({temp_f:.0f}°F) — keep it short"
    return "✓  GOOD", f"{temp_f:.0f}°F"


# ─────────────────────────────────────────────
# WEATHER
# ─────────────────────────────────────────────

def fetch_weekend_weather():
    """Pull hourly weather for Saturday + Sunday from Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "temperature_2m,precipitation_probability,weathercode",
        "daily": "temperature_2m_max,temperature_2m_min,weathercode,precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": TIMEZONE,
        "forecast_days": 7,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def parse_weekend_days(weather_data):
    """Return data for Saturday and Sunday (next occurrence)."""
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    if days_until_sat == 0:
        days_until_sat = 0  # today is Saturday
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    daily_times = weather_data["daily"]["time"]
    daily_max = weather_data["daily"]["temperature_2m_max"]
    daily_min = weather_data["daily"]["temperature_2m_min"]
    daily_code = weather_data["daily"]["weathercode"]
    daily_rain = weather_data["daily"]["precipitation_probability_max"]

    hourly_times = weather_data["hourly"]["time"]
    hourly_temps = weather_data["hourly"]["temperature_2m"]
    hourly_rain = weather_data["hourly"]["precipitation_probability"]

    result = {}
    for target_date, label in [(saturday, "saturday"), (sunday, "sunday")]:
        date_str = target_date.isoformat()
        if date_str in daily_times:
            idx = daily_times.index(date_str)
            hourly_for_day = {}
            for h_idx, h_time in enumerate(hourly_times):
                if h_time.startswith(date_str):
                    hour = int(h_time[11:13])
                    hourly_for_day[hour] = {
                        "temp_f": hourly_temps[h_idx],
                        "rain_pct": hourly_rain[h_idx],
                    }
            result[label] = {
                "date": target_date,
                "high_f": daily_max[idx],
                "low_f": daily_min[idx],
                "weather_code": daily_code[idx],
                "rain_pct_max": daily_rain[idx],
                "hourly": hourly_for_day,
            }
    return result


WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


def get_walk_windows(day_data, day_label):
    """
    Generate walk windows based on realistic weekend departure assumptions.

    Primary window: 10am–noon. Alex is not getting up at 6am unless forced.
    Rain fallback: if 10am–noon has >25% rain chance, flag that and find the
    best alternate hour in the 8am–9am or 1pm–3pm range instead.
    Evening window: always assessed (5pm–8pm) as a second walk option.

    Returns 2 windows: [primary/fallback, evening].
    """
    date = day_data["date"]
    month = date.month
    is_winter = month in [11, 12, 1, 2, 3]
    hourly = day_data["hourly"]

    def best_in_range(hours):
        """Return (best_hour, temp_f, rain_pct, score) for a range."""
        best_hour = None
        best_score = -9999
        for h in hours:
            if h not in hourly:
                continue
            temp_f = hourly[h]["temp_f"]
            rain_pct = hourly[h]["rain_pct"]
            if rain_pct > 25:
                score = -100
            elif is_winter:
                score = temp_f - abs(temp_f - 55) * 0.5
            else:
                score = -(temp_f - 72) ** 2 - rain_pct * 0.5
            if score > best_score:
                best_score = score
                best_hour = h
        if best_hour is None:
            return None
        return (best_hour, hourly[best_hour]["temp_f"],
                hourly[best_hour]["rain_pct"], best_score)

    def make_window(zone_name, hour, temp_f, rain_pct, forced_early=False):
        dt_utc = (datetime.datetime(date.year, date.month, date.day)
                  + datetime.timedelta(hours=hour + 4))
        elev = solar_elevation(dt_utc)
        pave_c = pavement_temp((temp_f - 32) * 5 / 9, elev)
        pave_f = c_to_f(pave_c)
        status, note = classify_walk(temp_f, pave_f, rain_pct, is_winter)
        label_hour = f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"
        if forced_early:
            note = f"rain in your usual window — earlier start: {note}"
        return {
            "zone": zone_name,
            "hour_label": label_hour,
            "status": status,
            "note": note,
            "forced_early": forced_early,
        }

    windows = []

    # ── PRIMARY WINDOW ──────────────────────────────────────────────────────
    # Preferred: 10am–noon. Check if rain forces an earlier or later start.
    preferred = best_in_range(range(10, 13))
    rain_in_preferred = all(
        hourly.get(h, {}).get("rain_pct", 0) > 25 for h in range(10, 13)
        if h in hourly
    )

    if preferred and not rain_in_preferred:
        h, temp_f, rain_pct, _ = preferred
        windows.append(make_window("Morning", h, temp_f, rain_pct))
    else:
        # Rain in the preferred window — find the best nearby alternative
        early = best_in_range(range(8, 10))     # 8–9am fallback
        late  = best_in_range(range(13, 16))    # 1–3pm fallback
        best_alt = None
        if early and late:
            best_alt = early if early[3] >= late[3] else late
        elif early:
            best_alt = early
        elif late:
            best_alt = late

        if best_alt:
            h, temp_f, rain_pct, _ = best_alt
            forced = h < 10
            windows.append(make_window("Morning", h, temp_f, rain_pct,
                                       forced_early=forced))
        elif preferred:
            # Everything is bad — just report the preferred window as-is
            h, temp_f, rain_pct, _ = preferred
            windows.append(make_window("Morning", h, temp_f, rain_pct))

    # ── EVENING WINDOW ───────────────────────────────────────────────────────
    evening = best_in_range(range(17, 21))
    if evening:
        h, temp_f, rain_pct, _ = evening
        windows.append(make_window("Evening", h, temp_f, rain_pct))

    return windows


# ─────────────────────────────────────────────
# MTA SERVICE ALERTS
# ─────────────────────────────────────────────

def _gtfs_translated_text(field):
    """
    Pull plain text out of a GTFS-realtime TranslatedString:
    {"translation": [{"text": ..., "language": "en"}, ...]}.
    Prefer English; fall back to the first translation. Returns "" if empty.
    """
    translations = (field or {}).get("translation", []) or []
    for t in translations:
        if (t.get("language") or "").lower().startswith("en"):
            return (t.get("text") or "").strip()
    return (translations[0].get("text") or "").strip() if translations else ""


# Mercury alert_type buckets. The feed's GTFS cause/effect are empty, so the
# planned-vs-realtime split keys off transit_realtime.mercury_alert.alert_type.
# Planned changes use a "Planned - ..." prefix (Reroute, Part Suspended, Stops
# Skipped, Express to Local, etc.); transient incidents are typed "Delays".
REALTIME_ALERT_TYPES = {"Delays"}
# Text backstop for transient incidents whose alert_type is missing/odd. These
# are the real-time delay/signal-problem markers we drop; planned reroutes,
# suspensions, skip-stops, and express patterns never contain them.
REALTIME_KEYWORDS = [
    "delay", "delayed", "signal problem", "signal issue", "sick customer",
    "police activity", "investigation", "smoke", "fire department",
    "medical", "offloading", "switch problem", "rail condition", "track fire",
]


def _mercury_alert_type(alert):
    """The MTA Mercury extension's human alert_type, e.g. 'Planned - Reroute'."""
    return (alert.get("transit_realtime.mercury_alert", {}) or {}).get("alert_type", "")


def _is_transient_alert(alert, text):
    """
    True for transient real-time incidents (delays, signal problems) we drop.
    False for planned/scheduled service changes — reroutes, suspensions,
    skip-stops, express-to-local, etc. — which we keep.
    """
    if _mercury_alert_type(alert) in REALTIME_ALERT_TYPES:
        return True
    low = text.lower()
    return any(kw in low for kw in REALTIME_KEYWORDS)


def _is_extra_service(alert):
    """
    True when an alert announces added service (July 4th extras, enhanced
    headways, etc.) rather than a disruption. These should appear in the
    service-changes list but must NOT trigger the home-station banner.
    Checks Mercury alert_type first; falls back to text detection.
    """
    at = alert.get("alert_type", "").lower()
    if "extra service" in at or "additional service" in at:
        return True
    text = alert.get("text", "").lower()
    return (text.startswith("additional ") or "extra service" in text
            or "enhanced service" in text or "supplemental service" in text)


def _weekend_epoch_window(saturday, sunday):
    """
    Epoch-second bounds for the weekend, in the dispatch's local timezone:
    Saturday 00:00:00 through Sunday 23:59:59 ET. Same Sat/Sun the weather and
    Greenmarket sections use (passed in from main()).
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TIMEZONE)
    start = datetime.datetime(saturday.year, saturday.month, saturday.day,
                              0, 0, 0, tzinfo=tz)
    end = datetime.datetime(sunday.year, sunday.month, sunday.day,
                            23, 59, 59, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _alert_active_in_window(alert, win_start, win_end):
    """
    True if any of the alert's active_period ranges overlaps the weekend window.
    A missing 'end' means open-ended (ongoing); a missing active_period means
    always-active (GTFS-rt) and so counts as overlapping.
    """
    periods = alert.get("active_period") or []
    if not periods:
        return True
    for p in periods:
        start = p.get("start") or 0
        end = p.get("end") or (1 << 62)  # open-ended
        if start <= win_end and end >= win_start:
            return True
    return False


def fetch_mta_alerts(saturday, sunday):
    """
    Pull MTA subway service alerts that inform route A, C, or 3 from the
    GTFS-realtime Service Alerts feed (camsys JSON), system-wide — not scoped to
    any hand-picked station list — then apply two filters and split into groups.

    Filters:
      - Planned vs. transient: keep planned/scheduled changes (reroutes,
        suspensions, skip-stops, express-to-local, …); drop transient real-time
        incidents (delays, signal problems). See _is_transient_alert.
      - Weekend scope: keep only alerts whose active_period overlaps the
        Saturday–Sunday window (the same window weather/Greenmarket use); drop
        changes planned for later in the week. See _alert_active_in_window.

    Each alert carries an informed_entity[] list; an entity may name a route
    (route_id), a stop (stop_id), or both. For each alert we take the union of
    all route_ids and all stop_ids across its entities, and keep it only if it
    informs one of HOME_LINES (A / C / 3).

    Returns a flat list (no your/elsewhere grouping — the banner and the single
    A/C/3 change list are derived from this list downstream). Each item:
      {
        "text":          alert header (falls back to description),
        "routes":        ["A", "C", ...] limited to A/C/3, in HOME_LINES order,
        "stop_ids":      sorted list of every stop_id the alert names,
        "is_line_level": True when the alert names no specific stop (whole-line
                         change); False when it names one or more stops,
      }
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        r = requests.get(MTA_ALERTS_FEED, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    home_lines = set(HOME_LINES)
    win_start, win_end = _weekend_epoch_window(saturday, sunday)

    alerts = []
    seen = set()

    for entity in data.get("entity", []):
        alert = entity.get("alert")
        if not alert:
            continue

        informed = alert.get("informed_entity", []) or []
        routes = {ie.get("route_id") for ie in informed if ie.get("route_id")}
        stops = {ie.get("stop_id") for ie in informed if ie.get("stop_id")}

        # Route filter: the alert must inform the A, C, or 3.
        matched_routes = routes & home_lines
        if not matched_routes:
            continue

        text = (_gtfs_translated_text(alert.get("header_text"))
                or _gtfs_translated_text(alert.get("description_text")))
        if not text:
            continue

        # Planned-vs-transient filter: drop real-time delays / signal problems,
        # keep planned reroutes, suspensions, skip-stops, express patterns.
        if _is_transient_alert(alert, text):
            continue

        # Drop bare redirect alerts with no route context (e.g. "Take the [A] instead").
        # These are unhelpful without knowing why and which stop is affected.
        import re as _re
        if len(text) < 80 and _re.match(r'(take|use) the \[', text.lower()):
            continue

        # Weekend-scope filter: only alerts active Saturday or Sunday.
        if not _alert_active_in_window(alert, win_start, win_end):
            continue

        # De-dupe identical alerts that recur across entities.
        dedupe_key = text[:160]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        alerts.append({
            "text": text,
            "plain": mw.plain_alert_text(text),
            "when": mw.alert_when(alert, saturday, sunday),
            "detail": mw.alert_detail(alert),
            "routes": [r for r in HOME_LINES if r in matched_routes],
            "stop_ids": sorted(stops),
            "is_line_level": not stops,
            "alert_type": _mercury_alert_type(alert),
        })

    return alerts


def fetch_surface_transit_alerts():
    """
    Conditional alerts for the B65 / B43 buses, the JFK AirTrain (Jamaica and
    Howard Beach), and the Nostrand Avenue LIRR station.

    These services are ONLY surfaced when they are disrupted — a planned
    reroute, suspension, or service cut. If nothing matching is found, this
    returns an empty list and the services are not mentioned in the dispatch
    at all (per the "only if cut or altered" rule). Normal subway info is
    handled separately by fetch_mta_alerts.

    Returns a list of {"service": <label>, "text": <alert text>} dicts.
    """
    # Each watched service: a label plus the substrings that identify it.
    WATCHED = [
        ("B65 bus",            ["b65"]),
        ("B43 bus",            ["b43"]),
        ("JFK AirTrain (Jamaica)",     ["airtrain", "air train"]),
        ("JFK AirTrain (Howard Beach)", ["airtrain", "air train"]),
        ("Nostrand Av LIRR",   ["nostrand av", "nostrand avenue"]),
    ]

    # Words that indicate the service is actually CUT or ALTERED (keep these).
    DISRUPTION_KEYWORDS = [
        "reroute", "rerouted", "rerouting", "detour", "diverted", "diversion",
        "suspended", "suspension", "no service", "not running", "will not run",
        "cancelled", "canceled", "service change", "service cut", "cut",
        "skipping", "will not stop", "won't stop", "bypass", "bypassing",
        "replacement bus", "shuttle", "running via", "out of service",
        "closed", "closure", "no trains", "no buses", "not stopping",
    ]
    # Real-time noise we explicitly ignore even if it mentions a service.
    REALTIME_KEYWORDS = [
        "delay", "delayed", "running late", "signal problem", "sick customer",
        "police activity", "investigation", "smoke", "medical", "minor delays",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    found = []
    seen = set()

    for url in ["https://new.mta.info/alerts", "https://www.mta.info/alerts"]:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.find_all(string=True):
                text = el.strip()
                if len(text) < 20:
                    continue
                low = text.lower()

                # Must reference one of the watched services.
                matched_service = None
                for label, needles in WATCHED:
                    if any(n in low for n in needles):
                        # For AirTrain, require the right station context so a
                        # generic AirTrain mention doesn't fire both entries.
                        if "airtrain" in label.lower() or "air train" in low:
                            if "jamaica" in label.lower() and "jamaica" not in low:
                                continue
                            if "howard beach" in label.lower() and "howard beach" not in low:
                                continue
                        matched_service = label
                        break
                if not matched_service:
                    continue

                # Must describe an actual cut/alteration, not a real-time delay.
                if not any(kw in low for kw in DISRUPTION_KEYWORDS):
                    continue
                if any(kw in low for kw in REALTIME_KEYWORDS):
                    continue

                key = (matched_service, text[:120])
                if key in seen:
                    continue
                seen.add(key)
                found.append({"service": matched_service, "text": text[:300]})

            if found:
                break
        except Exception:
            continue

    return found[:6]


# ─────────────────────────────────────────────
# FARMERS MARKET
# ─────────────────────────────────────────────

def fetch_market_data():
    """Pull Brooklyn greenmarket data from GrowNYC open data."""
    try:
        params = {
            "$where": "lower(borough) = 'brooklyn'",
            "$limit": 50,
        }
        r = requests.get(GROWNYC_API, params=params, timeout=10)
        r.raise_for_status()
        markets = r.json()
        # Filter to Saturday-operating markets
        saturday_markets = [
            m for m in markets
            if "saturday" in str(m.get("daysoperation", "")).lower()
            or "sat" in str(m.get("daysoperation", "")).lower()
        ]
        return saturday_markets
    except Exception as e:
        return [{"marketname": f"[Market data unavailable: {e}]"}]


def market_go_nogo(saturday_data, market_list):
    """
    Go/no-go logic for Sample Greenmarket.
    Rules:
    - Rain > 15% during 11am–3:30pm outing window → no-go
    - Sustained heat: average ≥ 90°F across the outing window → no-go
      (2 hours of round-trip walking with a black-coated dog in 90°F+ is unsafe)
    - Peak ≥ 92°F at any point, OR average ≥ 85°F → caution
      (a single hot hour can be timed around; a hot *average* can't)
    - Temp < 32°F average → no-go (not a summer concern but included)
    Walk time: 1 hour each way with Biscuit. Outing window: 11am–3:30pm.
    """
    hourly = saturday_data.get("hourly", {})
    outing_hours = [11, 12, 13, 14, 15]  # 11am–3pm

    # No forecast (the weather service was down this run): say so, don't invent a verdict.
    if not any(h in hourly for h in outing_hours):
        gap = next((m for m in market_list if "grand army" in str(m.get("marketname", "")).lower()), None)
        return {
            "verdict": "UNKNOWN",
            "reason": "The forecast wasn't available this morning, so there's no go/no-go. Check the weather before heading out with Biscuit.",
            "market_hours": gap.get("hoursoperations", "8am–3pm") if gap else "8am–3pm",
            "max_rain_outing": None, "peak_temp": None, "avg_temp": None,
        }

    max_rain = max((hourly.get(h, {}).get("rain_pct", 0) for h in outing_hours), default=0)
    temps_outing = [hourly.get(h, {}).get("temp_f", 72) for h in outing_hours if h in hourly]
    peak_temp = max(temps_outing) if temps_outing else 72
    avg_temp = sum(temps_outing) / len(temps_outing) if temps_outing else 72

    if max_rain > 15:
        verdict = "NO-GO"
        reason = f"Rain chance hits {max_rain:.0f}% during your outing window. Market's not worth the gamble with Biscuit."
    elif avg_temp >= 90:
        verdict = "NO-GO"
        reason = (f"Sustained {avg_temp:.0f}°F average across your whole outing window — "
                  f"two hours of walking in that heat is unsafe for Biscuit. Go solo by subway, or skip.")
    elif peak_temp >= 92 or avg_temp >= 85:
        verdict = "CAUTION"
        if avg_temp >= 85:
            reason = (f"Averages {avg_temp:.0f}°F across the outing (peak {peak_temp:.0f}°F) — "
                      f"that's sustained heat for a black-coated dog. Bring water, stick to shaded "
                      f"blocks, and seriously consider going before 11am or leaving Biscuit home.")
        else:
            reason = f"Peaks at {peak_temp:.0f}°F — doable but bring water, go early (before 11am if you can)."
    elif avg_temp < 32:
        verdict = "NO-GO"
        reason = "Too cold for the walk with Biscuit."
    else:
        verdict = "GO"
        reason = f"Conditions look solid — {avg_temp:.0f}°F average, rain {max_rain:.0f}% during your window."

    # Find Sample Greenmarket specifically in the market list
    gap = next((m for m in market_list if "grand army" in str(m.get("marketname", "")).lower()), None)
    market_hours = gap.get("hoursoperations", "8am–3pm") if gap else "8am–3pm"

    return {
        "verdict": verdict,
        "reason": reason,
        "market_hours": market_hours,
        "max_rain_outing": max_rain,
        "peak_temp": peak_temp,
        "avg_temp": avg_temp,
    }


# Northeast seasonal produce calendar for Sample Greenmarket.
# Reflects what's typically at peak for the NYC/Hudson Valley growing region.
_SEASONAL_PRODUCE = {
    1:  ["storage apples", "storage pears", "winter squash", "sweet potatoes", "turnips",
         "parsnips", "celeriac", "dried beans", "greenhouse greens", "aged cheeses"],
    2:  ["storage apples", "storage pears", "winter squash", "sweet potatoes", "parsnips",
         "celeriac", "greenhouse micro greens", "maple syrup (early)", "aged cheeses"],
    3:  ["storage apples", "storage pears", "greenhouse lettuce", "spinach", "radishes",
         "maple syrup", "greenhouse herbs", "dried beans", "aged cheeses"],
    4:  ["asparagus (early)", "ramps", "spring onions", "greenhouse lettuce", "spinach",
         "radishes", "arugula", "greenhouse herbs", "maple syrup", "parsnips"],
    5:  ["asparagus", "ramps", "snap peas (early)", "strawberries (early)", "lettuce",
         "spinach", "arugula", "radishes", "spring onions", "herbs", "rhubarb"],
    6:  ["strawberries", "snap peas", "asparagus (late)", "garlic scapes", "lettuce",
         "arugula", "spinach", "radishes", "beets", "kohlrabi", "herbs (basil, dill)",
         "cucumbers (early)", "zucchini (early)", "scallions", "rhubarb"],
    7:  ["blueberries", "raspberries", "sour cherries", "garlic (new crop)", "zucchini",
         "summer squash", "cucumbers", "tomatoes (early heirlooms, Sungolds)",
         "green beans", "basil", "eggplant (early)", "corn (early)", "beets",
         "carrots", "kohlrabi", "herbs", "peppers (early)"],
    8:  ["tomatoes (peak — heirlooms, Brandywine, Cherokee Purple)", "corn", "peppers",
         "eggplant", "peaches", "nectarines", "plums", "blueberries", "blackberries",
         "melons", "green beans", "basil", "zucchini", "summer squash",
         "potatoes (new)", "onions", "garlic", "cucumbers"],
    9:  ["apples (Honeycrisp, Cortland, Gala)", "pears", "plums", "grapes",
         "tomatoes (late)", "peppers", "eggplant", "corn (late)", "broccoli",
         "kale", "Brussels sprouts (early)", "sweet potatoes (early)",
         "winter squash (early — delicata, acorn)", "cauliflower", "leeks"],
    10: ["apples (peak — 20+ varieties)", "pears", "winter squash (butternut, delicata, acorn)",
         "sweet potatoes", "Brussels sprouts", "kale", "broccoli", "cauliflower",
         "beets", "turnips", "parsnips", "carrots", "leeks", "fennel", "grapes"],
    11: ["storage apples", "winter squash", "sweet potatoes", "Brussels sprouts",
         "kale", "collards", "turnips", "parsnips", "celeriac", "leeks", "fennel",
         "beets", "carrots", "greenhouse greens"],
    12: ["storage apples", "storage pears", "winter squash", "sweet potatoes",
         "turnips", "parsnips", "celeriac", "dried beans", "greenhouse greens",
         "aged cheeses", "cider"],
}


def get_seasonal_produce(date):
    """Return a formatted string of what's in season at Sample Greenmarket this month."""
    items = _SEASONAL_PRODUCE.get(date.month, [])
    return ", ".join(items) if items else "seasonal produce varies"


# ─────────────────────────────────────────────
# SUN, GOLDEN HOUR AND TIDES (under the forecast) · SEASONAL RECIPE (Greenmarket section)
# ─────────────────────────────────────────────



def _clock(hours):
    """Decimal clock hours to '6:42a' style, rounded to the minute."""
    total = round(hours * 60) % (24 * 60)
    h, m = divmod(total, 60)
    return f"{h % 12 or 12}:{m:02d}{'a' if h < 12 else 'p'}"


def fetch_tide_hilo(saturday, sunday):
    """High and low tides at the Rockaway station for Saturday and Sunday: [(datetime, 'H' or 'L')].
    Returns [] when NOAA cannot be read."""
    params = {"application": "WeekendDispatch", "begin_date": saturday.strftime("%Y%m%d"),
              "end_date": sunday.strftime("%Y%m%d"), "datum": "MLLW", "station": TIDE_STATION,
              "time_zone": "lst_ldt", "units": "english", "format": "json",
              "product": "predictions", "interval": "hilo"}
    try:
        r = requests.get(NOAA_TIDES_URL, timeout=30, params=params)
        r.raise_for_status()
        return [(datetime.datetime.strptime(p["t"], "%Y-%m-%d %H:%M"), p["type"])
                for p in r.json()["predictions"]]
    except Exception as e:
        print(f"  [sun/tide] tides unavailable: {e}")
        return []


def build_sun_tide_html(saturday, sunday):
    """Sunrise, sunset, golden hour and tides for each weekend day, placed under the forecast.
    Empty when none of it can be worked out. Sun times are worked out for Fort Greene (no network needed);
    tides come from NOAA's Rockaway station, the same one Shore Leave uses."""
    try:
        hilo = fetch_tide_hilo(saturday, sunday)
        rows = []
        for d, label in ((saturday, "Saturday"), (saturday + datetime.timedelta(days=1), "Sunday")):
            rise, set_ = _sun_times(d, LAT, LON)
            lines = [f"sunrise {_clock(rise)} &middot; sunset {_clock(set_)}",
                     f"golden hour {_clock(rise)}&ndash;{_clock(rise + 1)} in the morning, "
                     f"{_clock(set_ - 1)}&ndash;{_clock(set_)} in the evening"]
            if any(t.date() == d for t, _ in hilo):
                highs = ", ".join(_clock(t.hour + t.minute / 60) for t, ty in hilo if t.date() == d and ty == "H")
                lows = ", ".join(_clock(t.hour + t.minute / 60) for t, ty in hilo if t.date() == d and ty == "L")
                lines.append(f"tides: high {highs} &middot; low {lows}")
            rows.append(f"<div style='margin:0 0 8px;'><b>{label}</b><br>{'<br>'.join(lines)}</div>")
        if not rows:
            return ""
        return (f"<div class='sun-tide' style=\"margin:14px 0 4px;padding:12px 14px;border:1px solid #D4B98A;"
                f"font-size:14.5px;line-height:1.6;color:#2A1E0A;\">{''.join(rows)}"
                f"<div style='font-size:11.5px;color:#7A6A4A;'>Golden hour is taken as the hour after sunrise and "
                f"the hour before sunset. Tides: NOAA, East Rockaway Inlet, so the times run a little off for Fort Greene.</div></div>")
    except Exception as e:
        print(f"  [sun/tide] skipped: {e}")
        return ""



# ─────────────────────────────────────────────
# PUBLIC RECORDS EVENTS
# ─────────────────────────────────────────────

def fetch_public_records_events():
    """
    Scrape upcoming shows from Public Records (233 Butler St, Gowanus).
    Their homepage lists all events in server-rendered HTML with Dice.fm ticket links.
    Returns list of dicts with 'text', 'url', 'date_str', 'type' fields.
    """
    import re as _re
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    events = []
    try:
        r = requests.get("https://publicrecords.nyc/", headers=headers, timeout=15)
        if r.status_code != 200:
            return events
        soup = BeautifulSoup(r.text, "html.parser")

        # Each row div contains: "Day M.DD Type,\n   HH:MM pm, Room"
        # Its parent <a> wraps the row and sibling divs for artist names.
        today = datetime.date.today()
        current_year = today.year

        for el in soup.find_all("div"):
            text = el.get_text(" ", strip=True)
            # Match: "Sat 7.4 Club, 3:00 pm, Sound Room The Atrium The Nursery Upstairs"
            m = _re.match(
                r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\.(\d{1,2})"
                r"\s+(Live|Club|Etc),\s*(\d{1,2}:\d{2}\s*(?:am|pm)),\s*(.+)$",
                text, _re.I
            )
            if not m:
                continue

            month_num = int(m.group(2))
            day_num   = int(m.group(3))
            event_type = m.group(4)
            event_time = m.group(5).strip()
            room       = m.group(6).strip()

            try:
                event_date = datetime.date(current_year, month_num, day_num)
                if event_date < today:
                    event_date = datetime.date(current_year + 1, month_num, day_num)
            except ValueError:
                continue

            # Artist names are in a sibling div inside the same parent <a>
            parent_a = el.find_parent("a", href=True)
            if not parent_a:
                continue

            siblings_text = [
                s.get_text(" ", strip=True)
                for s in el.parent.children
                if hasattr(s, "get_text") and s.get_text(strip=True) and s is not el
            ]
            # Artist div is the first sibling; strip "Get tickets" noise
            artists_raw = siblings_text[0] if siblings_text else ""
            artists = _re.sub(r"\s*Get tickets.*$", "", artists_raw, flags=_re.I).strip()

            date_label = event_date.strftime("%a %b %-d")
            summary = f"{date_label} — {event_type} — {event_time} @ {room}"
            if artists:
                summary += f" — {artists}"

            events.append({
                "text": summary[:350],
                "url": parent_a["href"],
                "date": event_date,
                "type": event_type,
            })

    except Exception:
        pass

    # Keep only the upcoming Saturday's events, dedupe by URL
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    next_saturday = today + datetime.timedelta(days=days_until_sat)

    seen_urls = set()
    unique = []
    for e in sorted(events, key=lambda x: x["date"]):
        if e["date"] != next_saturday:
            continue
        if e["url"] not in seen_urls:
            seen_urls.add(e["url"])
            unique.append(e)

    return unique


def dice_going_out_dates(saturday, days=DICE_WINDOW_DAYS):
    """
    The Fridays and Saturdays inside the rolling window, starting from the
    Saturday the dispatch is built on. With the default fortnight that's four
    nights: this Saturday, then Fri/Sat, then the Friday after.
    """
    return [
        saturday + datetime.timedelta(days=i)
        for i in range(days)
        if (saturday + datetime.timedelta(days=i)).weekday() in DICE_GOING_OUT_WEEKDAYS
    ]


def _is_new_york(address):
    """True when a DICE venue address is in New York."""
    a = (address or "").lower()
    return (", ny" in a or "new york" in a
            or "brooklyn" in a or "queens" in a or "bronx" in a)

_DICE_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def _haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    radius = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def _dice_price(e):
    """DICE now quotes 'amount_from' (cents) where it used to quote 'amount'."""
    p = e.get("price") or {}
    amount, frm = p.get("amount"), p.get("amount_from")
    fmt = lambda c: f"${c / 100:.0f}" if c % 100 == 0 else f"${c / 100:.2f}"
    if amount == 0 or (amount is None and frm == 0):
        return "Free"
    if isinstance(amount, int) and amount > 0:
        return fmt(amount)
    if isinstance(frm, int) and frm > 0:
        return f"from {fmt(frm)}"
    return ""


def _dice_url(e):
    """perm_name is gone from the page JSON; /event/<id> redirects to the ticket page."""
    if e.get("perm_name"):
        return f"https://dice.fm/event/{e['perm_name']}"
    return f"https://dice.fm/event/{e['id']}" if e.get("id") else ""


_VENUE_MILES = {}


def _venue_miles(address):
    """Miles from home to a venue address, via NYC Planning's GeoSearch. None if it can't be placed."""
    if not address:
        return None
    if address not in _VENUE_MILES:
        miles = None
        try:
            r = requests.get("https://geosearch.planninglabs.nyc/v2/search",
                             params={"text": address, "size": 1}, timeout=15)
            feats = r.json().get("features") or []
            if feats:
                lon, lat = feats[0]["geometry"]["coordinates"]
                miles = _haversine_miles(LAT, LON, lat, lon)
        except Exception as e:
            print(f"  [dice] geocode failed for {address!r}: {e}")
        _VENUE_MILES[address] = miles
    return _VENUE_MILES[address]


def _fetch_dice_category(category):
    """Return the raw event dicts embedded in one DICE browse page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(f"{DICE_BROWSE}/{category}", headers=headers, timeout=20)
    if r.status_code != 200:
        return []
    m = _DICE_NEXT_DATA_RE.search(r.text)
    if not m:
        return []
    try:
        page_props = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError):
        return []
    return page_props.get("events") or []


def fetch_dice_events(target_dates):
    """
    Pull Brooklyn shows falling on any of `target_dates` from DICE's browse
    pages — the wider net that backs the companion page's third section.

    Browse pages only reach two to four days out, so in practice this fills in
    the nearest going-out night and thins out after that. The watchlist's
    profile pages carry the depth; this carries the breadth.

    Every field printed comes straight from DICE's own JSON — nothing is
    inferred or rewritten — and each event keeps its ticket URL so the claim is
    checkable. Anything missing a venue, a start time, or a Brooklyn address is
    dropped rather than guessed at.

    Returns a list of dicts sorted by start time.
    """
    raw = {}
    for category in DICE_CATEGORIES:
        try:
            for e in _fetch_dice_category(category):
                eid = e.get("id")
                if eid and eid not in raw:
                    raw[eid] = e
        except Exception:
            continue  # one dead category shouldn't sink the section
        time.sleep(1.0)  # be a polite guest

    target_dates = set(target_dates)
    events = []
    for e in raw.values():
        iso = (e.get("dates") or {}).get("event_start_date")
        if not iso:
            continue
        try:
            start = datetime.datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if start.date() not in target_dates:
            continue

        venues = e.get("venues") or []
        if not venues:
            continue
        venue = venues[0]
        venue_name = (venue.get("name") or "").strip()
        address = venue.get("address") or ""
        if "brooklyn" not in address.lower():
            continue
        if venue_name.lower() in DICE_EXCLUDE_VENUES:
            continue

        # Cancelled/postponed shows are not worth sending anyone out for.
        if (e.get("status") or "").lower() in ("cancelled", "postponed"):
            continue

        # DICE dropped per-venue coordinates in 2026 (only a city centroid is
        # left), so distance comes from geocoding the street address. A venue
        # we can't place is dropped: the page promises "within five miles".
        miles = _venue_miles(address)
        if miles is None or miles > DICE_MAX_MILES:
            continue

        price = _dice_price(e)

        artists = [
            a.get("name") for a in
            ((e.get("summary_lineup") or {}).get("top_artists") or [])
            if a.get("name")
        ]

        events.append({
            "name": (e.get("name") or "").strip(),
            "url": _dice_url(e),
            "start": start,
            "date": start.date(),
            "venue": venue_name,
            "miles": miles,
            "price": price,
            "sold_out": (e.get("status") or "").lower() == "sold-out",
            "artists": artists,
        })

    events.sort(key=lambda x: (x["start"], x["venue"]))

    # Trim, preserving chronological order: cap each venue so one busy room
    # can't swallow the section, and cap each day separately so a packed
    # Saturday can't push Sunday out entirely.
    per_venue, per_day = {}, {}
    trimmed = []
    for e in events:
        venue_key = e["venue"].lower()
        if per_venue.get(venue_key, 0) >= DICE_MAX_PER_VENUE:
            continue
        if per_day.get(e["date"], 0) >= DICE_MAX_PER_DAY:
            continue
        per_venue[venue_key] = per_venue.get(venue_key, 0) + 1
        per_day[e["date"]] = per_day.get(e["date"], 0) + 1
        trimmed.append(e)

    return trimmed


def _dice_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _fetch_dice_profile(url):
    """
    Read a DICE venue or artist page.

    Venue pages put the payload under pageProps.profile, artist pages under
    pageProps.initialProfile — same shape underneath. Returns
    (subject_dict, events_list, error_string_or_None).
    """
    try:
        r = requests.get(url, headers=_dice_headers(), timeout=25)
    except Exception as e:
        return {}, [], f"request failed: {e}"
    if r.status_code != 200:
        return {}, [], f"HTTP {r.status_code}"

    m = _DICE_NEXT_DATA_RE.search(r.text)
    if not m:
        return {}, [], "no __NEXT_DATA__"
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError) as e:
        return {}, [], f"parse: {e}"

    prof = pp.get("profile") or pp.get("initialProfile") or {}
    subject = prof.get("venue") or prof.get("artist") or {}

    events = []
    for section in prof.get("sections") or []:
        for e in section.get("events") or []:
            iso = (e.get("dates") or {}).get("event_start_date")
            if not iso:
                continue
            try:
                start = datetime.datetime.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if (e.get("status") or "").lower() in ("cancelled", "postponed"):
                continue
            v = (e.get("venues") or [{}])[0]
            price = _dice_price(e)
            events.append({
                "name": (e.get("name") or "").strip(),
                "url": _dice_url(e),
                "start": start,
                "date": start.date(),
                "venue": (v.get("name") or "").strip(),
                "address": v.get("address") or "",
                "price": price,
                "sold_out": (e.get("status") or "").lower() == "sold-out",
                "artists": [
                    a.get("name") for a in
                    ((e.get("summary_lineup") or {}).get("top_artists") or [])
                    if a.get("name")
                ],
            })

    # A room can list the same show in more than one section.
    seen, unique = set(), []
    for e in sorted(events, key=lambda x: x["start"]):
        if e["url"] and e["url"] in seen:
            continue
        seen.add(e["url"])
        unique.append(e)
    return subject, unique, None


def fetch_dice_watchlist():
    """
    Pull the full upcoming run for every tracked venue and artist.

    Returns {"venues": {label: {...}}, "artists": {label: {...}}}. Each entry
    carries its events plus a state — "live", "quiet" (listed, nothing on
    sale), or "unlisted" (no readable page) — so the page can say which it is
    instead of showing a silent gap.
    """
    out = {"venues": {}, "artists": {}}

    for label, slug in DICE_TRACKED_VENUES.items():
        entry = {"slug": slug, "events": [], "address": "", "state": "unlisted",
                 "note": DICE_VENUE_NOTES.get(label, "")}
        if slug:
            subject, events, err = _fetch_dice_profile(f"https://dice.fm/venue/{slug}")
            entry["address"] = subject.get("address") or ""
            if err:
                entry["state"] = "unlisted"
                entry["note"] = entry["note"] or f"Couldn't read the DICE page ({err})."
            elif events:
                entry["state"], entry["events"] = "live", events
            else:
                entry["state"] = "quiet"
                entry["note"] = entry["note"] or "Nothing on sale right now."
            time.sleep(1.0)
        out["venues"][label] = entry

    for label, slug in DICE_TRACKED_ARTISTS.items():
        subject, events, err = _fetch_dice_profile(f"https://dice.fm/artist/{slug}")
        elsewhere = 0
        if not err and DICE_ARTIST_LOCAL_ONLY:
            local = [e for e in events if _is_new_york(e.get("address"))]
            elsewhere = len(events) - len(local)
            events = local
        if err:
            note = f"Couldn't read the DICE page ({err})."
        elif events:
            note = ""
        elif elsewhere:
            # Say why it's empty — "nothing" and "nothing *here*" are different
            # answers, and the second one is the one Alex asked for.
            note = (f"No New York dates — {elsewhere} on sale elsewhere."
                    if elsewhere > 1 else "No New York dates — one on sale elsewhere.")
        else:
            note = "No dates announced."
        out["artists"][label] = {
            "slug": slug,
            "events": [] if err else events,
            "state": "unlisted" if err else ("live" if events else "quiet"),
            "note": note,
            "elsewhere": elsewhere,
        }
        time.sleep(1.0)

    return out


def select_headline_events(watchlist, saturday, friday, limit=DICE_HEADLINE_COUNT):
    """
    Pick the events the email itself carries: `limit` in total across the two
    dates, not `limit` per date.

    Saturday is the night Alex is reading; the following Friday is the one he
    still has time to buy for. So Saturday gets first call on the slots, and
    Friday fills what's left — but Friday is guaranteed at least one slot when
    it has anything, so the look-ahead never vanishes on a busy Saturday.
    """
    def on(day):
        rows = []
        for label, entry in watchlist["venues"].items():
            for e in entry["events"]:
                if e["date"] == day:
                    rows.append({**e, "tracked_venue": label})
        # A tracked artist playing New York on one of these nights belongs in
        # the email even when the room isn't one we follow — that's the whole
        # point of following the artist.
        for label, entry in watchlist["artists"].items():
            for e in entry["events"]:
                if e["date"] == day:
                    rows.append({**e, "tracked_venue": e.get("venue") or label,
                                 "artist_watch": label})
        return sorted(rows, key=lambda r: (r["start"], r["tracked_venue"]))

    sat_rows, fri_rows = on(saturday), on(friday)
    fri_quota = min(len(fri_rows), 1) if sat_rows else min(len(fri_rows), limit)
    sat_take = sat_rows[:max(0, limit - fri_quota)]
    fri_take = fri_rows[:limit - len(sat_take)]
    return sat_take + fri_take


# ─────────────────────────────────────────────
# BROOKLYN CULTURE NEWS (RSS)
# ─────────────────────────────────────────────

# Slot 6: Bushwick Daily vs Bedford + Bowery — selected at runtime
SLOT_6_FEEDS = {
    "Bushwick Daily":   "https://bushwickdaily.com/feed/",
    "Bedford + Bowery": "https://bedfordandbowery.com/feed/",
}

# Feed priority tiers:
# Tier 1 — Brooklyn-focused
# Tier 2 — NYC-focused
# Tier 3 — National/international failsafe
NEWS_FEEDS = {
    # Tier 1: Brooklyn
    "Brooklyn Magazine": "https://brooklynmagazine.org/feed/",
    "The City":          "https://thecity.nyc/feed",
    "Streetsblog NYC":   "https://nyc.streetsblog.org/feed/",
    # Tier 2: NYC
    "Gothamist":         "https://gothamist.com/feed",
    "Hyperallergic":     "https://hyperallergic.com/feed/",
    "The Drift":         "https://thedriftmag.com/feed",
    # Tier 3: National failsafe
    "NPR":               "https://feeds.npr.org/1001/rss.xml",
}

# Paid/sponsored posts mixed into a feed. Edible Brooklyn publishes casino,
# pharmacy, and legal advertorials under /resources-guide/, and tags brand
# partnerships "Sponsored", alongside its real restaurant coverage.
SPONSORED_LINK_MARKERS = ["/resources-guide/"]
SPONSORED_TAGS = {"sponsored", "resources guide"}


def _is_sponsored(entry):
    """True for a feedparser entry that is an advertorial rather than editorial."""
    if any(m in entry.get("link", "") for m in SPONSORED_LINK_MARKERS):
        return True
    return any((t.get("term") or "").lower() in SPONSORED_TAGS for t in entry.get("tags", []))

# Keywords that strongly suggest Manhattan focus
MANHATTAN_KEYWORDS = [
    "manhattan", "midtown", "upper east side", "upper west side",
    "lower east side", "les ", "soho", "tribeca", "chelsea",
    "hell's kitchen", "harlem", "times square", "wall street",
    "financial district", "battery park", "east village", "west village",
    "greenwich village", "flatiron", "gramercy", "murray hill",
]

# Keywords that confirm Brooklyn focus
BROOKLYN_KEYWORDS = [
    "brooklyn", "bushwick", "williamsburg", "bedford", "fort greene",
    "bed-stuy", "bedford-stuyvesant", "flatbush", "park slope",
    "carroll gardens", "cobble hill", "red hook", "gowanus",
    "greenpoint", "sunset park", "bay ridge", "canarsie", "east new york",
    "brownsville", "prospect", "atlantic ave", "fulton", "nostrand",
    "kingston", "utica", "grand army",
]


def _parse_rss(url, source_name, max_items=8):
    """Fetch and parse an RSS or Atom feed. Follows redirects. Returns list of item dicts."""
    try:
        import feedparser as _fp
        feed = _fp.parse(url)
        # feedparser returns status 200/301/302 etc
        status = getattr(feed, 'status', 200)
        if status in (403, 404, 410):
            return []
        # Drop sponsored posts before slicing so they don't eat into max_items
        entries = [e for e in feed.entries if not _is_sponsored(e)][:max_items]
        items = []
        for entry in entries:
            title_text = entry.get("title", "")
            link_text  = entry.get("link", "")
            # Try summary then content for description
            desc_raw = entry.get("summary", "") or ""
            if not desc_raw and entry.get("content"):
                desc_raw = entry["content"][0].get("value", "")
            desc_text = BeautifulSoup(desc_raw, "html.parser").get_text(strip=True)[:300]
            date_text = entry.get("published", "")

            combined = f"{title_text} {desc_text}".lower()
            is_brooklyn = any(kw in combined for kw in BROOKLYN_KEYWORDS)
            is_manhattan = any(kw in combined for kw in MANHATTAN_KEYWORDS) and not is_brooklyn

            items.append({
                "source":      source_name,
                "title":       title_text,
                "url":         link_text,
                "desc":        desc_text,
                "date":        date_text,
                "is_brooklyn": is_brooklyn,
                "is_manhattan": is_manhattan,
            })
        return items
    except Exception:
        return []


# Edible Brooklyn gets its own subsection at the foot of Around Brooklyn,
# rendered straight from the feed (no Claude prose). Articles are ranked by
# where they are: Fort Greene first, then Bed-Stuy, then anywhere else in
# Brooklyn; newest first within each tier. Matching reads the full article
# body, since the neighborhood is often only named in the address line.
EDIBLE_FEED = "https://ediblebrooklyn.com/feed/"
EDIBLE_MAX_AGE_DAYS = 42   # it publishes a few stories a week; look back ~6 weeks
EDIBLE_COUNT = 3
EDIBLE_TIERS = [
    ("Fort Greene", ["fort greene"]),
    ("Bed-Stuy",      ["bed-stuy", "bed stuy", "bedford-stuyvesant", "bedford stuyvesant"]),
    ("Brooklyn",      ["brooklyn"] + BROOKLYN_KEYWORDS),
]


def fetch_edible_picks(today, limit=EDIBLE_COUNT):
    """
    Recent Edible Brooklyn stories about Brooklyn places, Fort Greene first,
    then Bed-Stuy, then the rest of Brooklyn. Sponsored posts and stories that
    name no Brooklyn place (recipes, travel) are dropped. Returns a list of
    {title, url, dek, area, date} dicts; empty if the feed is unreachable.
    """
    try:
        import feedparser as _fp
        feed = _fp.parse(EDIBLE_FEED)
    except Exception:
        return []
    cutoff = today - datetime.timedelta(days=EDIBLE_MAX_AGE_DAYS)
    picks = []
    for entry in feed.entries:
        if _is_sponsored(entry):
            continue
        link = entry.get("link", "")
        parsed = entry.get("published_parsed")
        if not parsed:
            continue
        date = datetime.date(*parsed[:3])
        if date < cutoff:
            continue
        summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
        body = " ".join(
            BeautifulSoup(c.get("value", ""), "html.parser").get_text(" ", strip=True)
            for c in entry.get("content") or []
        )
        text = f"{entry.get('title', '')} {summary} {body}".lower()
        # The site's own name would put every story in the Brooklyn tier
        text = text.replace("edible brooklyn", "")
        tier = next(
            (i for i, (_, kws) in enumerate(EDIBLE_TIERS) if any(k in text for k in kws)),
            None,
        )
        if tier is None:
            continue
        # WordPress appends "The post X appeared first on Edible Brooklyn."
        dek = re.sub(r"\s*The post .* appeared first on .*$", "", summary).strip()
        picks.append({
            "title": entry.get("title", ""),
            "url":   link,
            "dek":   dek,
            "area":  EDIBLE_TIERS[tier][0],
            "date":  date,
            "tier":  tier,
        })
    picks.sort(key=lambda p: (p["tier"], -p["date"].toordinal()))
    return picks[:limit]


# ─────────────────────────────────────────────
# FORT GREENE COMMUNITY NEWS
# ─────────────────────────────────────────────
# The Fort Greene Community Organization publishes its monthly paper on a
# Squarespace site (no RSS) as a PDF with a printed table of contents. The
# companion page is built from that contents list — headline, byline and page
# number, each linking to its page of the PDF — so none of it is paraphrased.
# The one model-written element is an optional one-sentence summary per story.
# It is generated once per issue and cached in chco-cache.json, which also
# means the 12 MB PDF is downloaded once a month rather than every Saturday.
#
# The site titles each post by the month its stories cover ("October 2026");
# the masthead inside the PDF carries the month it was published ("November
# 2026"). The page uses the masthead's month, and picks the issue by post date.
CHCO_ORIGIN = "https://www.chcobrooklyn.com"
CHCO_COLLECTION_URL = f"{CHCO_ORIGIN}/chco"
CHCO_POST_PREFIX = "Fort Greene Community News"
CHCO_MAX_AGE_DAYS = 150      # it posts about monthly but has gone 50+ days between issues; the link stays until a newer one
CHCO_CACHE_FILE = "chco-cache.json"
CHCO_BLURB_MODEL = "claude-haiku-5-5"
CHCO_UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter)"}


def _chco_pdf_pages(pdf_bytes):
    """
    Per-page text of the PDF. Poppler's pdftotext when it is installed (it keeps
    kerned headlines intact); pypdf otherwise.
    """
    import io
    import logging
    import subprocess
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(pdf_bytes)
            f.flush()
            out = subprocess.run(["pdftotext", "-layout", f.name, "-"],
                                 capture_output=True, check=True, timeout=120)
        pages = out.stdout.decode("utf-8", "replace").split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        if any(p.strip() for p in pages):
            return pages
    except (OSError, subprocess.SubprocessError):
        pass
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # one font-table warning per page otherwise
    return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf_bytes)).pages]


def parse_chco_toc(pages):
    """
    The issue's printed "In This Issue" list as [{title, author, page}]. Each
    line reads "Headline by Author ......... 12". Printed page numbers match the
    PDF's own page indexes, so they can be used as #page= links.
    """
    start = next((i for i, p in enumerate(pages) if "IN THIS ISSUE" in p.upper()), None)
    if start is None:
        return []
    rows = []
    for line in "\n".join(pages[start:start + 2]).splitlines():
        m = re.match(r"^\s*(.+?)\s*[_.…]{4,}\s*(\d{1,3})\s*$", line)
        if not m:
            continue
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        page = int(m.group(2))
        if not 1 <= page <= len(pages):
            continue
        title, author = text, ""
        # "Women Write workshop by Katya Borkov by John DeWind" — the byline is
        # the last "by". "Poetry edited by …" is a credit, not a byline.
        if " by " in text:
            head, tail = text.rsplit(" by ", 1)
            if not re.search(r"\bedited$", head, re.I):
                title, author = head.strip(), tail.strip()
        rows.append({"title": title, "author": author, "page": page, "blurb": ""})
    return rows if len(rows) >= 3 else []


def _chco_masthead(pages):
    """{volume, number, month} from the 'Vol.4 | No.10 | November 2026' line, or {}."""
    # pypdf renders kerned capitals with a gap ("V ol.4"), so allow one after the V
    m = re.search(r"V\s?ol\.?\s*(\d+)\s*\|?\s*No\.?\s*(\d+)\s*\|?\s*([A-Z][a-z]+\s+\d{4})",
                  " ".join(pages[:2]))
    if not m:
        return {}
    return {"volume": m.group(1), "number": m.group(2), "month": re.sub(r"\s+", " ", m.group(3))}


def _chco_summary_call(client, body, articles, indices):
    """One request for the stories at `indices`; returns {index: sentence}."""
    contents = "\n".join(
        f"{i + 1}. {articles[i]['title']}"
        + (f" (by {articles[i]['author']})" if articles[i]["author"] else "")
        + f" — page {articles[i]['page']}"
        for i in indices
    )
    prompt = f"""Below is the full text of one issue of Fort Greene Community News, a monthly neighborhood paper, with page markers, followed by a list of entries from its table of contents.

Write ONE plain sentence (at most 30 words) for EVERY entry in the list, saying what that story covers, using only what the story itself says. Neutral and factual, present tense. Do not add dates, times, addresses or prices unless the story states them. No opinion and no praise. For a poetry or creative-writing section, say what it is and whose work it contains, using names the issue gives. Only if you genuinely cannot find an entry in the text, use an empty string for it.

Return ONLY a JSON object whose keys are the entry numbers as strings and whose values are the sentences.

=== ENTRIES TO DESCRIBE ===
{contents}

=== ISSUE TEXT ===
{body}"""
    try:
        response = client.messages.create(
            model=CHCO_BLURB_MODEL,
            max_tokens=6000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"  CHCN summaries: stop_reason {response.stop_reason}; "
              f"{response.usage.input_tokens} in / {response.usage.output_tokens} out tokens")
        if response.stop_reason != "end_turn":
            return {}
        text = "".join(b.text for b in response.content if b.type == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception as e:
        print(f"  CHCN summaries failed: {type(e).__name__}: {e}")
        return {}
    out = {}
    for key, val in data.items():
        try:
            idx = int(key) - 1
        except (TypeError, ValueError):
            continue
        if idx not in indices or not isinstance(val, str):
            continue
        val = re.sub(r"\s+", " ", val).strip()
        if val and len(val.split()) <= 45:
            out[idx] = val
    return out


def summarize_chco_articles(client, pages, articles, only=None):
    """
    One plain sentence per story, from the story's own text: {index: sentence}.
    Every story is expected to get one, so anything the first request misses is
    asked for again, once. `only` limits it to those story indexes. Returns whatever it has on any failure, because the
    page reads fine without them. Event facts (dates, times, addresses) are only
    repeated if the story states them, the same rule as the rest of the dispatch.
    """
    body = "\n\n".join(
        f"=== PAGE {i} ===\n" + re.sub(r"[ \t]+", " ", p).strip()
        for i, p in enumerate(pages, 1) if p.strip()
    )
    if len(body) > 300_000:
        print("  CHCN: issue text too long to summarise; skipping summaries")
        return {}
    wanted = list(range(len(articles))) if only is None else sorted(only)
    out = {}
    for _attempt in (1, 2):
        todo = [i for i in wanted if i not in out]
        if not todo:
            break
        out.update(_chco_summary_call(client, body, articles, todo))
    missing = [articles[i]["title"] for i in wanted if i not in out]
    if missing:
        print(f"  CHCN: no summary for {len(missing)} of {len(wanted)} stories: {'; '.join(missing)}")
    return out

def _chco_cache_load():
    import pathlib
    try:
        return json.loads((pathlib.Path.cwd() / CHCO_CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _chco_cache_save(pdf_url, masthead, n_pages, articles, attempts=0, meta=None):
    import pathlib
    data = {"pdf_url": pdf_url, "masthead": masthead, "pages": n_pages,
            "summary_attempts": attempts, **(meta or {}), "articles": articles}
    (pathlib.Path.cwd() / CHCO_CACHE_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_chco_issue(today, client=None):
    """
    The newest Fort Greene Community News issue as a dict, or None when the
    site is unreachable, has nothing recent, or the PDF can't be read. Never
    raises: the page is an extra, and a failure here must not hold up the send.
    `client` (an Anthropic client) is only used for the optional summaries.
    """
    try:
        issue = _fetch_chco_issue(today, client)
    except Exception as e:
        print(f"  CHCN: live check failed ({type(e).__name__}: {e}); using the saved issue")
        issue = None
    # The link to the Community News page should not come and go with the site's
    # availability, so fall back to the issue saved on the last good run.
    return issue or _chco_issue_from_cache(today)


def _chco_issue_from_cache(today):
    """The issue saved by the last successful run, or None if there isn't a recent one."""
    c = _chco_cache_load()
    if not (c.get("articles") and c.get("posted") and c.get("pdf_url")):
        return None
    posted = datetime.date.fromisoformat(c["posted"])
    if (today - posted).days > CHCO_MAX_AGE_DAYS:
        return None
    m = c.get("masthead", {})
    return {"post_title": c.get("post_title", ""), "post_url": c.get("post_url", CHCO_COLLECTION_URL),
            "pdf_url": c["pdf_url"], "posted": posted, "volume": m.get("volume", ""),
            "number": m.get("number", ""), "month": m.get("month") or posted.strftime("%B %Y"),
            "pages": c.get("pages", 0), "articles": c["articles"]}


def _fetch_chco_issue(today, client):
    from urllib.parse import urljoin
    from zoneinfo import ZoneInfo

    # Squarespace serves any collection as JSON with ?format=json
    r = requests.get(CHCO_COLLECTION_URL, params={"format": "json"}, headers=CHCO_UA, timeout=30)
    r.raise_for_status()
    posts = [i for i in r.json().get("items", [])
             if (i.get("title") or "").startswith(CHCO_POST_PREFIX)]
    if not posts:
        print("  CHCN: no issue posts found")
        return None

    def stamp(post):
        return post.get("publishOn") or post.get("addedOn") or 0

    post = max(posts, key=stamp)
    posted = datetime.datetime.fromtimestamp(stamp(post) / 1000, ZoneInfo(TIMEZONE)).date()
    age = (today - posted).days
    if age > CHCO_MAX_AGE_DAYS:
        print(f"  CHCN: newest issue ({post['title']}) was posted {age} days ago; too old to show")
        return None
    found = re.search(r"""(?:https?://[^"'\s<>]+)?/s/[^"'\s<>]+\.pdf""", post.get("body") or "")
    if not found:
        print(f"  CHCN: no PDF on {post['title']}")
        return None
    pdf_url = urljoin(CHCO_ORIGIN, found.group(0))

    # The issue never changes once posted, so reuse what we parsed last time and
    # only fetch the PDF for a new issue, or to add summaries that are missing
    # (at most three tries per issue; CHCO_REGENERATE=1 redoes them all).
    cache = _chco_cache_load()
    cached = cache.get("pdf_url") == pdf_url and cache.get("articles")
    articles = cache["articles"] if cached else None
    masthead = cache.get("masthead", {}) if cached else {}
    n_pages = cache.get("pages", 0) if cached else 0
    attempts = cache.get("summary_attempts", 0) if cached else 0
    regenerate = client is not None and os.environ.get("CHCO_REGENERATE") == "1"
    want = [i for i, a in enumerate(articles or []) if regenerate or not a.get("blurb")]
    if not articles or (client is not None and want and (regenerate or attempts < 3)):
        try:
            pdf = requests.get(pdf_url, headers=CHCO_UA, timeout=120)
            pdf.raise_for_status()
            if not pdf.content.startswith(b"%PDF") or len(pdf.content) > 60_000_000:
                raise ValueError("unexpected file at the PDF link")
            pages = _chco_pdf_pages(pdf.content)
        except Exception:
            if not articles:
                raise
            print("  CHCN: couldn't re-read the PDF; using the cached contents")
        else:
            if not articles:
                articles = parse_chco_toc(pages)
                masthead = _chco_masthead(pages)
                n_pages = len(pages)
                want = list(range(len(articles)))
            if not articles:
                print("  CHCN: couldn't find a table of contents in the PDF")
                return None
            if client is not None and want:
                blurbs = summarize_chco_articles(client, pages, articles, want)
                for i in want:
                    articles[i]["blurb"] = blurbs.get(i, articles[i].get("blurb", ""))
                attempts += 1

    post_url = urljoin(CHCO_ORIGIN, post.get("fullUrl", "/chco"))
    if articles:   # identical bytes unless something changed, so git sees no diff on quiet weeks
        _chco_cache_save(pdf_url, masthead, n_pages, articles, attempts,
                         {"post_title": post["title"], "post_url": post_url, "posted": posted.isoformat()})
    month = masthead.get("month") or post["title"].split(" - ", 1)[-1]
    return {
        "post_title": post["title"],
        "post_url": post_url,
        "pdf_url": pdf_url,
        "posted": posted,
        "volume": masthead.get("volume", ""),
        "number": masthead.get("number", ""),
        "month": month,
        "pages": n_pages,
        "articles": articles,
    }


def _score_brooklyn_edginess(item):
    """
    Score an item for Brooklyn-edgy relevance when both slot-6 feeds
    are equally Brooklyn (or equally not). Higher = more relevant.
    """
    text = f"{item['title']} {item['desc']}".lower()
    edge_keywords = [
        "art", "music", "show", "gallery", "mural", "graffiti", "skate",
        "bike", "diy", "punk", "underground", "bar", "venue", "festival",
        "nightlife", "community", "protest", "neighborhood", "local",
        "housing", "gentrification", "landmark", "historic", "street",
    ]
    return sum(1 for kw in edge_keywords if kw in text)


def fetch_brooklyn_news():
    """
    Fetch items from all six feeds. For slot 6 (Bushwick Daily vs
    Bedford + Bowery), apply the selection logic:
      - Brooklyn-focused beats Manhattan-focused
      - If tied, pick the edgier/more-relevant one
      - If neither is Brooklyn-focused, pick the edgier one (surprise him)
    Returns a dict: {source_name: [items]} for the five confirmed slots,
    plus the winning slot-6 source.
    """
    results = {}

    # Fetch all tiered feeds
    for name, url in NEWS_FEEDS.items():
        items = _parse_rss(url, name)
        results[name] = items

    # Fetch both slot-6 candidates
    slot6_candidates = {}
    for name, url in SLOT_6_FEEDS.items():
        items = _parse_rss(url, name)
        slot6_candidates[name] = items

    # Selection logic for slot 6
    def brooklyn_score(items):
        return sum(1 for i in items if i["is_brooklyn"])

    def manhattan_score(items):
        return sum(1 for i in items if i["is_manhattan"])

    bd_items = slot6_candidates["Bushwick Daily"]
    bb_items = slot6_candidates["Bedford + Bowery"]

    bd_brooklyn = brooklyn_score(bd_items)
    bb_brooklyn = brooklyn_score(bb_items)
    bd_manhattan = manhattan_score(bd_items)
    bb_manhattan = manhattan_score(bb_items)

    # One is Brooklyn-focused, the other is not
    if bd_brooklyn > 0 and bb_manhattan > bb_brooklyn:
        winner = "Bushwick Daily"
    elif bb_brooklyn > 0 and bd_manhattan > bd_brooklyn:
        winner = "Bedford + Bowery"
    else:
        # Both Brooklyn or neither — pick edgier
        bd_edge = sum(_score_brooklyn_edginess(i) for i in bd_items[:5])
        bb_edge = sum(_score_brooklyn_edginess(i) for i in bb_items[:5])
        winner = "Bushwick Daily" if bd_edge >= bb_edge else "Bedford + Bowery"

    results[winner] = slot6_candidates[winner]
    results["_slot6_winner"] = winner

    return results


# ─────────────────────────────────────────────
# CLAUDE NARRATIVE LAYER
# ─────────────────────────────────────────────

def generate_narrative(
    saturday_data, sunday_data,
    sat_windows, sun_windows,
    market_result, market_list,
    brooklyn_news,
    client,
):
    # NOTE: The MTA, Public Records, and On Sale This Weekend sections are not
    # written by Claude. All three are rendered directly from structured data
    # (see fetch_mta_alerts / build_mta_section_html, fetch_public_records_events,
    # and fetch_dice_events), so this narrative covers the other three sections
    # only. Keeping event facts out of the model is deliberate: times, prices,
    # and venues should never be paraphrased.
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    sat_str = saturday.strftime("%B %d")
    sun_str = sunday.strftime("%B %d")

    # Format walk windows — include forced_early flag for Claude
    def fmt_windows(windows):
        lines = []
        for w in windows:
            line = f"  {w['zone']} ({w['hour_label']}): {w['status']} — {w['note']}"
            if w.get("forced_early"):
                line += " [RAIN FORCES EARLIER START]"
            lines.append(line)
        return "\n".join(lines)

    # Format market list
    market_names = [m.get("marketname", "Unknown") for m in market_list[:5]]

    # Guard market temps against non-numeric fallback
    _avg = market_result.get("avg_temp")
    _peak = market_result.get("peak_temp")
    market_avg_str = f"{_avg:.0f}°F" if isinstance(_avg, (int, float)) else "?"
    market_peak_str = f"{_peak:.0f}°F" if isinstance(_peak, (int, float)) else "?"

    # Format news feeds for prompt
    slot6_winner = brooklyn_news.get("_slot6_winner", "Bushwick Daily")

    def fmt_feed(name, items):
        if not items:
            return f"{name}: [no items fetched]"
        lines = [f"{name}:"]
        for item in items[:5]:
            brooklyn_tag = " [BROOKLYN]" if item["is_brooklyn"] else (" [MANHATTAN]" if item["is_manhattan"] else "")
            lines.append(f"  • {item['title']}{brooklyn_tag}")
            if item["desc"]:
                lines.append(f"    {item['desc'][:150]}")
            if item["url"]:
                lines.append(f"    URL: {item['url']}")
        return "\n".join(lines)

    news_sections = []
    # Priority order: Brooklyn → NYC → National failsafe → Slot 6
    feed_order = [
        # Tier 1: Brooklyn-focused
        "Brooklyn Magazine", "The City", "Streetsblog NYC", slot6_winner,
        # Tier 2: NYC-focused
        "Gothamist", "Hyperallergic", "The Drift",
        # Tier 3: National failsafe
        "NPR",
    ]
    for name in feed_order:
        if name in brooklyn_news and brooklyn_news[name]:
            news_sections.append(fmt_feed(name, brooklyn_news[name]))

    news_text = "\n\n".join(news_sections)

    prompt = f"""You are writing the Fort Greene Weekend Dispatch — a single, tightly-written Saturday morning email for Alex, a Fort Greene resident with a small black dog named Biscuit (~20 lbs).
He lives near {NEIGHBORHOOD} Park. The Sample Greenmarket is a 1-hour walk with {READER_DOG}.

Write in plain, direct prose. No markdown headers. No bullet lists. Five sections, each with a clear label line then 2–4 sentences of narrative. Be specific and useful, not generic.

=== WEATHER DATA ===
{"" if saturday_data else "NOTE: the forecast could not be fetched this run. Say so plainly in one sentence, and do not guess at temperatures, rain or walk windows." + chr(10) + chr(10)}SATURDAY ({sat_str}):
  High: {saturday_data.get('high_f', '?')}°F  Low: {saturday_data.get('low_f', '?')}°F
  Conditions: {WMO_CODES.get(saturday_data.get('weather_code', 0), 'Unknown')}
  Max rain chance: {saturday_data.get('rain_pct_max', '?')}%

SATURDAY BISCUIT WALK WINDOWS:
{fmt_windows(sat_windows)}

SUNDAY ({sun_str}):
  High: {sunday_data.get('high_f', '?')}°F  Low: {sunday_data.get('low_f', '?')}°F
  Conditions: {WMO_CODES.get(sunday_data.get('weather_code', 0), 'Unknown')}
  Max rain chance: {sunday_data.get('rain_pct_max', '?')}%

SUNDAY BISCUIT WALK WINDOWS:
{fmt_windows(sun_windows)}

=== FARMERS MARKET ===
Sample Greenmarket verdict: {market_result['verdict']}
Reason: {market_result['reason']}
Sustained average temp during outing window: {market_avg_str} (peak {market_peak_str})
Market hours: {market_result['market_hours']}
Other Brooklyn Saturday markets found: {', '.join(market_names)}

IN-SEASON PRODUCE AT SAMPLE GREENMARKET THIS MONTH:
{get_seasonal_produce(saturday)}

=== BROOKLYN CULTURE NEWS ===
Items from six feeds. Each item tagged [BROOKLYN] or [MANHATTAN] where detectable.
Slot 6 this week: {slot6_winner}

Alex's interest profile for filtering (same as above — literary, NYC history, urbanism,
sci-fi, investigative journalism, bikes/surf/street culture, food, left politics):
{INTEREST_PROFILE}

{news_text}

=== INSTRUCTIONS ===

Write the dispatch in three labeled sections (the MTA and Public Records sections are generated separately — do NOT write them):

WEATHER + BISCUIT
2–3 sentences covering both days. Alex's default is leaving home between 10am and noon on weekends — he is not getting up at 6am unless forced. Work from that assumption. If the walk window data shows [RAIN FORCES EARLIER START], acknowledge that plainly and tell him rain is arriving mid-morning so he'll need to move if he wants to get Biscuit out dry. If the 10am–noon window is just hot (not rainy), tell him what to expect at that hour and how to manage it with Biscuit. Always include the evening window as a second option. Be specific about Saturday vs Sunday if they differ.

SAMPLE GREENMARKET
2–3 sentences. Lead with the verdict. Then name 2–3 specific produce picks that are peak right now and worth grabbing — be concrete and specific (e.g., "the Sungold tomatoes and garlic scapes" not just "tomatoes and garlic"). If no-go or caution, still name the best picks so he knows what to grab on a better week or a solo trip, then suggest one concrete indoor Brooklyn fallback.

AROUND BROOKLYN
Surface 4–6 items across the feeds, grouped by source. Priority order:
- Tier 1 (Brooklyn-focused): Brooklyn Magazine, The City, Streetsblog NYC, {slot6_winner}
- Tier 2 (NYC-focused): Gothamist, Hyperallergic, The Drift
- Tier 3 (National failsafe): NPR — only use if nothing relevant in Tiers 1 and 2

Prioritize Tier 1 sources. Only move to Tier 2 if Tier 1 has fewer than 2 relevant items. Only use NPR if both Tier 1 and Tier 2 are sparse.

CRITICAL FORMATTING RULE: Group all items from the same source together. Use this exact format:

[SOURCE NAME]
<a href="URL">Headline</a> — One sentence on why it's worth his attention.
<a href="URL">Headline</a> — One sentence on why it's worth his attention.

[SOURCE NAME]
<a href="URL">Headline</a> — One sentence on why it's worth his attention.

Write in the voice of The New Yorker's Goings On — terse, specific, a little editorial. Keep the [SOURCE NAME] tags exactly as shown so the email template can parse and style them. Only include sources that actually have relevant items.

Write it as one cohesive email. Don't start with "Hello" or "Hi Alex." Just start with the first section label."""

    # thinking is on by default for claude-sonnet-5 and spends from max_tokens;
    # disable it so the whole budget goes to the newsletter text.
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}]
    )
    print(f"  API stop_reason: {response.stop_reason}; "
          f"output_tokens: {response.usage.output_tokens}; "
          f"content blocks: {[b.type for b in response.content]}")
    if response.stop_reason == "max_tokens":
        print("  WARNING: output was truncated at max_tokens")
    text = "".join(b.text for b in response.content if b.type == "text")
    if not text.strip():
        raise ValueError("No text in API response")
    return text


# ─────────────────────────────────────────────
# EMAIL ASSEMBLY
# ─────────────────────────────────────────────

def _extract_recommended_lines(text):
    """
    Pull a recommended line out of alert text like 'Take the [A] instead' or
    'use the [A]'. MTA brackets routes, e.g. [A]. Returns the recommended
    A/C/3 line letters in mention order (deduped); other lines are ignored
    because the alternate lookup only covers A/C/3.
    """
    import re
    recs = []
    low = text.lower()
    for m in re.finditer(r'(?:take|use|board|catch|via|transfer to)\s+the\s+\[([a-z0-9])\]', low):
        recs.append(m.group(1).upper())
    for m in re.finditer(r'\[([a-z0-9])\]\s+instead', low):
        recs.append(m.group(1).upper())
    out = []
    for r in recs:
        if r in HOME_LINES and r not in out:
            out.append(r)
    return out


def _recommend_alternate(station, statuses, disrupted_lines, affected_stops):
    """
    Pick an alternate station for an affected home station, in priority order:
      (a) the OTHER home station, if it isn't affected this weekend;
      (b) a line the alert explicitly recommends ('take the [A]') -> nearest
          ALTERNATE_STATIONS entry serving that (still-running) line;
      (c) otherwise the nearest other station on a running A/C/3 service;
      (d) if everything nearby is disrupted, say so.
    Returns a plain-English sentence. `disrupted_lines` are lines with a
    weekend-active whole-line change; `affected_stops` are directly-named stops.
    """
    home_id = station["stop_id"]

    def line_running(line):
        return line not in disrupted_lines

    def usable(entry):
        if entry["stop_id"] in affected_stops:
            return False
        return any(line_running(l) for l in entry["lines"])

    # (a) the other home station, if it's clear
    for other in statuses:
        if other["stop_id"] != home_id and not other["affected"]:
            return (f'Take your other home station, {other["name"]} '
                    f'([{other["line"]}]), instead — it\'s unaffected this weekend.')

    # (b) a line the alert points you to -> nearest station serving it
    rec_lines = []
    for a in station["alerts"]:
        rec_lines += _extract_recommended_lines(a["text"])
    for rl in rec_lines:
        if not line_running(rl):
            continue
        for entry in ALTERNATE_STATIONS:
            if entry["stop_id"] == home_id or entry["stop_id"] in affected_stops:
                continue
            if rl in entry["lines"]:
                return (f'The alert points you to the [{rl}] — nearest is '
                        f'{entry["name"]} ({entry["walk"]}).')

    # (c) nearest other station on a running A/C/3 service
    for entry in ALTERNATE_STATIONS:
        if entry["stop_id"] == home_id:
            continue
        if usable(entry):
            running = "/".join(l for l in entry["lines"] if line_running(l))
            return (f'Nearest running alternative: {entry["name"]} '
                    f'([{running}], {entry["walk"]}).')

    # (d) nothing nearby is running
    return ("Every nearby A/C/3 station is disrupted this weekend — "
            "check the MTA app before you head out.")


def build_home_station_banner(alerts):
    """
    Build the home-station status banner from the weekend-active A/C/3 alerts,
    considering EXACTLY the two BANNER_STATIONS.

    A station is "touched" if an alert names its stop directly, or a whole-line
    (line-level) change hits its line. All-clear renders one calm line in the
    section's prose style; if one/both are affected, each gets a line naming
    what's happening plus an alternate recommendation (see _recommend_alternate).
    Returns an HTML string.
    """
    import html as _html

    # Banner only fires for disruptions — extra service additions are not disruptions.
    disruption_alerts = [a for a in alerts if not _is_extra_service(a)]

    affected_stops = set()
    disrupted_lines = set()          # lines with a whole-line (line-level) change
    for a in disruption_alerts:
        affected_stops.update(a["stop_ids"])
        if a["is_line_level"]:
            disrupted_lines.update(a["routes"])

    # Evaluate the two banner stations
    statuses = []
    for st in BANNER_STATIONS:
        relevant = [
            a for a in disruption_alerts
            if st["stop_id"] in a["stop_ids"]
            or (a["is_line_level"] and st["line"] in a["routes"])
        ]
        affected = (st["stop_id"] in affected_stops) or (st["line"] in disrupted_lines)
        statuses.append({
            "stop_id": st["stop_id"], "name": st["name"], "line": st["line"],
            "affected": affected, "alerts": relevant,
        })

    if not any(s["affected"] for s in statuses):
        return ('<p class="mta-banner mta-banner--clear">All home stations are '
                'unaffected in either direction this weekend.</p>')

    paras = []
    for s in statuses:
        if not s["affected"]:
            continue
        whats = s["alerts"][0]["text"] if s["alerts"] else "a service change is in effect"
        rec = _recommend_alternate(s, statuses, disrupted_lines, affected_stops)
        paras.append(
            f'<p class="mta-banner mta-banner--alert">'
            f'<strong>{_html.escape(s["name"])} ([{s["line"]}])</strong>: '
            f'{_html.escape(whats)}. {_html.escape(rec)}</p>'
        )
    return "".join(paras)


def build_mta_section_html(alerts, surface_alerts):
    """
    Render the MTA section body: a home-station status banner on top, then the
    weekend-active changes grouped under the A, C and 3 (plain wording, with
    when each applies), reusing the .news-item cards.
    Surface-transit disruptions, when present, follow underneath (unchanged
    'only if disrupted' rule).
    """
    import html as _html

    html = build_home_station_banner(alerts)

    # Drop system-wide extra service blasts (e.g. "Additional [2][4][6][A][C][F] will operate")
    # but keep line-specific extra service notices (e.g. "Additional [C] service between ...").
    # A notice is system-wide if it references routes outside A, C, and 3.
    import re as _re
    def _is_system_wide(a):
        if not _is_extra_service(a):
            return False
        mentioned = set(_re.findall(r'\[([A-Z0-9]+)\]', a.get("text", "")))
        return bool(mentioned - {"A", "C", "3"})
    change_alerts = [a for a in alerts if not _is_system_wide(a)]
    # One heading per home line (A, C, 3), each change in plain wording with the
    # days and hours it applies; a line with nothing planned says so.
    html += f"""
          <div class="mta-groups">{mw.render_home_lines(change_alerts)}</div>"""

    # Surface transit — conditional, only when a service is actually disrupted.
    if surface_alerts:
        s_cards = ""
        for a in surface_alerts:
            s_cards += f"""
            <div class="news-item">
              <div class="news-source">{_html.escape(a.get("service", ""))}</div>
              <div class="news-hed">{_html.escape(a.get("text", ""))}</div>
            </div>"""
        html += f"""
          <div class="mta-group">
            <div class="mta-group-label">Buses · AirTrain · LIRR</div>
            <div class="news-feed">{s_cards}</div>
          </div>"""

    return html


# Pigeon artwork lives in assets/ and is served from GitHub Pages so both the
# emailed envelope and the hosted newsletter can load it by absolute URL.
#   pigeon-grey.png — plain silhouette, tiled faintly behind the newsletter
#   pigeon-grey.png   — blue-jacket "ZT" pigeon, used on the envelope stamp
ASSET_BASE_URL = os.environ.get(
    "WEEKEND_ASSET_BASE_URL",
    "https://your-username.github.io/weekend-dispatch/assets",
).rstrip("/")
PIGEON_GREY_URL = f"{ASSET_BASE_URL}/pigeon-grey.png"
PIGEON_ZT_URL = f"{ASSET_BASE_URL}/pigeon-grey.png"
ZT_CREST_URL = f"{ASSET_BASE_URL}/crest-placeholder.png"   # gold crest, transparent, for the newsletter footer


# ─────────────────────────────────────────────
# NATURE SECTIONS — Bird / Tree / Map of the Week, Then & Now
# ─────────────────────────────────────────────
# Species and place write-ups are a hand-vetted bank (no LLM text), rotated by
# ISO week. Birds are filtered to months they're actually in NYC; PNW species
# (tagged region="pnw") are always eligible. Photos and the street-tree map are
# fetched live; any fetch failure drops that piece rather than guessing.

NATURE_BIRDS = [
    {
        "name": "Black-throated Blue Warbler", "audio_file": "Setophaga caerulescens - Black-throated Blue Warbler XC311813.mp3",
        "audio_note": "Song of an adult male, a lazy, buzzy &ldquo;zoo-zoo-zeee&rdquo; that rises at the end",
        "latin": "Setophaga caerulescens",
        "region": "ne", "months": [5, 9, 10],
        "tagline": "A migrant warbler that passes through Prospect Park each spring and fall",
        "photo_file": "Black-throated blue warblers (7140435321).jpg", "photo_note": "Adult male",
        "facts": [
            ("Where", "The Ravine and Midwood edges in Prospect Park. Look low, in the shrub layer, not up in the canopy."),
            ("ID · male", "Slate-blue back, black face and throat, clean white belly, and a small white square at the base of the wing: the &ldquo;pocket handkerchief.&rdquo;"),
            ("ID · female", "Plain olive-brown with a pale eyebrow. The same small pale wing spot is the one mark that gives her away."),
            ("Range", "Breeds in northern hardwood and Appalachian forests with a dense understory; winters in the Greater Antilles."),
        ],
        "conservation": "A shrub-nester, so it lives or dies by the understory, which is exactly the layer deer overbrowsing strips out of eastern forests. In the city the bigger threat is glass: NYC&rsquo;s Local Law 15 of 2020 requires bird-friendly glazing on new buildings.",
    },
    {
        "name": "Red-tailed Hawk", "audio_file": "Buteo jamaicensis - Red-tailed Hawk XC71575.mp3",
        "audio_note": "Call: the hoarse scream",
        "latin": "Buteo jamaicensis",
        "region": "ne", "months": list(range(1, 13)),
        "tagline": "New York&rsquo;s resident big hawk, nesting on ledges and park trees year-round",
        "wiki": "Red-tailed hawk",
        "facts": [
            ("Where", "Perched on light poles, building ledges and tall park trees; watch over Prospect Park&rsquo;s Long Meadow."),
            ("ID", "Big and broad-winged. Adults show a brick-red upper tail. From below, look for a dark &ldquo;belly band&rdquo; and a dark bar on the leading edge of each wing."),
            ("Voice", "The hoarse, descending scream that movies dub onto every eagle."),
            ("Range", "Nearly all of North America; in NYC they stay all year."),
        ],
        "conservation": "City hawks eat rats, which makes rat poison their biggest urban threat: anticoagulant rodenticides pass up the food chain and cause secondary poisoning.",
    },
    {
        "name": "White-throated Sparrow", "audio_file": "Zonotrichia albicollis - White-throated Sparrow XC569523.mp3",
        "audio_note": "Song of an adult male",
        "latin": "Zonotrichia albicollis",
        "region": "ne", "months": [1, 2, 3, 4, 10, 11, 12],
        "tagline": "The winter sparrow scratching through leaf litter under every park shrub",
        "wiki": "White-throated sparrow",
        "facts": [
            ("Where", "On the ground under shrubs and along path edges in any park, often in loose flocks."),
            ("ID", "A crisp white throat patch, a yellow spot between the eye and bill, and a striped crown. The stripes come in two forms, white-striped and tan-striped."),
            ("Voice", "A thin whistled song, often written &ldquo;Oh sweet Canada, Canada, Canada.&rdquo;"),
            ("Range", "Breeds in Canada&rsquo;s boreal forest; winters across the eastern U.S., including NYC."),
        ],
        "conservation": "Still common, but a frequent victim of window collisions during migration. It is also a genetics case study: the two stripe forms almost always pair with the opposite form.",
    },
    {
        "name": "Cedar Waxwing", "audio_file": "Bombycilla cedrorum - Cedar Waxwing XC107623.mp3",
        "audio_note": "Thin, high calls from a small flock",
        "latin": "Bombycilla cedrorum",
        "region": "ne", "months": list(range(1, 13)),
        "tagline": "A sleek, crested fruit-eater that travels in flocks from berry tree to berry tree",
        "wiki": "Cedar waxwing",
        "facts": [
            ("Where", "Anywhere with fruit: crabapples, hollies and junipers in parks and on streets."),
            ("ID", "Silky tan-grey with a crest, a black mask, a bright yellow tail tip, and small waxy red tips on the wing feathers."),
            ("Voice", "Very high, thin whistles, often the first clue a flock is overhead."),
            ("Range", "Across most of North America; in NYC they can turn up in any month."),
        ],
        "conservation": "A living indicator of what we plant: birds that eat non-native honeysuckle berries while growing tail feathers can have orange tail tips instead of yellow.",
    },
    {
        "name": "Chimney Swift", "audio_file": "Chaetura pelagica - Chimney Swift XC105129.mp3",
        "audio_note": "Chittering calls at dusk",
        "latin": "Chaetura pelagica",
        "region": "ne", "months": [5, 6, 7, 8, 9],
        "tagline": "A &ldquo;cigar with wings&rdquo; chattering over Brooklyn rooftops all summer",
        "wiki": "Chimney swift",
        "facts": [
            ("Where", "Overhead, never perched. Listen for chittering above rooftops at dusk."),
            ("ID", "Dark, stubby body with long, stiff, curved wings and rapid, fluttery wingbeats."),
            ("Habits", "Feeds entirely on the wing and roosts clinging inside chimneys, sometimes by the hundreds in late summer."),
            ("Range", "Breeds across eastern North America; winters in South America&rsquo;s Amazon basin."),
        ],
        "conservation": "Declining sharply. Capped and lined chimneys have removed much of its nesting habitat, and falling insect populations cut its food supply.",
    },
    {
        "name": "American Woodcock", "audio_file": "Scolopax minor - American Woodcock XC78342.mp3",
        "audio_note": "The nasal &ldquo;peent&rdquo; a male gives on his display ground",
        "latin": "Scolopax minor",
        "region": "ne", "months": [3, 4, 10, 11],
        "tagline": "A shorebird that lives in the woods, and a regular migrant through city parks",
        "wiki": "American woodcock",
        "facts": [
            ("Where", "On the ground in damp, leafy corners of parks, often frozen still and near-invisible."),
            ("ID", "Plump and leaf-brown with a very long bill and eyes set far back on the head, so it can watch for predators while probing for worms."),
            ("Display", "In early spring, males perform a spiralling &ldquo;sky dance&rdquo; at dusk, with twittering wings."),
            ("Range", "Eastern North America; migrates through NYC in spring and fall."),
        ],
        "conservation": "Migrating at night and low, woodcocks are among the birds most often found stunned or killed by building collisions in Manhattan. NYC Audubon&rsquo;s Project Safe Flight tracks them.",
    },
    {
        "name": "Northern Flicker", "audio_file": "Colaptes auratus - Northern Flicker XC104537.mp3",
        "audio_note": "Calls, including the rattle and the &ldquo;flick-a&rdquo; call",
        "latin": "Colaptes auratus",
        "region": "ne", "months": list(range(1, 13)),
        "tagline": "A woodpecker that feeds on the ground, flashing a white rump as it flies",
        "wiki": "Northern flicker",
        "facts": [
            ("Where", "On lawns and meadows, probing for ants; peaks in NYC during fall migration."),
            ("ID", "Brown with black barring, a black bib, a spotted belly, and a bright white rump in flight."),
            ("East vs west", "Eastern birds flash yellow under the wings; western birds, like the ones around UBC, flash red."),
            ("Range", "Across North America; the two colour forms meet and hybridize on the Great Plains."),
        ],
        "conservation": "Still widespread but slowly declining. It needs dead trees (snags) to nest in, and those are the first thing removed from tidy parks and yards.",
    },
    {
        "name": "Dark-eyed Junco", "audio_file": "Junco hyemalis - Dark-eyed Junco XC569535.mp3",
        "audio_note": "Song: a musical trill on one pitch",
        "latin": "Junco hyemalis",
        "region": "ne", "months": [1, 2, 3, 11, 12],
        "tagline": "The &ldquo;snowbird&rdquo; that arrives in NYC just as the weather turns",
        "wiki": "Dark-eyed junco",
        "facts": [
            ("Where", "Hopping on the ground at park edges and under feeders, often with sparrows."),
            ("ID", "Eastern birds are slate-grey above with a white belly and a pale pink bill. White outer tail feathers flash when it flies."),
            ("East vs west", "Vancouver&rsquo;s juncos are the &ldquo;Oregon&rdquo; form: a black hood over a brown back. Same species."),
            ("Range", "Breeds in northern and mountain forests; winters across most of the U.S."),
        ],
        "conservation": "Abundant, and a textbook example of how quickly birds diversify: the junco&rsquo;s colour forms evolved in the few thousand years since the last ice age.",
    },
    {
        "name": "Varied Thrush", "latin": "Ixoreus naevius",
        "region": "pnw", "months": list(range(1, 13)),
        "tagline": "The Pacific Northwest&rsquo;s signature rainforest thrush",
        "wiki": "Varied thrush",
        "facts": [
            ("Where", "Damp, mossy conifer forest. Pacific Spirit Regional Park next to UBC is classic habitat, especially in winter."),
            ("ID", "Robin-sized, with orange throat, eyebrow and wing bars, and a dark band across the breast."),
            ("Voice", "A single long, buzzy, eerie whistle on one pitch, then another on a different pitch."),
            ("Range", "Coastal forests from Alaska to northern California. In the Northeast it is a rare winter stray."),
        ],
        "conservation": "Tied to old, closed-canopy conifer forest, so it declines where that forest is fragmented, and it is a frequent window-strike victim in winter.",
    },
    {
        "name": "Steller&rsquo;s Jay", "audio_file": "Cyanocitta stelleri - Steller's Jay XC110261.mp3",
        "audio_note": "A mix of calls from a small foraging flock",
        "latin": "Cyanocitta stelleri",
        "region": "pnw", "months": list(range(1, 13)),
        "tagline": "British Columbia&rsquo;s provincial bird",
        "wiki": "Steller's jay",
        "facts": [
            ("Where", "Conifer forests and leafy neighbourhoods across coastal B.C., including the UBC campus."),
            ("ID", "A charcoal-black head and tall crest over deep blue wings and tail."),
            ("Voice", "Harsh &ldquo;shack-shack-shack&rdquo; calls, and a convincing Red-tailed Hawk impression."),
            ("Range", "Western North America, from Alaska to Central America; the western counterpart of the Blue Jay."),
        ],
        "conservation": "Secure and adaptable. Like other jays it caches seeds, which helps conifers regenerate.",
    },
]

NATURE_TREES = [
    {
        "name": "American Elm", "latin": "Ulmus americana", "region": "ne",
        "tagline": "Native across eastern North America, with New York squarely in its range",
        "photo_file": "American Elm Tree, Old South Street, Northampton, MA - October 2019.jpg",
        "photo_note": "&ldquo;Ed Cotton&rsquo;s elm,&rdquo; Northampton, Mass., October 2019",
        "facts": [
            ("Where", "Look for it along older boulevards and at the edges of parks."),
            ("Shape", "The classic <em>vase</em>: the trunk splits low into several upright limbs that arch outward into a wide, drooping crown."),
            ("Leaf", "Doubly toothed with a lopsided base: one side of the blade starts lower on the stalk than the other. Yellow in fall."),
            ("Bark trick", "Cut a small chip of outer bark and look at it in cross-section. Alternating light and dark layers means American elm; slippery elm is solid brown."),
        ],
        "conservation": "Dutch elm disease, a fungus carried by elm bark beetles, reached the U.S. around 1930 and killed tens of millions of American elms, emptying the elm-arched streets of the Northeast. Replacements are now usually disease-tolerant selections such as &lsquo;Princeton&rsquo; and &lsquo;Valley Forge.&rsquo;",
    },
    {
        "name": "Pin Oak", "latin": "Quercus palustris", "region": "ne",
        "tagline": "A native bottomland oak that became a Brooklyn street-tree staple",
        "wiki": "Quercus palustris",
        "facts": [
            ("Where", "Street trees across Fort Greene, Prospect Heights and Park Slope."),
            ("Shape", "A tidy pyramid: lower branches droop down, middle branches run level, upper branches angle up."),
            ("Leaf", "Deeply cut, with bristle-tipped lobes. Turns russet to red and often holds its dead leaves into winter."),
            ("Acorn", "Small and round, with a thin, saucer-shaped cap."),
        ],
        "conservation": "A native wetland species that tolerates compacted, waterlogged urban soil. In alkaline soils it cannot take up iron and its leaves yellow (iron chlorosis), a common sign of stress in street plantings.",
    },
    {
        "name": "Tulip Tree", "latin": "Liriodendron tulipifera", "region": "ne",
        "tagline": "The tallest hardwood in eastern North America",
        "wiki": "Liriodendron tulipifera",
        "facts": [
            ("Where", "Prospect Park&rsquo;s woodlands; the Queens Giant in Alley Pond Park, often called the oldest living thing in NYC, is one."),
            ("Leaf", "Unmistakable: four lobes with a squared-off, notched tip, like a cat&rsquo;s face or a T-shirt. Clear yellow in fall."),
            ("Flower", "Tulip-shaped, green and orange, in late spring, often too high in the crown to notice."),
            ("Trunk", "Ramrod-straight and branch-free for a long way up, which made it a prized timber tree."),
        ],
        "conservation": "Fast-growing and secure. It is a magnolia, not a poplar, despite the old name &ldquo;yellow poplar.&rdquo;",
    },
    {
        "name": "American Sycamore", "latin": "Platanus occidentalis", "region": "ne",
        "tagline": "One parent of the London planetree that lines half of New York&rsquo;s streets",
        "wiki": "Platanus occidentalis",
        "facts": [
            ("Where", "Along streams and in parks; its hybrid offspring, the London planetree, is on nearly every block."),
            ("Bark", "Peels in patches, leaving the upper trunk and limbs strikingly white."),
            ("Fruit", "Bristly seed balls that hang one per stalk. A London planetree usually hangs two or more per stalk."),
            ("Leaf", "Broad and maple-like, but alternate on the twig rather than opposite."),
        ],
        "conservation": "Native and common. The London planetree is its cross with the Oriental plane, and it is the official symbol of NYC Parks.",
    },
    {
        "name": "Eastern White Pine", "latin": "Pinus strobus", "region": "ne",
        "tagline": "The tallest tree of the Northeast&rsquo;s original forests",
        "wiki": "Pinus strobus",
        "facts": [
            ("Where", "Park plantings and yards; Prospect Park and the Botanic Garden both have them."),
            ("Needles", "Soft, long and in bundles of five. The memory trick is one needle per letter of W-H-I-T-E."),
            ("Shape", "Mature trees carry level, layered branches with a feathery, wind-swept look."),
            ("Cones", "Long, slender and slightly curved, often sticky with resin."),
        ],
        "conservation": "Colonial Britain reserved the tallest white pines as ship masts by marking them with the King&rsquo;s Broad Arrow, and resentment of that policy fed the American Revolution.",
    },
    {
        "name": "Black Cherry", "latin": "Prunus serotina", "region": "ne",
        "tagline": "A native tree that feeds more birds and caterpillars than almost anything else",
        "wiki": "Prunus serotina",
        "facts": [
            ("Where", "Park edges, vacant lots and woodland margins; it seeds in wherever birds perch."),
            ("Bark", "Dark and scaly on older trees, often described as &ldquo;burnt potato chips.&rdquo;"),
            ("Leaf", "Narrow and glossy with fine teeth, and a fuzzy line of hairs along the midrib underneath."),
            ("Fruit", "Small dark cherries in late summer, stripped quickly by birds."),
        ],
        "conservation": "A keystone native: it hosts hundreds of species of moth and butterfly caterpillars, which in turn feed nesting songbirds.",
    },
    {
        "name": "Western Redcedar", "latin": "Thuja plicata", "region": "pnw",
        "tagline": "British Columbia&rsquo;s provincial tree",
        "wiki": "Thuja plicata",
        "facts": [
            ("Where", "Moist lowland forest across coastal B.C., including Pacific Spirit Regional Park by UBC."),
            ("Foliage", "Flat sprays of tiny scale-leaves. Underneath, the white markings form a small &ldquo;bowtie&rdquo; pattern."),
            ("Bark", "Reddish, fibrous and stringy, peeling in long vertical strips."),
            ("Range", "Pacific coast from southeast Alaska to northern California, plus the interior wet belt."),
        ],
        "conservation": "Called the &ldquo;tree of life&rdquo; by many coastal First Nations for its uses in canoes, houses, clothing and baskets. Old-growth cedar is at the centre of B.C.&rsquo;s old-growth logging debates.",
    },
    {
        "name": "Douglas-fir", "latin": "Pseudotsuga menziesii", "region": "pnw",
        "tagline": "The Pacific Northwest&rsquo;s backbone timber tree, and not a true fir",
        "wiki": "Pseudotsuga menziesii",
        "facts": [
            ("Where", "Across coastal B.C. and Washington; the big trees in Stanley Park and Pacific Spirit Park."),
            ("Cones", "The giveaway: three-pointed bracts stick out between the scales, like the back legs and tail of a mouse diving in."),
            ("Needles", "Soft and flat, radiating all around the twig."),
            ("Bark", "Very thick and deeply furrowed on old trees, which lets them survive fires."),
        ],
        "conservation": "Its genus name means &ldquo;false hemlock.&rdquo; Coastal old-growth Douglas-fir is now a small fraction of its original extent.",
    },
    {
        "name": "Bigleaf Maple", "latin": "Acer macrophyllum", "region": "pnw",
        "tagline": "The maple with the biggest leaves in the world, and a garden of moss on every limb",
        "wiki": "Acer macrophyllum",
        "facts": [
            ("Where", "Moist valley bottoms and ravines across coastal B.C. and the Pacific Northwest."),
            ("Leaf", "Deeply five-lobed and often wider than a dinner plate."),
            ("Epiphytes", "Trunks and limbs are carpeted in moss and licorice fern, a canopy garden that can outweigh the tree&rsquo;s own leaves."),
            ("Seeds", "Paired, hairy samaras held at a sharp angle."),
        ],
        "conservation": "It enriches forest soils and streams; its leaves break down quickly and feed the food webs of salmon streams.",
    },
    {
        "name": "Sitka Spruce", "latin": "Picea sitchensis", "region": "pnw",
        "tagline": "The giant of the Pacific fog belt, and the largest spruce in the world",
        "wiki": "Picea sitchensis",
        "facts": [
            ("Where", "A narrow coastal strip from Alaska to northern California, rarely far from salt spray and fog. On Vancouver Island, the Carmanah Valley holds some of the biggest."),
            ("Needles", "Stiff, flattened and sharp enough to hurt when you grab a branch, with whitish bands underneath."),
            ("Cones", "Light and papery, with thin, wavy-edged scales."),
            ("Bark", "Thin and grey-brown, flaking off in small, round scales."),
        ],
        "conservation": "Its wood is light and strong for its weight, which made it the favoured timber for early aircraft frames and still makes it the classic soundboard for guitars and pianos. Old-growth coastal stands like Carmanah were protected only after long campaigns against logging.",
    },
]

THEN_AND_NOW = [
    {
        "hed": "You live on a glacier&rsquo;s leftovers",
        "then": "Fort Greene sits on the <strong>Harbor Hill terminal moraine</strong>, the ridge of rock and gravel the last ice sheet dumped when it stopped here roughly 20,000 years ago. Eastern Parkway rides its crest. South of it lies Flatbush&rsquo;s flat outwash plain. This was Lenape land. In the 1800s the ridge was called <strong>Crow Hill</strong>. At its eastern edge, <strong>Weeksville</strong> (founded 1838) became one of the country&rsquo;s largest free Black communities.",
        "now": "The moraine still sets the terrain: the Brooklyn Museum&rsquo;s Mount Prospect and the ridge in Prospect Park are the same landform. Its passes decided the Battle of Brooklyn in 1776, when the British flanked the American line through Jamaica Pass. Almost nothing of the moraine&rsquo;s original oak forest survives except fragments like the Park&rsquo;s Ravine.",
    },
    {
        "hed": "Gowanus was a creek before it was a canal",
        "then": "<strong>Gowanus Creek</strong> was a tidal inlet winding through salt marsh, known for its oysters. In the Battle of Brooklyn in 1776, the Maryland troops&rsquo; stand near the Old Stone House held off the British long enough for American soldiers to retreat across the creek&rsquo;s marshes.",
        "now": "The creek was dredged and bulkheaded into the <strong>Gowanus Canal</strong> in the mid-1800s and ringed with industry. It was designated an EPA <strong>Superfund</strong> site in 2010. Cleanup means dredging contaminated sediment and building tanks to capture combined sewer overflows.",
    },
    {
        "hed": "Wallabout Bay and the prison ships",
        "then": "The bay took its name from the <strong>Walloon</strong> settlers who farmed its shore in the 1600s. During the Revolution, the British moored <strong>prison ships</strong> there, and more than 11,000 American captives died aboard them.",
        "now": "The shoreline became the <strong>Brooklyn Navy Yard</strong> in 1801, which built the USS <em>Missouri</em>, where Japan&rsquo;s surrender was signed in 1945. It closed as a Navy yard in 1966 and is now an industrial park. The prisoners are remembered by the Prison Ship Martyrs&rsquo; Monument in Fort Greene Park.",
    },
    {
        "hed": "Coney Island used to be an island",
        "then": "<strong>Coney Island</strong> was a barrier island of dunes and marsh, cut off from the mainland by <strong>Coney Island Creek</strong>. The name is usually traced to the Dutch for &ldquo;rabbit island.&rdquo;",
        "now": "Landfill in the 20th century closed most of the creek, joining the island to Brooklyn as a peninsula. The sea still remembers the old geography: Hurricane Sandy in 2012 flooded the low ground along the former creek.",
    },
    {
        "hed": "Prospect Park is a designed wilderness",
        "then": "The land straddled the moraine and the outwash plain, with woods on the ridge and farms below. It held <strong>Battle Pass</strong>, one of the gaps American troops defended in 1776.",
        "now": "Olmsted and Vaux began building the park in 1866. The Long Meadow, the Lake and the Ravine are all engineered, including the Lake, which was dug on the flat outwash plain. The Ravine is <strong>Brooklyn&rsquo;s only forest</strong>, and it is also where to look for the warblers passing through each spring and fall.",
    },
]

WIKI_UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter; github.com/zach-works/weekend-dispatch-showcase)"}


def _week_pick(items, saturday, salt=0):
    """Deterministic weekly rotation: same Saturday → same pick."""
    if not items:
        return None
    week = saturday.isocalendar()[1] + saturday.year * 53
    return items[(week + salt) % len(items)]


def _wiki_get(url, params, tries=3):
    """GET a Wikimedia API endpoint as JSON, backing off on rate limits."""
    import time
    for attempt in range(tries):
        r = requests.get(url, headers=WIKI_UA, timeout=15, params=params)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        time.sleep(3 * (attempt + 1))
    r.raise_for_status()
    raise ValueError(f"non-JSON response from {url}")


def fetch_wiki_audio(file):
    """Resolve a Wikimedia Commons audio file to its URL plus a credit line. None on any failure."""
    import re
    try:
        info = next(iter(_wiki_get("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "titles": f"File:{file}", "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime", "format": "json"})["query"]["pages"].values()))["imageinfo"][0]
        if not (info.get("mime") or "").startswith("audio/"):
            return None
        meta = info.get("extmetadata", {})
        strip = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()
        artist = strip(meta.get("Artist", {}).get("value")) or "Unknown"
        lic = strip(meta.get("LicenseShortName", {}).get("value")) or "see source"
        via = "xeno-canto and Wikimedia Commons" if " XC" in file else "Wikimedia Commons"
        return {"url": info["url"].split("?")[0], "mime": info["mime"],
                "credit": f"Recording: {artist}, {lic}, via {via}"}
    except Exception as e:
        print(f"  [nature] audio lookup failed for {file}: {e}")
        return None


def fetch_wiki_photo(title=None, file=None):
    """
    Resolve a Wikimedia Commons image (by file name, or the lead image of an
    English Wikipedia article) to a 960px thumbnail URL plus credit line.
    Returns None on any failure so the card simply renders without a photo.
    """
    import re
    try:
        if not file:
            pages = _wiki_get("https://en.wikipedia.org/w/api.php", {
                "action": "query", "titles": title, "prop": "pageimages",
                "piprop": "name", "format": "json", "redirects": 1})["query"]["pages"]
            file = next(iter(pages.values())).get("pageimage")
            if not file:
                return None
        info = next(iter(_wiki_get("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "titles": f"File:{file}", "prop": "imageinfo",
            "iiprop": "url|extmetadata", "iiurlwidth": 960, "format": "json"})["query"]["pages"].values()))["imageinfo"][0]
        meta = info.get("extmetadata", {})
        strip = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()
        artist = strip(meta.get("Artist", {}).get("value")) or "Unknown"
        lic = strip(meta.get("LicenseShortName", {}).get("value")) or "see source"
        url = info.get("thumburl") or info.get("url")
        if not url:
            return None
        url = url.split("?")[0]
        return {"url": url, "credit": f"Photo: {artist}, {lic}, via Wikimedia Commons",
                "page": info.get("descriptionurl", "")}
    except Exception as e:
        print(f"  [nature] photo lookup failed for {title or file}: {e}")
        return None





def _species_card(entry):
    photo = fetch_wiki_photo(title=entry.get("wiki"), file=entry.get("photo_file"))
    photo_html = ""
    if photo:
        note = entry.get("photo_note")
        cap = f"{note}. {photo['credit']}" if note else photo["credit"]
        photo_html = (f'<figure class="xs-photo"><img src="{photo["url"]}" alt="{entry["name"]}" width="536">'
                      f'<figcaption>{cap}</figcaption></figure>')
    audio_html = ""
    if entry.get("audio_file"):
        clip = fetch_wiki_audio(entry["audio_file"])
        if clip:
            audio_html = (f'<figure class="xs-audio"><div class="xs-audio-label">&#9835; Listen &middot; {entry.get("audio_note", "")}</div>'
                          f'<audio controls preload="none"><source src="{clip["url"]}" type="{clip["mime"]}"></audio>'
                          f'<figcaption>{clip["credit"]}</figcaption></figure>')
    facts = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in entry["facts"])
    return f"""
          <div class="xs-hed">{entry["name"]}</div>
          <div class="xs-sub">{entry["latin"]} &mdash; {entry["tagline"]}</div>
          {photo_html}
          {audio_html}
          <dl class="xs-kv">{facts}</dl>
          <div class="xs-box">
            <div class="xs-box-label">Conservation angle</div>
            <p>{entry["conservation"]}</p>
          </div>"""


# Map of the Week: one Brooklyn or Manhattan neighbourhood per Saturday, on a
# USGS aerial photo, with a 10-20-30 rule check and the five biggest trunks
# starred. Rotation alternates boroughs, nearest-to-home first (78 weeks).
ROTATION_START = datetime.date(2026, 10, 3)
TREE_MAP_ROTATION = [
    ("BK61", "Fort Greene North"), ("MN28", "Lower East Side"), ("BK63", "Fort Greene South"),
    ("MN25", "Battery Park City-Lower Manhattan"), ("BK35", "Stuyvesant Heights"), ("MN27", "Chinatown"),
    ("BK60", "Prospect Lefferts Gardens-Wingate"), ("MN50", "Stuyvesant Town-Cooper Village"),
    ("BK64", "Prospect Heights"), ("MN22", "East Village"), ("BK69", "Clinton Hill"),
    ("MN20", "Murray Hill-Kips Bay"), ("BK75", "Bedford"), ("MN24", "SoHo-TriBeCa-Civic Center-Little Italy"),
    ("BK79", "Ocean Hill"), ("MN21", "Gramercy"), ("BK78", "Bushwick South"),
    ("MN19", "Turtle Bay-East Midtown"), ("BK96", "Rugby-Remsen Village"), ("MN23", "West Village"),
    ("BK95", "Erasmus"), ("MN17", "Midtown-Midtown South"), ("BK81", "Brownsville"),
    ("MN31", "Lenox Hill-Roosevelt Island"), ("BK72", "Williamsburg"),
    ("MN13", "Hudson Yards-Chelsea-Flatiron-Union Square"), ("BK77", "Bushwick North"), ("MN15", "Clinton"),
    ("BK68", "Fort Greene"), ("MN40", "Upper East Side-Carnegie Hill"), ("BK91", "East Flatbush-Farragut"),
    ("MN32", "Yorkville"), ("BK37", "Park Slope-Gowanus"), ("MN14", "Lincoln Square"),
    ("BK85", "East New York (Pennsylvania Ave)"), ("MN33", "East Harlem South"), ("BK40", "Windsor Terrace"),
    ("MN12", "Upper West Side"), ("BK90", "East Williamsburg"), ("MN34", "East Harlem North"),
    ("BK38", "DUMBO-Vinegar Hill-Downtown Brooklyn-Boerum Hill"), ("MN11", "Central Harlem South"),
    ("BK42", "Flatbush"), ("MN09", "Morningside Heights"), ("BK73", "North Side-South Side"),
    ("MN06", "Manhattanville"), ("BK09", "Brooklyn Heights-Cobble Hill"),
    ("MN03", "Central Harlem North-Polo Grounds"), ("BK41", "Kensington-Ocean Parkway"),
    ("MN04", "Hamilton Heights"), ("BK83", "Cypress Hills-City Line"), ("MN36", "Washington Heights South"),
    ("BK82", "East New York"), ("MN35", "Washington Heights North"), ("BK58", "Flatlands"),
    ("MN01", "Marble Hill-Inwood"), ("BK50", "Canarsie"),
    ("BK33", "Carroll Gardens-Columbia Street-Red Hook"), ("BK93", "Starrett City"), ("BK76", "Greenpoint"),
    ("BK43", "Midwood"), ("BK32", "Sunset Park West"), ("BK88", "Borough Park"), ("BK34", "Sunset Park East"),
    ("BK46", "Ocean Parkway South"), ("BK45", "Georgetown-Marine Park-Bergen Beach-Mill Basin"),
    ("BK44", "Madison"), ("BK25", "Homecrest"), ("BK30", "Dyker Heights"), ("BK28", "Bensonhurst West"),
    ("BK31", "Bay Ridge"), ("BK29", "Bensonhurst East"),
    ("BK17", "Sheepshead Bay-Gerritsen Beach-Manhattan Beach"), ("BK27", "Bath Beach"), ("BK26", "Gravesend"),
    ("BK19", "Brighton Beach"), ("BK23", "West Brighton"), ("BK21", "Seagate-Coney Island"),
]
NTA_LAYER_URL = ("https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/arcgis/rest/services/"
                 "NYC_Neighborhood_Tabulation_Areas_2010/FeatureServer/0/query")
TREE_POINTS_URL = "https://data.cityofnewyork.us/resource/hn5i-inap.json"    # NYC Parks Forestry Tree Points (live)
PLANTING_SPACES_URL = "https://data.cityofnewyork.us/resource/82zj-84is.json"
CENTERLINE_URL = "https://data.cityofnewyork.us/resource/inkn-q76z.json"
USGS_TILE_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
MAP_ZOOM = 16
MAP_COLOURS = ["#F2C14E", "#E0633A", "#5B8DEF", "#8BC34A"]


def _merc(lat, lon):
    world = 256 * 2 ** MAP_ZOOM
    return ((lon + 180) / 360 * world,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * world)


def _pip(x, y, poly):
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy or 1e-9)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _ordinal(n):
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_STREET_ABBR = {"AVENUE": "Ave", "AVE": "Ave", "AV": "Ave", "STREET": "St", "ST": "St", "PLACE": "Pl", "PL": "Pl",
                "PARKWAY": "Pkwy", "PKWY": "Pkwy", "BOULEVARD": "Blvd", "BLVD": "Blvd", "ROAD": "Rd", "RD": "Rd",
                "DRIVE": "Dr", "DR": "Dr", "FDR": "FDR"}


def _street(name):
    """'WEST 90 STREET' -> 'W 90th St'; 'WEST END AVENUE' -> 'West End Ave'."""
    ws = (name or "").upper().replace("F D R", "FDR").split()
    out = []
    for i, w in enumerate(ws):
        nxt = ws[i + 1] if i + 1 < len(ws) else ""
        if w in ("WEST", "EAST", "NORTH", "SOUTH", "W", "E", "N", "S"):
            out.append(w[0] if nxt.isdigit() else w.title())
        elif w.isdigit() and out and out[-1] in ("W", "E", "N", "S"):
            out.append(_ordinal(w))
        else:
            out.append(_STREET_ABBR.get(w, w.title()))
    return " ".join(out)


_ODD_STREETS = ("LINE", "EXIT", "RAMP", "BEND", "WAY", "SHUTTLE", "ENTRANCE", "BRIDGE", "TUNNEL", "PATH")
_PROPER_TREE_WORDS = {"American", "London", "Japanese", "Chinese", "Norway", "Kentucky", "Amur", "Callery", "Turkish",
                      "European", "Siberian", "Korean", "English", "Oriental", "Ohio", "Osage", "Persian", "Freeman",
                      "Kwanzan", "Carolina", "Himalayan"}


def _common_name(c):
    return c if c.split()[:1] and c.split()[0] in _PROPER_TREE_WORDS else c[:1].lower() + c[1:]


def _soql_multipolygon(rings):
    parts = []
    for ring in rings:
        k = max(1, len(ring) // 150)          # keep the query URL short; exact filter happens locally
        pts = ring[::k] + [ring[0]]
        parts.append("((" + ",".join(f"{x:.6f} {y:.6f}" for x, y in pts) + "))")
    return "MULTIPOLYGON(" + ",".join(parts) + ")"


def _socrata_all(url, params):
    rows, off = [], 0
    while True:
        r = requests.get(url, timeout=90, params={**params, "$limit": 50000, "$offset": off})
        r.raise_for_status()
        batch = r.json(); rows += batch
        if len(batch) < 50000:
            return rows
        off += 50000


def _aerial(x0, y0, x1, y1, width_px=1100):
    """Stitch USGS imagery tiles for the box and scale to ~width_px wide (keeps the page light)."""
    from PIL import Image
    import io
    tx0, ty0, tx1, ty1 = int(x0 // 256), int(y0 // 256), int(x1 // 256), int(y1 // 256)
    img = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            r = requests.get(USGS_TILE_URL.format(z=MAP_ZOOM, x=tx, y=ty), timeout=30)
            r.raise_for_status()
            img.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), ((tx - tx0) * 256, (ty - ty0) * 256))
    img = img.crop((int(x0 - tx0 * 256), int(y0 - ty0 * 256), int(x1 - tx0 * 256), int(y1 - ty0 * 256)))
    return img, width_px / img.width


def build_tree_map_html(code):
    """Map of the Week for one NTA. Raises on any fetch failure (caller drops the section)."""
    from PIL import Image, ImageDraw, ImageEnhance
    import io, base64, collections

    r = requests.get(NTA_LAYER_URL, timeout=60, params={
        "where": f"NTACode='{code}'", "outFields": "NTAName", "outSR": 4326, "f": "geojson"})
    r.raise_for_status()
    feat = r.json()["features"][0]
    name = feat["properties"]["NTAName"]
    g = feat["geometry"]
    rings = [p[0] for p in ([g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"])]
    wkt = _soql_multipolygon(rings)

    spaces = {(s.get("globalid") or "").upper(): s for s in _socrata_all(PLANTING_SPACES_URL, {
        "$select": "globalid,pssite,streetname,buildingnumber,crossstreet1,crossstreet2",
        "$where": f"within_polygon(location,'{wkt}')"})}
    trees, seen = [], set()
    for t in _socrata_all(TREE_POINTS_URL, {
            "$select": "genusspecies,dbh,tpcondition,location,plantingspaceglobalid",
            "$where": f"within_polygon(location,'{wkt}') AND tpstructure='Full' AND tpcondition NOT IN ('Dead','Unknown')"}):
        ps = (t.get("plantingspaceglobalid") or "").upper()
        if spaces.get(ps, {}).get("pssite") != "Street" or ps in seen:
            continue
        try:
            lon, lat = t["location"]["coordinates"]
        except (KeyError, TypeError, ValueError):
            continue
        if not any(_pip(lon, lat, ring) for ring in rings):
            continue
        latin, _, common = (t.get("genusspecies") or "Unknown").partition(" - ")
        seen.add(ps)
        trees.append({"lat": lat, "lon": lon, "latin": latin.strip(), "common": _common_name((common or latin).strip()),
                      "genus": latin.split()[0] if latin.strip() else "Unknown",
                      "dbh": float(t["dbh"]) if t.get("dbh") else None, "cond": (t.get("tpcondition") or "").lower(),
                      "ps": ps})
    n = len(trees)
    if n < 200:
        raise ValueError(f"only {n} street trees returned for {code}")

    # geometry in Web Mercator pixels at MAP_ZOOM
    P = [[_merc(la, lo) for lo, la in ring] for ring in rings]
    allp = [p for ring in P for p in ring]
    pad = 40
    x0, x1 = min(p[0] for p in allp) - pad, max(p[0] for p in allp) + pad
    y0, y1 = min(p[1] for p in allp) - pad, max(p[1] for p in allp) + pad
    W, H = x1 - x0, y1 - y0
    inside = lambda x, y: any(_pip(x, y, ring) for ring in P)
    u = max(W, H) / 536                         # marks/text sized to read the same at 536px
    fs = 9.5 * u

    img, k = _aerial(x0, y0, x1, y1)
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    for ring in P:
        draw.polygon([(x - x0, y - y0) for x, y in ring], fill=255)
    img = Image.composite(ImageEnhance.Color(img).enhance(0.75),
                          ImageEnhance.Brightness(ImageEnhance.Color(img).enhance(0.15)).enhance(0.45), mask)
    img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72, optimize=True)
    img_uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    sp = collections.Counter(t["common"] for t in trees)
    ge = collections.Counter(t["genus"] for t in trees)
    top = [s for s, _ in sp.most_common(4)]
    col = dict(zip(top, MAP_COLOURS))
    pct = lambda c: 100 * c / n
    big = sorted([t for t in trees if t["dbh"] and t["genus"] != "Unknown"], key=lambda t: -t["dbh"])[:5]

    # street labels: widest named streets running through the area, no overlaps
    s_, n_ = min(la for ring in rings for _, la in ring), max(la for ring in rings for _, la in ring)
    w_, e_ = min(lo for ring in rings for lo, _ in ring), max(lo for ring in rings for lo, _ in ring)
    r = requests.get(CENTERLINE_URL, timeout=90, params={
        "$select": "full_street_name,streetwidth,the_geom",
        "$where": f"within_box(the_geom,{n_},{w_},{s_},{e_}) AND rw_type='1'", "$limit": 20000})
    r.raise_for_status()
    segs = []
    for st in r.json():
        for line in (st.get("the_geom") or {}).get("coordinates", []):
            segs.append((st.get("full_street_name") or "", float(st.get("streetwidth") or 0),
                         [_merc(la, lo) for lo, la in line]))
    length, width = collections.Counter(), collections.defaultdict(float)
    for nm, wd, pts in segs:
        for a, b in zip(pts, pts[1:]):
            if nm and inside((a[0] + b[0]) / 2, (a[1] + b[1]) / 2):
                length[nm] += math.dist(a, b); width[nm] = max(width[nm], wd)
    majors = sorted([s for s in length if length[s] > 0.25 * max(W, H)], key=lambda s: (-width[s], -length[s]))[:8]

    placed = [(_merc(t["lat"], t["lon"])[0] - x0 + 5 * u, _merc(t["lat"], t["lon"])[1] - y0 - 2 * u, 20 * u, 14 * u)
              for t in big]
    hx, hy = _merc(*HOME_LATLON)
    placed.append((hx - x0 + 20 * u, hy - y0, 32 * u, 12 * u))
    free = lambda cx, cy, hw, hh: all(abs(cx - a) > hw + c or abs(cy - b) > hh + d for a, b, c, d in placed)
    labels = []
    for nm in majors:
        text = _street(nm).upper()
        half = len(text) * fs * 0.62 / 2 + fs * 0.4
        best = None
        for snm, _, pts in segs:
            if snm != nm:
                continue
            for a, b in zip(pts, pts[1:]):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                if not inside(mx, my):
                    continue
                mx -= x0; my -= y0
                ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                ang = ang - 180 if ang > 90 else ang + 180 if ang < -90 else ang
                rad = math.radians(ang)
                hw = abs(math.cos(rad)) * half + abs(math.sin(rad)) * fs * 0.6
                hh = abs(math.sin(rad)) * half + abs(math.cos(rad)) * fs * 0.6
                if not (hw < mx < W - hw and hh < my < H - hh and free(mx, my, hw, hh)):
                    continue
                d = math.dist((mx, my), (W / 2, H / 2))
                if best is None or d < best[0]:
                    best = (d, mx, my, ang, hw, hh)
        if best:
            _, mx, my, ang, hw, hh = best
            placed.append((mx, my, hw, hh))
            labels.append(f'<text x="{mx:.0f}" y="{my:.0f}" transform="rotate({ang:.1f} {mx:.0f} {my:.0f})" '
                          f'text-anchor="middle" dy="0.35em" font-family="Courier Prime,monospace" font-weight="700" '
                          f'font-size="{fs:.1f}" letter-spacing="{fs * 0.08:.1f}" fill="#F5EDD8" stroke="#1A1208" '
                          f'stroke-width="{fs * 0.42:.1f}" paint-order="stroke">{text}</text>')

    dots = []
    for layer in top[::-1]:
        for t in trees:
            if t["common"] == layer:
                x, y = _merc(t["lat"], t["lon"])
                dots.append(f'<circle cx="{x - x0:.0f}" cy="{y - y0:.0f}" r="{2.1 * u:.1f}" fill="{col[layer]}" '
                            f'stroke="#111" stroke-width="{0.63 * u:.1f}"/>')

    def star(cx, cy, R):
        return " ".join(f"{cx + (R if k % 2 == 0 else R * 0.45) * math.cos(math.pi / 2 + k * math.pi / 5):.1f},"
                        f"{cy - (R if k % 2 == 0 else R * 0.45) * math.sin(math.pi / 2 + k * math.pi / 5):.1f}"
                        for k in range(10))
    stars = []
    for i, t in enumerate(big, 1):
        x, y = _merc(t["lat"], t["lon"]); x -= x0; y -= y0; R = 12 * u
        stars.append(f'<polygon points="{star(x, y, R)}" fill="#FFE066" stroke="#1A1208" stroke-width="{1.6 * u:.1f}" '
                     f'stroke-linejoin="round"/><text x="{x + R * 0.9:.0f}" y="{y - R * 0.5:.0f}" '
                     f'font-family="Courier Prime,monospace" font-weight="700" font-size="{fs * 1.4:.1f}" fill="#FFE066" '
                     f'stroke="#1A1208" stroke-width="{fs * 0.5:.1f}" paint-order="stroke">{i}</text>')
    home = ""
    if inside(hx, hy):
        home = (f'<circle cx="{hx - x0:.0f}" cy="{hy - y0:.0f}" r="{9 * u:.0f}" fill="none" stroke="#FFF" '
                f'stroke-width="{1.8 * u:.1f}"/><text x="{hx - x0 + 12 * u:.0f}" y="{hy - y0 + 3.5 * u:.0f}" '
                f'font-family="Courier Prime,monospace" font-weight="700" font-size="{fs * 1.05:.1f}" fill="#FFF" '
                f'stroke="#1A1208" stroke-width="{fs * 0.42:.1f}" paint-order="stroke" letter-spacing="1">HOME</text>')
    outline = "".join(f'<path d="M{" L".join(f"{x - x0:.0f},{y - y0:.0f}" for x, y in ring)} Z" fill="none" '
                      f'stroke="#F5EDD8" stroke-width="{1.4 * u:.1f}"/>' for ring in P)
    svg = (f'<svg viewBox="0 0 {W:.0f} {H:.0f}" width="100%" xmlns="http://www.w3.org/2000/svg" style="display:block">'
           f'<image href="{img_uri}" width="{W:.0f}" height="{H:.0f}"/>{outline}{"".join(dots)}{"".join(labels)}'
           f'{home}{"".join(stars)}</svg>')
    legend = "".join(f'<span class="mw-key"><i style="background:{col[s]}"></i>{s} {pct(sp[s]):.1f}%</span>' for s in top)
    legend += '<span class="mw-key"><i class="star">&#9733;</i>5 largest trunks</span>'

    over_sp = [(s, pct(c)) for s, c in sp.most_common() if pct(c) > 10]
    over_ge = [(g_, pct(c)) for g_, c in ge.most_common() if pct(c) > 20 and g_ != "Unknown"]
    near_ge = [(g_, pct(c)) for g_, c in ge.most_common() if 17 <= pct(c) <= 20 and g_ != "Unknown"]

    def fmt(xs, italic=False):
        parts = [f"<strong>{'<em>' + s + '</em>' if italic else s} ({p:.1f}%)</strong>" for s, p in xs]
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    gw = lambda xs: "genus" if len(xs) == 1 else "genera"
    verb = lambda xs, one, many: one if len(xs) == 1 else many
    hed = f"{name} {'breaks' if over_sp or over_ge else 'passes'} the 10-20-30 rule"
    if over_sp:
        finding = f"Here, {fmt(over_sp)} {verb(over_sp, 'breaks', 'each break')} the species line."
        if over_ge:
            finding += f" The {gw(over_ge)} {fmt(over_ge, True)} also {verb(over_ge, 'passes', 'pass')} 20%."
    elif over_ge:
        finding = f"No single species passes 10%, but the {gw(over_ge)} {fmt(over_ge, True)} {verb(over_ge, 'passes', 'pass')} 20%."
    else:
        finding = f"No species passes 10%. The most common, <strong>{top[0]} ({pct(sp[top[0]]):.1f}%)</strong>, stays under the line."
    if near_ge and not over_ge:
        finding += f" The {gw(near_ge)} {fmt(near_ge, True)} {verb(near_ge, 'sits', 'sit')} just under the 20% ceiling."

    def address(t):
        s = spaces.get(t["ps"], {})
        st, no = s.get("streetname"), (s.get("buildingnumber") or "").strip()
        if not st:
            return ""
        a = f", outside {no} {_street(st)}" if no[:1].isdigit() else f", on {_street(st)}"
        c1, c2 = s.get("crossstreet1"), s.get("crossstreet2")
        if all(c and not any(k_ in c.upper() for k_ in _ODD_STREETS) and _street(c) != _street(st) for c in (c1, c2)):
            a += f", between {_street(c1)} and {_street(c2)}"
        return a
    rows = "".join(
        f'<li><span class="bt-n">{i}</span><div><strong>{t["common"][:1].upper() + t["common"][1:]}</strong>, '
        f'{t["dbh"]:.0f}&Prime; trunk{address(t)}<div class="bt-meta"><em>{t["latin"]}</em> &middot; '
        f'condition: {t["cond"]}</div></div></li>' for i, t in enumerate(big, 1))

    return f"""
          <div class="xs-hed">{hed}</div>
          <div class="xs-sub">{n:,} living street trees on a USGS aerial photo</div>
          <figure class="mw-fig">{svg}<div class="mw-legend">{legend}</div></figure>
          <p>Urban foresters use Santamour&rsquo;s <strong>10-20-30 rule</strong> as a hedge against pests: no more than 10% of a street-tree population from one species, 20% from one genus, 30% from one family. {finding}</p>
          <div class="xs-box">
            <div class="xs-box-label">&#9733; The five biggest trunks, likely the oldest trees here</div>
            <ol class="bt-list">{rows}</ol>
          </div>
          <div class="xs-sign">Sources: NYC Parks Forestry Tree Points (live inventory) &middot; NYC street centerline &middot; imagery: USGS The National Map</div>"""


def map_of_the_week(saturday):
    return TREE_MAP_ROTATION[((saturday - ROTATION_START).days // 7) % len(TREE_MAP_ROTATION)][0]


# Shore Leave: live Rockaway conditions dashboard (NOAA tides + harbor-entrance
# buoy) plus a feature alternating East Coast lighthouses / Pacific coast.
ROCKAWAY_LATLON = (40.583, -73.815)
TIDE_STATION = "8516881"          # NOAA, East Rockaway Inlet (tide predictions)
WAVE_BUOY = "44065"               # NOAA NDBC, New York Harbor entrance
WATER_FALLBACK_STATION = "8531680"  # NOAA, Sandy Hook (water temperature)
NOAA_TIDES_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

SHORE_EAST = [
    {
        "kicker": "East Coast &middot; Lighthouse", "name": "Sandy Hook Lighthouse", "wiki": "Sandy Hook Light",
        "tagline": "Sandy Hook, N.J. &mdash; first lit in 1764, the oldest working lighthouse in the United States",
        "facts": [
            ("Built", "1764, paid for by a lottery run by New York merchants who wanted a safer harbor entrance. It was first called the New York Lighthouse."),
            ("Tower", "An octagonal rubble-stone tower about 103 feet tall."),
            ("The moving point", "It was built near the tip of the hook. Sand carried north by the currents has since stretched the hook well over a mile past it."),
            ("Today", "Part of Gateway National Recreation Area, the same park that includes Fort Tilden and Jacob Riis Park in the Rockaways."),
        ],
    },
    {
        "kicker": "East Coast &middot; Lighthouse", "name": "Montauk Point Lighthouse", "wiki": "Montauk Point Light",
        "tagline": "Montauk, N.Y. &mdash; the first lighthouse in New York State",
        "facts": [
            ("Built", "Authorized under President George Washington and finished in 1796."),
            ("Tower", "About 110 feet of sandstone, painted white with a single brown band."),
            ("The eroding bluff", "It was built roughly 300 feet back from the edge. Storms have eaten the bluff to a fraction of that, and terracing and rock armour now hold the point in place."),
            ("Today", "A museum and a National Historic Landmark at the eastern tip of Long Island."),
        ],
    },
    {
        "kicker": "East Coast &middot; Lighthouse", "name": "The Little Red Lighthouse", "wiki": "Little Red Lighthouse",
        "tagline": "Jeffrey&rsquo;s Hook, Manhattan &mdash; the lighthouse under the George Washington Bridge",
        "facts": [
            ("Origins", "Built in 1880 at Sandy Hook, then moved to Jeffrey&rsquo;s Hook on the Hudson in 1921."),
            ("Outshone", "Once the bridge opened in 1931, its lights made the little tower obsolete, and it was decommissioned in 1947."),
            ("Saved by a book", "The 1942 children&rsquo;s book <em>The Little Red Lighthouse and the Great Gray Bridge</em> made it famous, and public outcry stopped its sale for scrap."),
            ("Today", "Owned by NYC Parks in Fort Washington Park, reachable by the Hudson River Greenway."),
        ],
    },
    {
        "kicker": "East Coast &middot; Lighthouse", "name": "Fire Island Lighthouse", "wiki": "Fire Island Light",
        "tagline": "Fire Island, N.Y. &mdash; for generations of immigrants, the first light of America",
        "facts": [
            ("Built", "The current tower dates to 1858 and stands about 168 feet tall; its black-and-white bands came later."),
            ("Landfall", "For transatlantic ships bound for New York, it was often the first American light they saw."),
            ("Rescued", "Decommissioned in 1974 and left to decay, it was restored by a local preservation society and relit in 1986."),
            ("Today", "Part of Fire Island National Seashore, a walk from Robert Moses State Park."),
        ],
    },
    {
        "kicker": "East Coast &middot; Lighthouse", "name": "Portland Head Light", "wiki": "Portland Head Light",
        "tagline": "Cape Elizabeth, Maine &mdash; one of the oldest lighthouses in the country",
        "facts": [
            ("Built", "First lit in January 1791, early in George Washington&rsquo;s presidency."),
            ("Setting", "A white tower on a rocky headland at the entrance to Portland Harbor, now inside Fort Williams Park."),
            ("Fame", "It is often called the most photographed lighthouse in America."),
            ("Today", "Still an active aid to navigation, with a museum in the keepers&rsquo; house."),
        ],
    },
    {
        "kicker": "East Coast &middot; Lighthouse", "name": "Cape Hatteras Lighthouse", "wiki": "Cape Hatteras Light",
        "tagline": "Outer Banks, N.C. &mdash; the lighthouse that was moved away from the sea",
        "facts": [
            ("Built", "1870, the tallest brick lighthouse in the United States at about 198 feet."),
            ("Stripes", "The black-and-white spiral is its daymark, so sailors could tell it apart from other lights by day."),
            ("The move", "By the 1990s the eroding shoreline had nearly reached its base. In 1999 the whole tower was rolled about 2,900 feet inland."),
            ("Why here", "It guards Diamond Shoals, the &ldquo;Graveyard of the Atlantic.&rdquo;"),
        ],
    },
]

SHORE_WEST = [
    {
        "kicker": "Pacific Coast &middot; Forest &amp; Sea", "name": "Marbled Murrelet", "wiki": "Marbled murrelet",
        "tagline": "<em>Brachyramphus marmoratus</em> &mdash; the seabird that nests in old-growth forest",
        "facts": [
            ("Double life", "It feeds at sea on small fish, then flies inland, sometimes dozens of kilometres, to nest on a single wide, mossy limb high in an old-growth Sitka spruce, Douglas-fir or redcedar."),
            ("The mystery", "No one found its nest until 1974, when a tree trimmer came across a chick high in a Douglas-fir in California&rsquo;s Big Basin Redwoods State Park."),
            ("ID", "A small, stubby auk. Mottled brown in breeding season, which gives it its name; crisp black and white in winter."),
        ],
        "conservation": "Because it needs old trees with big mossy branches, its fortunes track old-growth logging. It is listed as Threatened under Canada&rsquo;s Species at Risk Act and under the U.S. Endangered Species Act in Washington, Oregon and California.",
    },
    {
        "kicker": "Pacific Coast &middot; Forest &amp; Sea", "name": "Bull Kelp", "wiki": "Nereocystis",
        "tagline": "<em>Nereocystis luetkeana</em> &mdash; the underwater forest of the North Pacific",
        "facts": [
            ("Growth", "An annual seaweed that grows from the seafloor to the surface in a single season, among the fastest-growing organisms on Earth."),
            ("The float", "A gas-filled bulb at the top of its long stipe holds the blades up at the surface, in the light."),
            ("Habitat", "Its forests shelter young rockfish and salmon and feed a whole food web of snails, urchins and the otters that eat them."),
        ],
        "conservation": "After sea star wasting disease killed off urchin predators from 2013, urchin numbers exploded along parts of the coast and grazed kelp forests down to bare &ldquo;urchin barrens,&rdquo; worst in northern California.",
    },
    {
        "kicker": "Pacific Coast &middot; Forest &amp; Sea", "name": "Sea Otter", "wiki": "Sea otter",
        "tagline": "<em>Enhydra lutris</em> &mdash; the keystone species of the kelp forest",
        "facts": [
            ("Warmth", "With no blubber, it relies on the densest fur of any animal and eats a large share of its body weight each day."),
            ("Tools", "It floats on its back and uses rocks to crack open shellfish."),
            ("Keystone", "By eating urchins, otters keep urchins from mowing down kelp, so where otters return, kelp forests tend to recover."),
        ],
        "conservation": "The fur trade wiped sea otters out of British Columbia by the early 1900s. Between 1969 and 1972, 89 otters were moved from Alaska to the west coast of Vancouver Island, and their descendants now number in the thousands.",
    },
    {
        "kicker": "Pacific Coast &middot; Forest &amp; Sea", "name": "Southern Resident Killer Whales", "wiki": "Southern resident orcas",
        "tagline": "<em>Orcinus orca</em> &mdash; the J, K and L pods of the Salish Sea",
        "facts": [
            ("Who", "Three family groups that spend much of the year in the waters between Vancouver Island, Vancouver and Seattle."),
            ("Diet", "Specialists in salmon, above all Chinook, the largest salmon species."),
            ("Culture", "Each pod has its own dialect of calls, passed down through the generations."),
        ],
        "conservation": "Listed as endangered in both Canada and the U.S., with fewer than 80 whales. Their three main threats are too little Chinook salmon, pollutants in the food web, and noise and disturbance from boats.",
    },
    {
        "kicker": "Pacific Coast &middot; Forest &amp; Sea", "name": "Salmon Forests", "wiki": "Sockeye salmon",
        "tagline": "How the ocean fertilizes the coastal rainforest",
        "facts": [
            ("The run", "Pacific salmon spend years growing at sea, then return to the stream where they hatched to spawn and die."),
            ("The delivery", "Bears, eagles and wolves carry salmon carcasses from the streams into the forest and leave the remains on the forest floor."),
            ("The evidence", "Researchers have traced ocean-derived nitrogen, which has a distinctive chemical signature, into streamside trees and plants."),
        ],
        "conservation": "When runs decline, the forest loses a fertilizer delivery that has arrived every autumn for thousands of years, which is why salmon are managed as a forest issue, not just a fisheries one.",
    },
    {
        "kicker": "Pacific Coast &middot; Forest &amp; Sea", "name": "Western Hemlock", "wiki": "Tsuga heterophylla",
        "tagline": "<em>Tsuga heterophylla</em> &mdash; the shade-lover of the coastal rainforest",
        "facts": [
            ("Tell-tale", "The very top of the tree droops over, like a bent fishing rod, which makes it easy to pick out on a skyline."),
            ("Needles", "Short, flat and of uneven lengths, with two white bands underneath."),
            ("Seedbeds", "Seedlings often sprout on rotting &ldquo;nurse logs,&rdquo; leaving rows of trees standing on stilt-like roots once the log is gone."),
        ],
        "conservation": "One of the most common trees on the B.C. coast and Washington&rsquo;s state tree. Its shade tolerance lets it grow up under older Douglas-fir and take over as the forest ages.",
    },
]


def _sun_times(day, lat, lon):
    """Sunrise/sunset (local clock hours, America/New_York) via the NOAA solar approximation."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo("America/New_York")
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(decl))
                                - math.tan(math.radians(lat)) * math.tan(decl)))
    off = datetime.datetime.combine(day, datetime.time(12), tz).utcoffset().total_seconds() / 3600
    rise = (720 - 4 * (lon + ha) - eqt) / 60 + off
    set_ = (720 - 4 * (lon - ha) - eqt) / 60 + off
    return rise, set_


def fetch_rockaway_conditions(saturday):
    """Tides (30-min + highs/lows) for Sat–Sun, latest buoy waves/water temp, sun times. Raises if tides fail."""
    sunday = saturday + datetime.timedelta(days=1)
    base = {"application": "WeekendDispatch", "begin_date": saturday.strftime("%Y%m%d"),
            "end_date": sunday.strftime("%Y%m%d"), "datum": "MLLW", "station": TIDE_STATION,
            "time_zone": "lst_ldt", "units": "english", "format": "json", "product": "predictions"}
    parse = lambda p: datetime.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
    r = requests.get(NOAA_TIDES_URL, timeout=30, params={**base, "interval": "30"}); r.raise_for_status()
    curve = [(parse(p), float(p["v"])) for p in r.json()["predictions"]]
    r = requests.get(NOAA_TIDES_URL, timeout=30, params={**base, "interval": "hilo"}); r.raise_for_status()
    hilo = [(parse(p), float(p["v"]), p["type"]) for p in r.json()["predictions"]]
    if len(curve) < 60 or len(hilo) < 4:
        raise ValueError("incomplete tide predictions")

    out = {"curve": curve, "hilo": hilo, "water_f": None, "wave_ft": None, "period_s": None, "swell_deg": None,
           "sun": {d: _sun_times(d, *ROCKAWAY_LATLON) for d in (saturday, sunday)}}
    try:
        lines = requests.get(f"https://www.ndbc.noaa.gov/data/realtime2/{WAVE_BUOY}.txt", timeout=20).text.splitlines()
        hdr = lines[0].lstrip("#").split()
        for ln in lines[2:]:
            row = dict(zip(hdr, ln.split()))
            if out["wave_ft"] is None and row.get("WVHT", "MM") != "MM":
                out["wave_ft"] = float(row["WVHT"]) * 3.28084
                out["period_s"] = float(row["DPD"]) if row.get("DPD", "MM") != "MM" else None
                out["swell_deg"] = float(row["MWD"]) if row.get("MWD", "MM") != "MM" else None
            if out["water_f"] is None and row.get("WTMP", "MM") != "MM":
                out["water_f"] = float(row["WTMP"]) * 9 / 5 + 32
            if out["wave_ft"] is not None and out["water_f"] is not None:
                break
    except Exception as e:
        print(f"  [shore] buoy {WAVE_BUOY} unavailable: {e}")
    if out["water_f"] is None:
        try:
            r = requests.get(NOAA_TIDES_URL, timeout=20, params={
                "product": "water_temperature", "application": "WeekendDispatch", "date": "latest",
                "station": WATER_FALLBACK_STATION, "units": "english", "time_zone": "lst_ldt", "format": "json"})
            out["water_f"] = float(r.json()["data"][0]["v"])
        except Exception as e:
            print(f"  [shore] water temperature unavailable: {e}")
    return out


def _wetsuit(f):
    return ("boardshorts weather" if f >= 73 else "a spring suit or 2mm top" if f >= 68 else "a 3/2 full wetsuit"
            if f >= 63 else "a 4/3 full wetsuit" if f >= 58 else "a 4/3 or 5/4 with booties" if f >= 52
            else "a 5/4 with hood, gloves and booties")


def _wave_body(ft):
    return ("ankle-to-knee high" if ft < 1.5 else "knee-to-thigh high" if ft < 2.5 else "about waist high"
            if ft < 3.5 else "chest-to-head high" if ft < 5 else "overhead")


def _wave_character(ft, period):
    """Plain-English read of height + period, e.g. 'Small, clean swell'."""
    size = ("Tiny" if ft < 1 else "Small" if ft < 2.5 else "Moderate" if ft < 4 else "Sizable" if ft < 6 else "Big")
    if not period:
        return f"{size} waves"
    kind = ("choppy, wind-driven waves" if period < 6 else "short-period swell" if period < 8
            else "clean swell" if period < 13 else "powerful ground swell")
    return f"{size}, {kind}"


def _compass16(deg):
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][
        round(deg / 22.5) % 16]


def build_shore_dashboard_html(c, saturday):
    NAVY, PALE, CREAM, SAND = "#1E2B3A", "#9EC3D6", "#F5EDD8", "#E4D4AE"
    fmt = lambda d: d.strftime("%-I:%M") + ("a" if d.hour < 12 else "p")
    gauges = []

    if c["water_f"] is not None:
        f = c["water_f"]; lo, hi = 35, 80; fr = max(0, min(1, (f - lo) / (hi - lo)))
        ticks = "".join(f'<text x="44" y="{14 + 102 * (1 - (t - lo) / (hi - lo)) + 3:.0f}" text-anchor="end" '
                        f'font-family="Courier Prime,monospace" font-size="9" fill="{PALE}">{t}&deg;</text>' for t in (40, 50, 60, 70, 80))
        gauges.append(f'''<div><svg viewBox="0 0 120 150" width="100%" style="max-width:120px;display:block;margin:0 auto">
<rect x="50" y="10" width="20" height="110" rx="10" fill="#2A3B4E"/><rect x="54" y="{14 + 102 * (1 - fr):.0f}" width="12" height="{102 * fr:.0f}" rx="6" fill="#E0633A"/>
<circle cx="60" cy="126" r="16" fill="#E0633A"/>{ticks}
<text x="76" y="{14 + 102 * (1 - fr) + 4:.0f}" font-family="Cormorant Garamond,serif" font-size="16" fill="{CREAM}">{f:.0f}&deg;F</text></svg>
<div class="sl-cap">Water {f:.0f}&deg;F: {_wetsuit(f)}, by the usual surfer&rsquo;s rule of thumb</div></div>''')

    if c["wave_ft"] is not None:
        ft = c["wave_ft"]; wh = min(ft, 8) * 13.5
        per = f", {c['period_s']:.0f} seconds apart" if c["period_s"] else ""
        gauges.append(f'''<div><svg viewBox="0 0 120 150" width="100%" style="max-width:120px;display:block;margin:0 auto">
<g fill="#7F93A3"><circle cx="78" cy="30" r="8"/><rect x="72" y="40" width="12" height="36" rx="5"/><rect x="72" y="74" width="5" height="46" rx="2"/><rect x="79" y="74" width="5" height="46" rx="2"/></g>
<path d="M10 120 L10 {120 - wh:.0f} Q 40 {120 - wh - 10:.0f} 60 {120 - wh:.0f} T 110 {120 - wh:.0f} L110 120 Z" fill="{PALE}" opacity="0.8"/>
<line x1="8" y1="120" x2="112" y2="120" stroke="{PALE}"/>
<text x="60" y="142" text-anchor="middle" font-family="Cormorant Garamond,serif" font-size="15" fill="{CREAM}">{_wave_character(ft, c["period_s"]).lower()}</text></svg>
<div class="sl-cap"><span class="sl-cap-hed">{ft:.0f} ft waves{per}</span><br>Roughly {_wave_body(ft)}, measured at the harbor-entrance buoy</div></div>''')

    if c["swell_deg"] is not None:
        a = math.radians(c["swell_deg"]); cx, cy, R = 60, 66, 44
        fx, fy, tx, ty = cx + R * math.sin(a), cy - R * math.cos(a), cx + 10 * math.sin(a), cy - 10 * math.cos(a)
        card = "".join(f'<text x="{cx + R * math.sin(math.radians(d)):.0f}" y="{cy - R * math.cos(math.radians(d)) + 3:.0f}" '
                       f'text-anchor="middle" font-family="Courier Prime,monospace" font-size="9" fill="{PALE}">{l}</text>'
                       for d, l in ((0, "N"), (90, "E"), (180, "S"), (270, "W")))
        d16 = _compass16(c["swell_deg"])
        gauges.append(f'''<div><svg viewBox="0 0 120 150" width="100%" style="max-width:120px;display:block;margin:0 auto">
<defs><marker id="sl-ah" markerWidth="8" markerHeight="8" refX="5" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="{CREAM}"/></marker></defs>
<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" stroke="#2A3B4E" stroke-width="10"/>{card}
<path d="M{cx - 34} {cy + 18} Q {cx} {cy + 8} {cx + 34} {cy + 20}" stroke="{SAND}" stroke-width="3" fill="none"/>
<text x="{cx}" y="{cy + 34}" text-anchor="middle" font-family="Courier Prime,monospace" font-size="7" fill="{SAND}">ROCKAWAY BEACH</text>
<line x1="{fx:.1f}" y1="{fy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" stroke="{CREAM}" stroke-width="3" marker-end="url(#sl-ah)"/>
<text x="60" y="142" text-anchor="middle" font-family="Cormorant Garamond,serif" font-size="15" fill="{CREAM}">swell from the {d16}</text></svg>
<div class="sl-cap">The direction the waves are coming from</div></div>''')

    # tide curve with night shading
    W, H, top, bot, hrs, ymax = 536, 116, 22, 22, 48, max(v for _, v in c["curve"]) + 0.6
    t0 = datetime.datetime.combine(saturday, datetime.time())
    X = lambda d: (d - t0).total_seconds() / 3600 / hrs * W
    Y = lambda v: top + (1 - v / ymax) * (H - top - bot)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" style="display:block;background:{NAVY}">']
    for i, (rise, set_) in enumerate(c["sun"][d] for d in sorted(c["sun"])):
        off = i * 24
        parts.append(f'<rect x="{off / hrs * W:.1f}" y="0" width="{rise / hrs * W:.1f}" height="{H}" fill="#162130"/>')
        parts.append(f'<rect x="{(off + set_) / hrs * W:.1f}" y="0" width="{(24 - set_) / hrs * W:.1f}" height="{H}" fill="#162130"/>')
    pts = " ".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in c["curve"])
    parts.append(f'<polygon points="{X(c["curve"][0][0]):.1f},{H - bot} {pts} {X(c["curve"][-1][0]):.1f},{H - bot}" fill="{PALE}" opacity="0.18"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{PALE}" stroke-width="2.2"/>')
    parts.append(f'<line x1="{W / 2}" y1="8" x2="{W / 2}" y2="{H - bot}" stroke="{CREAM}" stroke-opacity="0.25" stroke-dasharray="3 3"/>')
    for i, lab in enumerate(("SATURDAY", "SUNDAY")):
        parts.append(f'<text x="{i * W / 2 + 8}" y="15" font-family="Courier Prime,monospace" font-size="9" letter-spacing="2" fill="{CREAM}" opacity="0.8">{lab}</text>')
    for h in (6, 12, 18, 30, 36, 42):
        parts.append(f'<text x="{h / hrs * W:.1f}" y="{H - 8}" text-anchor="middle" font-family="Courier Prime,monospace" font-size="8" fill="{CREAM}" opacity="0.55">{ {6: "6a", 12: "noon", 18: "6p"}[h % 24] }</text>')
    parts.append("</svg>")
    days = []
    for d in sorted(c["sun"]):
        ev = [h for h in c["hilo"] if h[0].date() == d]
        hi_ = " &amp; ".join(fmt(t) for t, _, ty in ev if ty == "H"); lo_ = " &amp; ".join(fmt(t) for t, _, ty in ev if ty == "L")
        rise, set_ = c["sun"][d]
        hm = lambda x: f"{int(x) % 12 or 12}:{round((x % 1) * 60):02d}{'a' if x < 12 else 'p'}"
        days.append(f'<b>{d.strftime("%a")}</b> high {hi_} &middot; low {lo_} &middot; sun {hm(rise)}&ndash;{hm(set_)}')

    return f'''
          <div class="sl-dash">
            <div class="sl-k">This weekend at Rockaway</div>
            {'<div class="sl-gauges">' + "".join(gauges) + '</div>' if gauges else ''}
            <div class="sl-tide">{"".join(parts)}</div>
            <div class="sl-times">{"<br>".join(days)}</div>
            <div class="sl-src">Tides: NOAA, East Rockaway Inlet &middot; water &amp; waves: NOAA buoy {WAVE_BUOY}, harbor entrance, latest reading when this issue was built &middot; shaded = night</div>
          </div>'''


def build_shore_leave_html(saturday):
    """Shore Leave section body. Dashboard and feature each fail independently."""
    body = ""
    try:
        body += build_shore_dashboard_html(fetch_rockaway_conditions(saturday), saturday)
    except Exception as e:
        print(f"  [shore] conditions skipped: {e}")
    weeks = (saturday - ROTATION_START).days // 7
    bank = SHORE_EAST if weeks % 2 == 0 else SHORE_WEST
    entry = bank[(weeks // 2) % len(bank)]
    photo = fetch_wiki_photo(title=entry["wiki"])
    photo_html = (f'<figure class="xs-photo"><img src="{photo["url"]}" alt="{entry["name"]}" width="536">'
                  f'<figcaption>{photo["credit"]}</figcaption></figure>') if photo else ""
    facts = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in entry["facts"])
    cons = (f'<div class="xs-box"><div class="xs-box-label">Conservation angle</div><p>{entry["conservation"]}</p></div>'
            if entry.get("conservation") else "")
    try:
        sound_html = dm.shore_sound_html(saturday)      # a different open-licence sea recording each week
    except Exception as e:
        sound_html = ""
        print(f"  [shore] sound skipped: {e}")
    body += f"""
          {dm.waves_html()}
          <div class="sl-kicker">{entry["kicker"]}</div>
          <div class="xs-hed">{entry["name"]}</div>
          <div class="xs-sub">{entry["tagline"]}</div>
          {photo_html}
          {sound_html}
          <dl class="xs-kv">{facts}</dl>{cons}"""
    return body


def build_nature_pieces(saturday):
    """
    Bird of the Week, Tree of the Week, Map of the Week, Then & Now and Shore
    Leave as {key: (label, inner_html)}. Each is independent and fail-closed: an
    error drops that one piece.
    """
    out = {}
    month = saturday.month

    birds = [b for b in NATURE_BIRDS if month in b["months"]]
    for key, label, bank, salt in (("bird", "Bird of the Week", birds, 0), ("tree", "Tree of the Week", NATURE_TREES, 3)):
        try:
            entry = _week_pick(bank, saturday, salt)
            if entry:
                out[key] = (label, _species_card(entry))
        except Exception as e:
            print(f"  [nature] {label} skipped: {e}")

    try:
        out["map"] = ("Map of the Week", build_tree_map_html(map_of_the_week(saturday)))
    except Exception as e:
        print(f"  [nature] Map of the Week skipped: {e}")

    try:
        tn = _week_pick(THEN_AND_NOW, saturday, 1)
        out["then_now"] = ("Then &amp; Now", f"""
          <div class="xs-hed">{tn["hed"]}</div>
          <div class="xs-cols">
            <div><div class="xs-col-label">Then</div><p>{tn["then"]}</p></div>
            <div><div class="xs-col-label">Now</div><p>{tn["now"]}</p></div>
          </div>""")
    except Exception as e:
        print(f"  [nature] Then & Now skipped: {e}")

    try:
        shore = build_shore_leave_html(saturday)
        if shore.strip():
            out["shore"] = ("Shore Leave", shore)
    except Exception as e:
        print(f"  [nature] Shore Leave skipped: {e}")

    return out


NATURE_CSS = """
  .xs-hed { font-family:'Cormorant Garamond',Georgia,serif; font-weight:600; font-size:21px; line-height:1.2; color:#1A1208; margin:0 0 10px; }
  .xs-hed + .xs-sub { margin-top:-8px; }
  .xs-sub { font-family:'Cormorant Garamond',Georgia,serif; font-style:italic; font-size:15px; color:#6A5030; margin:0 0 12px; }
  .xs-photo { margin:0 0 14px; }
  .xs-photo img { display:block; width:100%; height:auto; border:1px solid #D4B98A; }
  .xs-photo figcaption { font-family:'Courier Prime',monospace; font-size:7.5px; letter-spacing:.1em; text-transform:uppercase; color:#B89A5A; margin-top:6px; line-height:1.5; }
  .xs-audio { margin:0 0 16px; padding:10px 12px; background:#EFE3C3; border-left:3px solid #C4A46A; }
  .xs-audio-label { font-family:'Cormorant Garamond',Georgia,serif; font-style:italic; font-size:15px; color:#3A2A10; margin-bottom:8px; }
  .xs-audio audio { display:block; width:100%; }
  .xs-audio figcaption { font-family:'Courier Prime',monospace; font-size:7.5px; letter-spacing:.1em; text-transform:uppercase; color:#B89A5A; margin-top:6px; line-height:1.5; }
  .xs-kv { display:grid; grid-template-columns:92px 1fr; gap:6px 14px; margin:0 0 14px; }
  .xs-kv dt { font-family:'Courier Prime',monospace; font-size:8px; letter-spacing:.2em; text-transform:uppercase; color:#9B3A1A; padding-top:5px; }
  .xs-kv dd { margin:0; font-family:'Cormorant Garamond',Georgia,serif; font-size:16px; line-height:1.55; color:#2A1E0A; }
  .xs-box { background:#EFE3C3; border-left:3px solid #C4A46A; padding:12px 16px; margin:4px 0 12px; }
  .xs-box-label { font-family:'Courier Prime',monospace; font-size:8px; letter-spacing:.22em; text-transform:uppercase; color:#9B3A1A; margin-bottom:6px; }
  .b4-section .xs-box p { font-size:15.5px; line-height:1.6; margin:0; }
  .mw-fig { margin:0 0 12px; border:1px solid #D4B98A; }
  .mw-legend { display:flex; flex-wrap:wrap; gap:6px 14px; padding:8px 10px; border-top:1px solid #D4B98A; font-family:'Courier Prime',monospace; font-size:8.5px; letter-spacing:.06em; color:#5C4422; }
  .mw-key i { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:-1px; }
  .xs-cols { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:0 0 4px; }
  .xs-col-label { font-family:'Courier Prime',monospace; font-size:8px; letter-spacing:.22em; text-transform:uppercase; color:#9B3A1A; margin-bottom:6px; }
  .b4-section .xs-cols p { font-size:15.5px; line-height:1.6; }
  .xs-sign { font-family:'Courier Prime',monospace; font-size:8px; letter-spacing:.18em; text-transform:uppercase; color:#B89A5A; margin-top:10px; }
  .mw-key i.star { background:none; width:auto; height:auto; color:#B8923E; font-style:normal; font-size:11px; border-radius:0; }
  .bt-list { list-style:none; margin:0; padding:0; }
  .bt-list li { display:flex; gap:10px; align-items:flex-start; font-family:'Cormorant Garamond',Georgia,serif; font-size:15.5px; line-height:1.45; color:#2A1E0A; padding:6px 0; border-bottom:1px solid #E0CCA4; }
  .bt-list li:last-child { border-bottom:none; }
  .bt-n { flex:none; width:20px; height:20px; border-radius:50%; background:#1A1208; color:#FFF3C4; font-family:'Courier Prime',monospace; font-size:10px; font-weight:700; display:flex; align-items:center; justify-content:center; margin-top:2px; }
  .bt-meta { font-size:13.5px; color:#6A5030; }
  .sl-dash { background:#1E2B3A; color:#F5EDD8; padding:14px 16px 12px; margin:0 0 18px; }
  .sl-k { font-family:'Courier Prime',monospace; font-size:8.5px; letter-spacing:.24em; text-transform:uppercase; color:#9EC3D6; margin-bottom:8px; }
  .sl-gauges { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; text-align:center; margin-bottom:10px; }
  .sl-cap { font-family:'Cormorant Garamond',Georgia,serif; font-size:13px; font-style:italic; color:#C9D8DF; line-height:1.35; margin-top:4px; }
  .sl-cap-hed { font-style:normal; font-size:15px; color:#F5EDD8; }
  .sl-times { font-family:'Cormorant Garamond',Georgia,serif; font-size:14px; line-height:1.55; color:#EAF0F2; margin-top:8px; }
  .sl-times b { font-family:'Courier Prime',monospace; font-size:9px; letter-spacing:.14em; text-transform:uppercase; color:#9EC3D6; font-weight:400; margin-right:4px; }
  .sl-src { font-family:'Courier Prime',monospace; font-size:7px; letter-spacing:.08em; color:#8FA9B6; margin-top:8px; line-height:1.5; }
  .sl-kicker { font-family:'Courier Prime',monospace; font-size:8px; letter-spacing:.22em; text-transform:uppercase; color:#3F6A86; margin:0 0 6px; }
  @media (max-width:520px) { .sl-gauges { grid-template-columns:1fr 1fr; } .xs-cols { grid-template-columns:1fr; } .xs-kv { grid-template-columns:1fr; } .xs-kv dt { padding-top:0; } }
"""


def public_records_as_picks(pr_events):
    """
    Public Records' shows in the same shape as the DICE picks, so they can sit in the On Sale list. Each keeps
    its artists (the linked title) and its "time @ room" line word for word; its start time comes from that line.
    """
    import re as _re
    out = []
    for e in pr_events or []:
        d = e.get("date")
        if not d:
            continue
        parts = [p.strip() for p in e.get("text", "").split(" — ")]
        time_room = parts[2] if len(parts) > 2 else ""
        artists = " — ".join(parts[3:]) if len(parts) > 3 else ""
        m = _re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", time_room, _re.I)
        if m:
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            start = datetime.datetime(d.year, d.month, d.day, hour, int(m.group(2)))
        else:
            start = datetime.datetime(d.year, d.month, d.day, 23, 59)
        out.append({
            "name": artists or time_room, "url": e.get("url", ""), "start": start,
            "has_time": bool(m), "venue": "Public Records", "tracked_venue": "Public Records",
            "artists": [], "price": "", "sold_out": False,
            "dek": time_room if artists else "", "public_records": True,
            # a show listed at 12-4 am belongs to the end of that night, not the start of the day
            "sort_start": start + datetime.timedelta(days=1) if (m and start.hour < 5) else start,
        })
    return out


def on_sale_lineup(dice_picks, pr_events):
    """The On Sale list: the DICE picks and Public Records' shows together, in the order they start."""
    key = lambda e: (e.get("sort_start") or e["start"]).replace(tzinfo=None)    # compare wall-clock times
    return sorted(list(dice_picks or []) + public_records_as_picks(pr_events), key=key)


def news_feature_html(roman, news_url, items_html):
    """
    The News & Culture section of the main page: a masthead band in the page's own colours that echoes the News &
    Culture page's (the brownstone drawing and the title) with a button, then the five newest headlines under a
    section-mark label. The whole masthead is a link to the edition.
    """
    import html as _html
    url = _html.escape(news_url, quote=True)
    return f"""
        <div class="b4-section nc-feature">
          <a class="nc-masthead" href="{url}">
            <div class="nc-kicker">No. {roman.rstrip('.')} &middot; The weekend&rsquo;s reading</div>
            <div class="nc-vignette" aria-hidden="true">{dp.VIGNETTE_SVG}</div>
            <div class="nc-title">News &amp; Culture</div>
            <span class="nc-cta">Read the full edition &rarr;</span>
          </a>
          <div class="news-subhead">From this week&rsquo;s edition</div>
          <div class="news-feed">{items_html}</div>
        </div>"""


def build_nature_report_html(inat_html, nature):
    """
    The Nature Report section body: this week's iNaturalist sightings, then Bird, Tree and Map of the Week as
    sub-sections (each under a section-mark label, in the style of the page's other group labels). Returns ""
    when there is nothing at all, so the section is left out; it still appears if only one part is available.
    """
    html = inat_html or ""
    for key in ("bird", "tree", "map"):
        piece = (nature or {}).get(key)
        if piece:
            html += f'<div class="nature-sub"><div class="news-subhead">{piece[0]}</div>{piece[1]}</div>'
    return html


def _nature_section(label, roman, inner):
    return f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
{inner}
        </div>"""


# a raw string: the CSS has escapes like \00A7 that Python must not interpret
DESIGN_CSS = r"""
  /* Section headers, group labels and the small lines above items: newspaper rules, a typewriter headline with
     the numeral in a ring, italic serif group labels with a section mark, a square bullet on item lines.
     Prefixed with .b4-wrap so it wins over the base rules whatever their order. */
  .b4-wrap .news-subhead { border-top: 0; padding-top: 0; }
  .b4-wrap .b4-section { padding: 36px 0 26px; }
  .b4-wrap .b4-label-row { display: grid; grid-template-columns: 48px 1fr; column-gap: 14px; align-items: center; margin-bottom: 22px; padding: 12px 0;
    border-top: 4px solid #1A1208; border-bottom: 1px solid #1A1208; box-shadow: 0 -3px 0 #F5EDD8, 0 -4px 0 #1A1208; }
  .b4-wrap .b4-label-roman { grid-column: 1; grid-row: 1; width: 46px; height: 46px; border: 2px solid #9B3A1A; border-radius: 50%; display: flex;
    align-items: center; justify-content: center; font-size: 20px; color: #9B3A1A; opacity: 1; padding-right: 2px; }
  .b4-wrap .b4-label { grid-column: 2; grid-row: 1; font-family: 'Special Elite', 'Courier Prime', monospace; font-size: 27px; letter-spacing: 0;
    text-transform: none; color: #1A1208; line-height: 1.1; white-space: normal; }
  .b4-wrap .b4-label-rule { display: none; }
  .b4-wrap :is(.mta-group-label, .news-subhead, .xs-box-label, .xs-col-label, .sl-kicker) {
    font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-weight: 600; font-size: 21px; letter-spacing: 0; text-transform: none;
    color: #9B3A1A; margin: 18px 0 14px; padding-bottom: 4px; border-bottom: 1px dotted #C4A46A; }
  .b4-wrap :is(.mta-group-label, .news-subhead, .xs-box-label, .xs-col-label, .sl-kicker)::before { content: "\00A7\00A0"; }
  .b4-wrap .news-source { font-size: 10px; font-weight: 700; letter-spacing: .06em; color: #6A5030; }   /* tight enough that most fit one line on a phone */
  .b4-wrap .news-source::before { content: "\25A0\00A0"; color: #9B3A1A; font-size: 8px; vertical-align: 1px; }
  /* Bird, Tree and Map of the Week sit inside the Nature Report under a section-mark label */
  .b4-wrap .nature-sub { margin-top: 12px; }
  .b4-wrap .nature-sub + .nature-sub { margin-top: 28px; }
"""


# The News & Culture masthead on the main page: the front door to the separate edition, drawn as that page's own
# masthead turned inside out (dark band, brownstone line drawing, big serif title) so it is the one thing on the
# page that cannot be scrolled past. Full width of the page, like the Ink Band design.
NC_FEATURE_CSS = r"""
  .b4-wrap .b4-section.nc-feature { padding: 0 0 26px; border-bottom: 0; margin-top: 34px; }
  .b4-wrap .nc-masthead { display: block; margin: 0 -32px 26px; padding: 36px 40px 34px; background: #EDE0C0; color: #1A1208; text-align: center; text-decoration: none;
    border-top: 5px solid #1A1208; border-bottom: 5px solid #1A1208;
    box-shadow: inset 0 0 0 10px #EDE0C0, inset 0 0 0 12px #1A1208, inset 0 0 0 15px #EDE0C0, inset 0 0 0 16px #9B3A1A; }
  .b4-wrap .nc-kicker { font-family: 'Courier Prime', monospace; font-size: 10px; letter-spacing: .3em; text-transform: uppercase; color: #9B3A1A; }
  .b4-wrap .nc-vignette { max-width: 300px; margin: 20px auto 16px; color: #1A1208; opacity: .9; }
  .b4-wrap .nc-vignette svg { display: block; width: 100%; height: auto; }
  .b4-wrap .nc-title { font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 600; font-size: clamp(40px, 11.5vw, 60px); line-height: 1; letter-spacing: -.015em; color: #1A1208; }
  .b4-wrap .nc-title::after { content: ""; display: block; width: 44px; height: 2px; background: #9B3A1A; margin: 16px auto 0; }
  .b4-wrap .nc-cta { display: inline-block; margin-top: 24px; padding: 13px 26px; background: #1A1208; color: #F5EDD8; font-family: 'Courier Prime', monospace;
    font-size: 10px; font-weight: 700; letter-spacing: .22em; text-transform: uppercase; }
  .b4-wrap .nc-masthead:hover .nc-cta { background: #9B3A1A; }
  @media (max-width: 400px) {
    .b4-wrap .nc-masthead { padding: 32px 26px 30px; }
    .b4-wrap .nc-kicker { letter-spacing: .18em; font-size: 9px; }
    .b4-wrap .nc-cta { padding: 13px 18px; letter-spacing: .16em; }
  }
"""


def build_email_html(narrative, saturday_data, sunday_data, saturday, sunday,
                     mta_alerts, surface_alerts, pr_events, dice_picks=None,
                     long_list_url="", edible_picks=None,
                     headlines=None, news_url="", inat_html="", nature=None,
                     weekender_html="", around_extra_html=""):
    sat_label = saturday.strftime("%A, %B %d")
    sun_label = sunday.strftime("%B %d")
    sat_date_top = saturday.strftime("%B %d")
    sun_date_top = sunday.strftime("%B %d")
    year = saturday.year

    # Parse narrative into labeled sections
    section_order = [
        "MTA THIS WEEKEND",
        "WEATHER + BISCUIT",
        "SAMPLE GREENMARKET",
        "ON SALE THIS WEEKEND",
        "AROUND BROOKLYN",
        "NEWS & CULTURE",
    ]
    section_labels_display = [
        "MTA This Weekend",
        "Weather + Biscuit",
        "Sample Greenmarket",
        "On Sale This Weekend",          # Public Records' shows are part of this list
        "Around Brooklyn",
        "News &amp; Culture",
    ]
    # Roman numerals are handed out in the order sections actually appear, so a section that is left out
    # (News & Culture with nothing to show) never leaves a gap.
    romans = iter(["i.", "ii.", "iii.", "iv.", "v.", "vi.", "vii.", "viii.", "ix.", "x.", "xi.", "xii.", "xiii.", "xiv."])

    # Split narrative by section headers
    import re
    blocks = re.split(
        r'\n(?=' + '|'.join(re.escape(s) for s in section_order) + r')',
        narrative.strip()
    )

    parsed = {}
    for block in blocks:
        for key in section_order:
            if block.strip().upper().startswith(key):
                content = block.strip()[len(key):].strip().lstrip(':').strip()
                parsed[key] = content
                break

    # Build HTML sections
    html_sections = ""
    for i, key in enumerate(section_order):
        if key == "NEWS & CULTURE" and not (headlines and news_url):
            continue                        # nothing to point at yet: no section, and no numeral used up
        label = section_labels_display[i]
        roman = next(romans)
        content = parsed.get(key, "")

        if key == "MTA THIS WEEKEND":
            # MTA is rendered from structured alert data, not Claude prose:
            # a home-station status banner plus a single A/C/3 change list.
            mta_html = build_mta_section_html(mta_alerts, surface_alerts)
            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          {mta_html}
        </div>"""
        elif key == "AROUND BROOKLYN":
            # Parse grouped [SOURCE] blocks into styled cards
            import re as _re
            items_html = ""
            # Split on lines that are just [SOURCE NAME]
            source_blocks = _re.split(r'\n(?=\[[^\]]+\]\s*\n)', content.strip())
            for block in source_blocks:
                block = block.strip()
                if not block:
                    continue
                # Extract source name from first line
                source_match = _re.match(r'^\[([^\]]+)\]\s*\n(.*)', block, _re.DOTALL)
                if source_match:
                    source = source_match.group(1).strip()
                    rest = source_match.group(2).strip()
                    # Each remaining line is a headline + blurb
                    headline_lines = [l.strip() for l in rest.split('\n') if l.strip()]
                    headlines_html = ""
                    for hl in headline_lines:
                        # Split on " — " to separate link from blurb
                        parts = hl.split(' — ', 1)
                        link_part = parts[0].strip()
                        blurb = parts[1].strip() if len(parts) > 1 else ""
                        headlines_html += f"""
              <div class="news-headline-row">
                <div class="news-hed">{link_part}</div>
                {"<div class='news-dek'>" + blurb + "</div>" if blurb else ""}
              </div>"""
                    items_html += f"""
          <div class="news-item">
            <div class="news-source">{source}</div>
            {headlines_html}
          </div>"""
                else:
                    # Fallback: try old single-line [SOURCE] format
                    old_match = _re.match(r'^\[([^\]]+)\]\s*(.*)', block, _re.DOTALL)
                    if old_match:
                        source = old_match.group(1).strip()
                        rest = old_match.group(2).strip()
                        lines = rest.split('\n', 1)
                        headline_html = lines[0].strip()
                        blurb = lines[1].strip() if len(lines) > 1 else ""
                        items_html += f"""
          <div class="news-item">
            <div class="news-source">{source}</div>
            <div class="news-hed">{headline_html}</div>
            {"<div class='news-dek'>" + blurb + "</div>" if blurb else ""}
          </div>"""
                    else:
                        items_html += f"<p>{block}</p>"

            # Edible Brooklyn subsection — quoted from the feed, not Claude.
            import html as _html
            edible_html = ""
            for e in (edible_picks or []):
                eyebrow = f"{_html.escape(e['area'])} &middot; {e['date'].strftime('%b %-d')}"
                hed = f'<a href="{_html.escape(e["url"])}">{_html.escape(e["title"])}</a>'
                dek = _html.escape(e["dek"])
                edible_html += f"""
          <div class="news-item">
            <div class="news-source">{eyebrow}</div>
            <div class="news-headline-row">
              <div class="news-hed">{hed}</div>
              {"<div class='news-dek'>" + dek + "</div>" if dek else ""}
            </div>
          </div>"""
            if edible_html:
                items_html += f"""
          <div class="news-subhead">From Edible Brooklyn</div>{edible_html}"""
            items_html += around_extra_html      # At the cinema (culture_listings); empty when unreadable

            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          <div class="news-feed">{items_html}</div>
        </div>"""
        elif key == "ON SALE THIS WEEKEND":
            # Rendered straight from DICE's JSON — no Claude prose. Every value
            # below is quoted from the source; the headline links to the ticket
            # page so any claim can be checked in one click. Five picks total
            # across tonight and the following Friday; the rest live on the
            # companion page linked underneath.
            import html as _html
            items_html = ""
            # The DICE picks plus any Public Records show, in the order they start.
            for e in on_sale_lineup(dice_picks, pr_events):
                eyebrow_bits = [
                    e["start"].strftime("%a %-d %b &middot; %-I:%M %p") if e.get("has_time", True)
                    else e["start"].strftime("%a %-d %b"),
                    e.get("tracked_venue") or e.get("venue", ""),
                ]
                eyebrow = " &middot; ".join(b for b in eyebrow_bits if b)

                hed_text = _html.escape(e["name"])
                url = e.get("url", "")
                hed = f'<a href="{_html.escape(url)}">{hed_text}</a>' if url else hed_text

                dek_bits = []
                if e.get("public_records"):
                    # Public Records' own description, word for word ("11:00 pm @ Sound Room ...")
                    if e.get("dek"):
                        dek_bits.append(e["dek"])
                else:
                    # Skip artists the headline already names, so the dek adds
                    # information instead of echoing the title.
                    name_lower = e["name"].lower()
                    artists = [
                        a for a in e.get("artists", [])
                        if a and a.lower() not in name_lower
                    ]
                    if artists:
                        dek_bits.append(", ".join(artists[:3]))
                    if e.get("price"):
                        dek_bits.append(e["price"])
                    if e.get("sold_out"):
                        dek_bits.append("Sold out")
                dek = " &middot; ".join(_html.escape(b) for b in dek_bits)

                items_html += f"""
          <div class="news-item">
            <div class="news-source">{eyebrow}</div>
            <div class="news-headline-row">
              <div class="news-hed">{hed}</div>
              {"<div class='news-dek'>" + dek + "</div>" if dek else ""}
            </div>
          </div>"""
            if not items_html:
                items_html = "<p>Nothing on the watchlist for either night.</p>"
            if long_list_url:
                items_html += f"""
          <p style="margin-top:14px;"><a href="{_html.escape(long_list_url)}">The long list &mdash; every room, every night &rarr;</a></p>"""
            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          <div class="news-feed">{items_html}</div>
        </div>"""
        elif key == "NEWS & CULTURE":
            # The way into the separate News & Culture page: the five newest
            # headlines across its sections (one per section), quoted from their
            # sources, then the link. Left out when that page has nothing yet.
            if not (headlines and news_url):
                continue
            import html as _html
            items_html = ""
            for h in headlines:
                eyebrow = " &middot; ".join(b for b in (
                    _html.escape(h["category"]), _html.escape(h["source"]),
                    h["date"].strftime("%b %-d") if h.get("date") else "") if b)
                dek = _html.escape(h.get("dek", ""))
                items_html += f"""
          <div class="news-item">
            <div class="news-source">{eyebrow}</div>
            <div class="news-headline-row">
              <div class="news-hed"><a href="{_html.escape(h['url'])}">{_html.escape(h['title'])}</a></div>
              {"<div class='news-dek'>" + dek + "</div>" if dek else ""}
            </div>
          </div>"""
            items_html += f"""
          <p style="margin-top:14px;"><a href="{_html.escape(news_url)}">News &amp; Culture &rarr;</a></p>"""
            html_sections += news_feature_html(roman, news_url, items_html)
        else:
            paras = [p.strip() for p in content.split("\n\n") if p.strip()]
            paras_html = "".join(f"<p>{p}</p>" for p in paras) if paras else f"<p>{content}</p>"
            if key == "WEATHER + BISCUIT":
                paras_html += build_sun_tide_html(saturday, sunday)   # sun, golden hour and tides under the forecast
                paras_html += dm.radar_html()      # live NWS radar loop under the forecast
            elif key == "SAMPLE GREENMARKET":
                paras_html += rcp.build_recipe_html(saturday, _SEASONAL_PRODUCE.get(saturday.month, []))  # a real recipe on what's in season
            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          {paras_html}
        </div>"""

    # The Nature Report holds Bird, Tree and Map of the Week as sub-sections; Then & Now and Shore Leave
    # follow as sections of their own, then the MTA Weekender closes the page.
    nature_html = ""
    nature_body = build_nature_report_html(inat_html, nature)
    if nature_body:
        nature_html += _nature_section("Nature Report", next(romans), nature_body)
    for key in ("then_now", "shore"):
        piece = (nature or {}).get(key)
        if piece:
            nature_html += _nature_section(piece[0], next(romans), piece[1])
    # MTA Weekender closes the page: where subway trains are not running, then three car-free trips.
    if weekender_html:
        nature_html += _nature_section("MTA Weekender", next(romans), weekender_html)

    pigeon_defs = f"""<defs>
      <image id="pp" href="{PIGEON_GREY_URL}" width="60" height="52"/>
    </defs>"""

    pigeon_uses = """
    <use href="#pp" transform="translate(30,30) scale(0.55)" opacity="0.11"/>
    <use href="#pp" transform="translate(185,20) scale(0.45)" opacity="0.10"/>
    <use href="#pp" transform="translate(360,35) scale(0.6)" opacity="0.11"/>
    <use href="#pp" transform="translate(510,15) scale(0.5)" opacity="0.10"/>
    <use href="#pp" transform="translate(80,185) scale(0.5)" opacity="0.10"/>
    <use href="#pp" transform="translate(270,195) scale(0.55)" opacity="0.11"/>
    <use href="#pp" transform="translate(450,180) scale(0.45)" opacity="0.10"/>
    <use href="#pp" transform="translate(20,345) scale(0.6)" opacity="0.11"/>
    <use href="#pp" transform="translate(200,355) scale(0.48)" opacity="0.10"/>
    <use href="#pp" transform="translate(390,340) scale(0.55)" opacity="0.11"/>
    <use href="#pp" transform="translate(530,360) scale(0.44)" opacity="0.10"/>
    <use href="#pp" transform="translate(100,510) scale(0.52)" opacity="0.10"/>
    <use href="#pp" transform="translate(310,520) scale(0.58)" opacity="0.11"/>
    <use href="#pp" transform="translate(480,505) scale(0.46)" opacity="0.10"/>
    <use href="#pp" transform="translate(40,665) scale(0.56)" opacity="0.11"/>
    <use href="#pp" transform="translate(230,675) scale(0.5)" opacity="0.10"/>
    <use href="#pp" transform="translate(430,660) scale(0.54)" opacity="0.11"/>
    <use href="#pp" transform="translate(545,680) scale(0.42)" opacity="0.10"/>
    <use href="#pp" transform="translate(120,825) scale(0.53)" opacity="0.10"/>
    <use href="#pp" transform="translate(340,835) scale(0.57)" opacity="0.11"/>
    <use href="#pp" transform="translate(510,820) scale(0.47)" opacity="0.10"/>
    <use href="#pp" transform="translate(25,985) scale(0.58)" opacity="0.11"/>
    <use href="#pp" transform="translate(200,995) scale(0.5)" opacity="0.10"/>
    <use href="#pp" transform="translate(390,980) scale(0.55)" opacity="0.11"/>
    <use href="#pp" transform="translate(540,1000) scale(0.44)" opacity="0.10"/>
    <use href="#pp" transform="translate(85,1140) scale(0.52)" opacity="0.10"/>
    <use href="#pp" transform="translate(290,1150) scale(0.56)" opacity="0.11"/>
    <use href="#pp" transform="translate(470,1135) scale(0.48)" opacity="0.10"/>
    <use href="#pp" transform="translate(35,1295) scale(0.54)" opacity="0.11"/>
    <use href="#pp" transform="translate(215,1305) scale(0.5)" opacity="0.10"/>
    <use href="#pp" transform="translate(415,1290) scale(0.57)" opacity="0.11"/>
    <use href="#pp" transform="translate(545,1310) scale(0.43)" opacity="0.10"/>
    <use href="#pp" transform="translate(100,1430) scale(0.5)" opacity="0.09"/>
    <use href="#pp" transform="translate(310,1435) scale(0.46)" opacity="0.09"/>
    <use href="#pp" transform="translate(490,1425) scale(0.52)" opacity="0.09"/>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&family=Special+Elite&display=swap" rel="stylesheet">
<style>
  body {{ margin: 0; padding: 16px; background: #EDE0C0; }}
  .b4-wrap {{
    max-width: 600px;
    margin: 0 auto;
    background: #F5EDD8;
    color: #1A1208;
    font-family: 'Cormorant Garamond', Georgia, serif;
    border: 1px solid #D4B98A;
    position: relative;
    overflow: hidden;
  }}
  .pigeons-global {{
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    width: 100%;
    height: 100%;
  }}
  .b4-header {{
    padding: 24px 32px 0;
    background: #EDE0C0;
    position: relative;
  }}
  .b4-eyebrow {{
    font-family: 'Courier Prime', monospace;
    font-size: 8.5px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #8A6A3A;
    margin-bottom: 8px;
    position: relative;
    z-index: 1;
    display: flex;
    justify-content: space-between;
  }}
  .b4-title-row {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
    position: relative;
    z-index: 1;
  }}
  .b4-title {{
    font-family: 'Cormorant Garamond', Georgia, serif;
    font-weight: 300;
    font-style: italic;
    font-size: 58px;
    line-height: 0.9;
    color: #1A1208;
    margin: 0;
    letter-spacing: -0.01em;
  }}
  .b4-title em {{ color: #9B3A1A; font-style: normal; }}
  .b4-date {{
    font-family: 'Courier Prime', monospace;
    font-size: 9.5px;
    letter-spacing: 0.14em;
    color: #8A6A3A;
    line-height: 1.9;
    text-transform: uppercase;
    text-align: right;
    position: relative;
    z-index: 1;
  }}
  .b4-rule {{ width: 32px; height: 2px; background: #9B3A1A; margin: 10px 0 0; position: relative; z-index: 1; }}
  .b4-header-bottom-rule {{ height: 1px; background: #C4A46A; margin-top: 12px; position: relative; z-index: 1; }}
  .b4-body {{ padding: 0 32px 28px; position: relative; z-index: 1; }}
  .b4-section {{ padding: 20px 0 18px; border-bottom: 1px solid #D4B98A; }}
  .b4-section:last-child {{ border-bottom: none; }}
  .b4-label-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }}
  .b4-label {{ font-family: 'Courier Prime', monospace; font-size: 8px; letter-spacing: 0.26em; text-transform: uppercase; color: #9B3A1A; white-space: nowrap; }}
  .b4-label-rule {{ flex: 1; height: 1px; background: #D4B98A; }}
  .b4-label-roman {{ font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-size: 12px; color: #B89A5A; }}
  .b4-section p {{ font-family: 'Cormorant Garamond', Georgia, serif; font-size: 16.5px; line-height: 1.72; color: #2A1E0A; margin: 0 0 12px; font-weight: 400; }}
  .b4-section p:last-child {{ margin-bottom: 0; }}
  .b4-section a {{ color: #9B3A1A; }}
  .news-feed {{ display: flex; flex-direction: column; gap: 0; }}
  .news-item + .news-item {{ border-top: 1px solid #D4B98A; padding-top: 16px; margin-top: 4px; }}
  .mta-banner {{ font-family: 'Cormorant Garamond', Georgia, serif; font-size: 16px; line-height: 1.6; color: #2A1E0A; margin: 0 0 16px; padding: 12px 16px; background: #EFE3C3; overflow-wrap: break-word; word-break: break-word; }}
  .mta-banner strong {{ color: #9B3A1A; font-weight: 600; }}
  .mta-banner--clear {{ border-left: 3px solid #C4A46A; }}
  .mta-banner--alert {{ border-left: 3px solid #9B3A1A; }}
  .mta-group {{ margin-bottom: 20px; }}
  .mta-group:last-child {{ margin-bottom: 0; }}
  .mta-group-label {{ font-family: 'Courier Prime', monospace; font-size: 8px; letter-spacing: 0.22em; text-transform: uppercase; color: #9B3A1A; margin-bottom: 12px; }}
  .news-item {{ padding: 12px 0 16px 14px; border-left: 2px solid #9B3A1A; margin-bottom: 16px; }}
  .news-item:last-child {{ margin-bottom: 0; }}
  .news-subhead {{ font-family: 'Courier Prime', monospace; font-size: 8px; letter-spacing: 0.26em; text-transform: uppercase; color: #9B3A1A; margin: 8px 0 14px; padding-top: 16px; border-top: 1px solid #D4B98A; }}
  .news-source {{ font-family: 'Courier Prime', monospace; font-size: 8px; letter-spacing: 0.22em; text-transform: uppercase; color: #B89A5A; margin-bottom: 8px; }}
  .news-headline-row {{ margin-bottom: 8px; }}
  .news-headline-row:last-child {{ margin-bottom: 0; }}
  .news-hed {{ font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 600; font-size: 15px; line-height: 1.3; color: #1A1208; margin-bottom: 5px; overflow-wrap: break-word; word-break: break-word; }}
  .news-hed a {{ color: #1A1208; text-decoration: none; border-bottom: 1px solid #D4B98A; }}
  .news-hed a:hover {{ color: #9B3A1A; border-bottom-color: #9B3A1A; }}
  .news-dek {{ font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-size: 13.5px; color: #6A5030; line-height: 1.55; }}
  .b4-footer {{
    background: #1A1208;
    padding: 16px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: relative;
    z-index: 1;
  }}
  .b4-footer-left {{ display: flex; flex-direction: column; gap: 3px; }}
  .b4-crest {{ display: block; width: 88px; height: 88px; flex: none; margin-left: 16px; }}
  @media (max-width: 520px) {{ .b4-crest {{ width: 64px; height: 64px; }} }}
  .b4-footer-eyebrow {{ font-family: 'Courier Prime', monospace; font-size: 7.5px; letter-spacing: 0.16em; text-transform: uppercase; color: #9B8A5A; }}
  .b4-signature {{ font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-size: 20px; color: #D4B98A; line-height: 1; margin-top: 2px; }}
  .b4-tagline {{ font-family: 'Courier Prime', monospace; font-size: 7px; letter-spacing: 0.04em; text-transform: uppercase; color: #9B8A5A; margin-top: 2px; white-space: nowrap; }}
{NATURE_CSS}{dp.INAT_CSS}{mw.MTA_CSS}{dm.MOTION_CSS}{DESIGN_CSS}{NC_FEATURE_CSS}</style>
</head>
<body>
<div class="b4-wrap">

  <svg class="pigeons-global" viewBox="0 0 600 1500" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg">
    {pigeon_defs}
    {pigeon_uses}
  </svg>

  <div class="b4-header">
    <div class="b4-eyebrow">
      <span>Weekend Dispatch</span>
      <span>Fort Greene &middot; Brooklyn</span>
    </div>
    <div class="b4-title-row">
      <h1 class="b4-title">The<br><em>Week</em>end.</h1>
      <div class="b4-date">
        Saturday<br>{sat_date_top}<br>&mdash;&mdash;<br>Sunday<br>{sun_date_top}<br>{year}
      </div>
    </div>
    <div class="b4-rule"></div>
    <div class="b4-header-bottom-rule"></div>
    {dm.pigeon_walk_html(PIGEON_GREY_URL)}
  </div>

  <div class="b4-body">
    {html_sections}
    {nature_html}
  </div>

  <div class="b4-footer">
    <div class="b4-footer-left">
      <span class="b4-footer-eyebrow">Fort Greene &middot; Brooklyn, N.Y. &middot; {year}</span>
      <span class="b4-signature">Alex Sample</span>
      <span class="b4-tagline">A personal dispatch &mdash; written by machines, curated by instinct.</span>
    </div>
    <img class="b4-crest" src="{ZT_CREST_URL}" width="88" height="88" alt="sample crest">
  </div>

</div>
</body>
</html>"""
    return html


# The Long List's stylesheet: a risograph show-flyer zine. Newsprint ground, an electric-yellow masthead with
# a hot-pink offset shadow, cobalt date banners, 2px ink borders, and a three-column grid of flyer cards.
# The cards for shows that made the email (.is-pick) get a blush fill, a pink shadow and a badge.
# Light is the default; a dark variant swaps the tokens under prefers-color-scheme. No JavaScript.
# A raw string, so nothing in the CSS is read as a Python escape (and no doubled braces).
LONG_LIST_CSS = r"""
  :root {
    --paper:#F7F3E8; --card:#FFFDF6; --pick:#FFF0F5;
    --ink:#16141A; --muted:#575160; --stroke:#16141A; --rule:rgba(22,20,26,.3); --dot:rgba(22,20,26,.07);
    --yellow:#FFE600; --pink:#FF2A6D; --cobalt:#0057FF; --time:#0057FF; --cobalt-deep:#0040C8;
    --on-yellow:#16141A; --on-pink:#16141A; --on-cobalt:#FFFFFF;
    --btn-bg:#16141A; --btn-fg:#FFE600; --focus:#0057FF;
    --serif:'Cormorant Garamond',Georgia,'Times New Roman',serif;
    --mono:'Courier Prime','Courier New',ui-monospace,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper:#121016; --card:#1D1A24; --pick:#2B1527;
      --ink:#F7F3E8; --muted:#BDB6C8; --stroke:#F7F3E8; --rule:rgba(247,243,232,.32); --dot:rgba(247,243,232,.06);
      --cobalt:#2563EB; --time:#8CB0FF;
      --btn-bg:#FFE600; --btn-fg:#16141A; --focus:#FFE600;
    }
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--paper); color:var(--ink);
         background-image:radial-gradient(var(--dot) 1.1px, transparent 1.3px); background-size:14px 14px;
         font-family:var(--serif); font-size:18px; line-height:1.5; -webkit-font-smoothing:antialiased; }
  .wrap { max-width:960px; margin:0 auto; padding:24px 16px 72px; }
  a { color:inherit; }
  a:focus-visible { outline:3px solid var(--focus); outline-offset:3px; }

  /* masthead: the flyer */
  .masthead { position:relative; overflow:hidden; background:var(--yellow); color:var(--on-yellow);
              border:2px solid var(--on-yellow); box-shadow:8px 8px 0 var(--pink); padding:20px 20px 30px; }
  .masthead::after { content:""; position:absolute; right:-36px; top:-36px; width:230px; height:230px; border-radius:50%;
                     background-image:radial-gradient(var(--on-yellow) 1.7px, transparent 2px); background-size:12px 12px;
                     opacity:.2; pointer-events:none; }
  .masthead > * { position:relative; z-index:1; }
  .kicker { margin:0 0 6px; font-family:var(--mono); font-weight:700; font-size:12px; letter-spacing:.2em; text-transform:uppercase; }
  .kicker a { display:inline-flex; align-items:center; min-height:44px; text-decoration:underline;
              text-decoration-thickness:2px; text-underline-offset:4px; }
  .kicker a:hover { background:var(--on-yellow); color:var(--yellow); text-decoration:none; padding:0 6px; margin:0 -6px; }
  .masthead a:focus-visible { outline-color:var(--on-yellow); }
  .masthead h1 { margin:0; font-weight:700; font-size:clamp(42px,11vw,80px); line-height:.96; letter-spacing:-.01em; text-wrap:balance; }
  .masthead h1 em { display:block; margin-top:6px; font-style:italic; font-weight:600; color:var(--cobalt-deep);
                    font-size:.62em; line-height:1.05; letter-spacing:0; }
  .standfirst { margin:16px 0 0; max-width:58ch; font-style:italic; font-weight:500; font-size:clamp(18px,3.6vw,22px); line-height:1.4; }

  /* section labels */
  .seclabel { display:flex; align-items:center; gap:12px; margin:54px 0 22px; font-size:inherit; font-weight:400; }
  .seclabel .sec-name { font-family:var(--mono); font-weight:700; font-size:13px; letter-spacing:.2em; text-transform:uppercase;
                        padding:7px 12px; background:var(--btn-bg); color:var(--btn-fg); }
  .seclabel .bar { flex:1; min-width:12px; height:0; border-top:2px solid var(--stroke); }
  .seclabel .rom { font-style:italic; font-weight:600; font-size:20px; color:var(--muted); }

  /* date banners */
  .date-group + .date-group { margin-top:38px; }
  .date-banner { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px 14px;
                 margin:0 0 24px; padding:10px 16px; background:var(--cobalt); color:var(--on-cobalt);
                 border:2px solid var(--stroke); font-weight:400; }
  .is-headline .date-banner { box-shadow:6px 6px 0 var(--yellow); }
  .date-name { font-style:italic; font-weight:700; font-size:clamp(23px,5.4vw,30px); line-height:1.1; }
  .daytag { font-family:var(--mono); font-weight:700; font-size:11px; letter-spacing:.14em; text-transform:uppercase;
            padding:5px 9px; background:var(--yellow); color:var(--on-yellow); border:2px solid var(--on-yellow); }

  /* the flyer grid */
  .event-grid { list-style:none; margin:0; padding:0; display:grid; gap:24px 18px; grid-template-columns:repeat(3,minmax(0,1fr)); }
  .event-card { position:relative; display:flex; flex-direction:column; gap:6px; min-width:0; padding:18px 16px 16px;
                background:var(--card); border:2px solid var(--stroke); }
  .event-card.is-pick { background:var(--pick); box-shadow:6px 6px 0 var(--pink); }
  .pick-badge { position:absolute; top:-14px; right:10px; transform:rotate(2deg); padding:6px 8px; line-height:1;
                font-family:var(--mono); font-weight:700; font-size:11px; letter-spacing:.12em; text-transform:uppercase;
                background:var(--pink); color:var(--on-pink); border:2px solid var(--stroke); }
  .event-time { margin:0; font-family:var(--mono); font-weight:700; font-size:14px; letter-spacing:.04em; color:var(--time); }
  .event-title { margin:0; font-weight:700; font-size:24px; line-height:1.1; color:var(--ink); overflow-wrap:anywhere; }
  .event-title a { display:flex; align-items:center; min-height:44px; padding:4px 0; color:inherit;
                   text-decoration:underline; text-decoration-thickness:2px; text-underline-offset:4px;
                   text-decoration-color:var(--pink); }
  .event-title a:hover { background:var(--yellow); color:var(--on-yellow); text-decoration-color:var(--on-yellow); }
  .event-title .event-name { display:block; padding:4px 0; }
  .event-venue { margin:0; font-family:var(--mono); font-size:13px; letter-spacing:.02em; color:var(--muted); }
  .event-price { align-self:flex-start; display:inline-block; padding:2px 8px; font-family:var(--mono); font-weight:700; font-size:13px;
                 background:var(--yellow); color:var(--on-yellow); border:2px solid var(--on-yellow); }
  .event-actions { margin-top:auto; padding-top:10px; }
  .dice-btn { display:inline-flex; align-items:center; justify-content:center; min-height:44px; padding:0 16px;
              font-family:var(--mono); font-weight:700; font-size:12.5px; letter-spacing:.08em; text-transform:uppercase;
              text-decoration:none; background:var(--btn-bg); color:var(--btn-fg); border:2px solid var(--btn-bg); }
  .dice-btn span { margin-left:.55em; }
  .dice-btn:hover { background:var(--pink); color:var(--on-pink); border-color:var(--stroke); }
  .event-soldout { display:inline-flex; align-items:center; min-height:44px; padding:0 12px;
                   font-family:var(--mono); font-weight:700; font-size:12.5px; letter-spacing:.08em; text-transform:uppercase;
                   color:var(--muted); border:2px dashed var(--stroke); }
  .rempty { grid-column:1/-1; list-style:none; padding:14px 16px; font-style:italic; font-size:18px; color:var(--muted);
            border:2px dashed var(--stroke); }

  /* the rooms */
  .roomgrid { display:grid; align-items:start; gap:24px 20px; grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr)); }
  .room { min-width:0; padding:16px 18px 14px; background:var(--card); border:2px solid var(--stroke); }
  .room--live { box-shadow:6px 6px 0 var(--cobalt); }
  .room--quiet, .room--unlisted { background:transparent; border-style:dashed; }
  .roomhead { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px 10px; }
  .roomhead h3 { margin:0; font-weight:700; font-size:27px; line-height:1.05; color:var(--ink); }
  .pill { padding:4px 8px; font-family:var(--mono); font-weight:700; font-size:11px; letter-spacing:.1em; text-transform:uppercase;
          white-space:nowrap; border:2px solid var(--stroke); }
  .pill--live { background:var(--cobalt); color:var(--on-cobalt); }
  .pill--quiet { background:var(--yellow); color:var(--on-yellow); border-color:var(--on-yellow); }
  .pill--unlisted { border-style:dashed; color:var(--muted); }
  .roomwhere { margin:6px 0 10px; font-family:var(--mono); font-size:12.5px; letter-spacing:.04em; color:var(--muted); }
  .room-rows { list-style:none; margin:0; padding:0; }
  .room-row { display:grid; grid-template-columns:84px minmax(0,1fr); gap:0 12px; align-items:start; padding:6px 0 6px 12px;
              border-top:2px dashed var(--rule); }
  .room-row:first-child { border-top:0; }
  .room-row.is-pick { background:var(--pick); box-shadow:inset 5px 0 0 var(--pink); }
  .room-when { padding-top:14px; font-family:var(--mono); font-weight:700; font-size:12.5px; line-height:1.3; color:var(--time); }
  .room-when small { display:block; font-size:12px; font-weight:400; color:var(--muted); }
  .room-main { min-width:0; }
  .room-row .event-title { display:block; font-size:21px; }
  .room-meta { display:flex; flex-wrap:wrap; align-items:center; gap:6px 8px; padding-bottom:6px;
               font-family:var(--mono); font-size:12.5px; color:var(--muted); }
  .room-meta:empty { display:none; }
  .room-meta .pick-badge { position:static; transform:none; }
  .room-meta .event-soldout { min-height:0; padding:2px 8px; font-size:11px; }
  .room .rempty { margin:0 0 4px; }

  .foot { margin-top:60px; padding-top:16px; border-top:2px solid var(--stroke); display:flex; flex-wrap:wrap; gap:8px 14px;
          justify-content:space-between; font-family:var(--mono); font-size:12px; letter-spacing:.08em; text-transform:uppercase;
          color:var(--muted); }

  @media (max-width:840px) {
    .event-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  }
  @media (max-width:560px) {
    .wrap { padding:18px 16px 56px; }
    .masthead { padding:14px 16px 24px; box-shadow:6px 6px 0 var(--pink); }
    .masthead::after { right:-30px; top:-30px; width:140px; height:140px; opacity:.16; }
    .kicker { font-size:11.5px; letter-spacing:.1em; }
    .event-grid { grid-template-columns:1fr; gap:26px; }
    .dice-btn { width:100%; }
    .seclabel { margin-top:44px; }
    .room-row { grid-template-columns:76px minmax(0,1fr); gap:0 10px; }
  }
"""


def build_long_list_html(watchlist, sweep_events, saturday, friday, picked_urls=()):
    """
    The companion page: everything the email had no room for.

    Scoped the way Alex actually goes out — Fridays and Saturdays only, rolling
    two weeks forward from the Saturday this was built on. Four nights, no
    weekday filler, no stale tail.

    Two passes over the same data, because they answer different questions.
    The ledger comes first — every tracked show in date order, one flyer card
    each, with the five that made the email picked out in pink so it's clear
    what was left out. Then the rooms, one card per venue, for when the
    question is "what's on at Xanadu" rather than "what's on Friday".
    """
    import html as _html

    window = dice_going_out_dates(saturday)
    window_set = set(window)

    def esc(s):
        return _html.escape(str(s or ""))

    def is_pick(e):
        return bool(e.get("url") and e["url"] in picked_urls)

    def title_link(e):
        # the title as the show's own words, linked when DICE gave us an address
        name = esc(e["name"])
        if e.get("url"):
            return f'<a href="{esc(e["url"])}">{name}</a>'
        return f'<span class="event-name">{name}</span>'

    def event_card(e):
        """One flyer. Time, title, room, price, and a way to buy."""
        pick = is_pick(e)
        name = esc(e["name"])
        badge = '<span class="pick-badge">In The Email</span>' if pick else ""
        when = e["start"].strftime("%-I:%M %p").lower()
        room = esc(e.get("tracked_venue") or e.get("venue"))
        venue = f'<p class="event-venue">{room}</p>' if room else ""
        price = f'<span class="event-price">{esc(e["price"])}</span>' if e.get("price") else ""
        if e.get("sold_out"):
            action = '<span class="event-soldout">Sold out</span>'
        elif e.get("url"):
            # the button's name carries the show, so a screen reader can tell eleven of them apart
            action = (f'<a class="dice-btn" href="{esc(e["url"])}" aria-label="Tickets on DICE: {name}">'
                      'Tickets on DICE<span aria-hidden="true">&rarr;</span></a>')
        else:
            action = ""
        actions = f'<div class="event-actions">{action}</div>' if action else ""
        pick_class = " is-pick" if pick else ""
        return f"""
          <li class="event-card{pick_class}">
            {badge}
            <p class="event-time">{when}</p>
            <h4 class="event-title">{title_link(e)}</h4>
            {venue}
            {price}
            {actions}
          </li>"""

    def room_row(e):
        """A compact line inside a room's own card: date and time, title, then the small print."""
        pick = is_pick(e)
        bits = []
        if pick:
            bits.append('<span class="pick-badge">In The Email</span>')
        if e.get("price"):
            bits.append(f'<span class="event-price">{esc(e["price"])}</span>')
        if e.get("sold_out"):
            bits.append('<span class="event-soldout">Sold out</span>')
        joined = "".join(bits)
        meta = f'<span class="room-meta">{joined}</span>'
        day = e["start"].strftime("%a %-d %b")
        clock = e["start"].strftime("%-I:%M %p").lower()
        pick_class = " is-pick" if pick else ""
        return f"""
          <li class="room-row{pick_class}">
            <span class="room-when">{day}<small>{clock}</small></span>
            <span class="room-main"><span class="event-title">{title_link(e)}</span>{meta}</span>
          </li>"""

    def night_groups(days, rows_for, tag_for=None):
        out = ""
        for d in days:
            tag = tag_for(d) if tag_for else ""
            headline = ' is-headline' if tag else ""
            cards = "".join(event_card(r) for r in rows_for(d))
            if not cards:
                cards = '<li class="rempty">Nothing booked at the tracked rooms.</li>'
            out += f"""
      <section class="date-group{headline}">
        <h3 class="date-banner"><span class="date-name">{d.strftime('%A, %B %-d')}</span>{tag}</h3>
        <ul class="event-grid">{cards}</ul>
      </section>"""
        return out

    # ── rooms ────────────────────────────────────────────────────────────
    rooms_html = ""
    for label, entry in watchlist["venues"].items():
        state = entry["state"]
        in_window = [e for e in entry["events"] if e["date"] in window_set]
        # Count what's in the window, not the room's whole run — "30 upcoming"
        # is a lie when only three of them fall on a night Alex goes out.
        if state == "live" and not in_window:
            state = "quiet"
        pill = {"live": f"{len(in_window)} this fortnight",
                "quiet": "nothing this fortnight",
                "unlisted": "not readable"}[state]
        where = ""
        addr_parts = [p.strip() for p in (entry.get("address") or "").split(",")]
        if len(addr_parts) > 2:
            where = ", ".join(addr_parts[1:3])
        rows = "".join(room_row(e) for e in in_window)
        if not rows:
            rows = f'<li class="rempty">{esc(entry.get("note") or "Nothing on a Friday or Saturday in the next fortnight.")}</li>'
        rooms_html += f"""
      <article class="room room--{state}">
        <header class="roomhead">
          <h3>{esc(label)}</h3><span class="pill pill--{state}">{esc(pill)}</span>
        </header>
        <p class="roomwhere">{esc(where)}</p>
        <ul class="room-rows">{rows}</ul>
      </article>"""

    for label, entry in watchlist["artists"].items():
        rows = ""
        for e in entry["events"][:5]:
            city = ""
            parts = [p.strip() for p in (e.get("address") or "").split(",")]
            if len(parts) > 2:
                city = parts[-3]
            city_html = f'<span class="room-meta">{esc(city)}</span>' if city else ""
            rows += f"""
          <li class="room-row">
            <span class="room-when">{e['start'].strftime('%-d %b')}</span>
            <span class="room-main"><span class="event-title"><a href="{esc(e['url'])}">{esc(e['venue'])}</a></span>{city_html}</span>
          </li>"""
        if not rows:
            rows = f'<li class="rempty">{esc(entry.get("note") or "No dates announced.")}</li>'
        rooms_html += f"""
      <article class="room room--{entry['state']} room--artist">
        <header class="roomhead">
          <h3>{esc(label)}</h3>
          <span class="pill pill--{entry['state']}">artist watch</span>
        </header>
        <p class="roomwhere">Following</p>
        <ul class="room-rows">{rows}</ul>
      </article>"""

    # ── ledger ───────────────────────────────────────────────────────────
    all_rows = []
    for label, entry in watchlist["venues"].items():
        for e in entry["events"]:
            all_rows.append({**e, "tracked_venue": label})
    all_rows.sort(key=lambda r: (r["start"], r["tracked_venue"]))

    by_date = {}
    for r in all_rows:
        if r["date"] in window_set:
            by_date.setdefault(r["date"], []).append(r)

    # Walk the window rather than the data, so a night with nothing on it still
    # gets a banner. A missing Friday should read as "nothing booked", not as
    # a night that quietly didn't exist.
    ledger_html = night_groups(
        window,
        lambda d: by_date.get(d, []),
        lambda d: '<span class="daytag">In the email</span>' if d in (saturday, friday) else "")

    # ── the wider sweep ──────────────────────────────────────────────────
    sweep_by_date = {}
    for e in (sweep_events or []):
        sweep_by_date.setdefault(e["date"], []).append(e)
    sweep_html = night_groups(sorted(sweep_by_date), lambda d: sweep_by_date[d])
    if not sweep_html:
        sweep_html = ('<p class="rempty">Nothing else within five miles on these '
                      'nights. DICE\'s browse pages only see a few days ahead, so '
                      'this section fills in closer to the weekend.</p>')

    generated = datetime.datetime.now().strftime("%-d %B %Y, %-I:%M %p")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex, nofollow">
<title>The Long List &middot; Weekend Dispatch</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;0,600;0,700;1,500;1,600;1,700&family=Courier+Prime:wght@400;700&display=swap" rel="stylesheet">
<style>{LONG_LIST_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <p class="kicker"><a href="weekend.html">Weekend Dispatch</a> &middot; Companion Page</p>
    <h1>The Long List<em>every room, every night</em></h1>
    <p class="standfirst">The five picks in the email came from here. This is the
      rest of it &mdash; every Friday and Saturday at the tracked rooms between
      now and {esc(window[-1].strftime('%-d %B'))}, plus whatever else DICE has
      within five miles of Fort Greene.</p>
  </header>

  <h2 class="seclabel"><span class="sec-name">The Ledger</span><span class="bar"></span><span class="rom">i.</span></h2>
  <div class="ledger">{ledger_html}
  </div>

  <h2 class="seclabel"><span class="sec-name">The Rooms</span><span class="bar"></span><span class="rom">ii.</span></h2>
  <div class="roomgrid">{rooms_html}
  </div>

  <h2 class="seclabel"><span class="sec-name">Elsewhere in Brooklyn</span><span class="bar"></span><span class="rom">iii.</span></h2>
  <div class="ledger">{sweep_html}
  </div>

  <div class="foot">
    <span>Fort Greene &middot; Brooklyn</span>
    <span>Source: DICE &middot; generated {esc(generated)}</span>
  </div>
</div>
</body>
</html>"""



def build_chco_html(issue):
    """
    The Fort Greene Community News companion page: the issue's own table of
    contents as a two-column broadsheet, each story linking to its page of the
    PDF. Headlines, bylines and page numbers are quoted from the printed
    contents list; the one-line summaries (when present) are machine-written
    and labelled as such. Volume/number, page count, byline and summary are each
    left out cleanly when the data doesn't have them.
    """
    import html as _html

    def esc(s):
        return _html.escape(str(s or ""))

    arts = issue["articles"]
    pdf = issue["pdf_url"]
    month_name = issue["month"].split()[0]
    vol, num = issue.get("volume"), issue.get("number")
    posted = issue["posted"].strftime("%-d %B")
    has_blurbs = any(a.get("blurb") for a in arts)

    stories = ""
    for a in arts:
        byline = f'<span class="b-byline">By {esc(a["author"])}</span>' if a.get("author") else ""
        blurb = f'<p class="b-blurb">{esc(a["blurb"])}</p>' if a.get("blurb") else ""
        stories += f"""
      <li class="b-story">
        <div class="b-story-meta">
          <span class="b-page">Page {a['page']}</span>
          {byline}
        </div>
        <h2 class="b-title"><a href="{esc(pdf)}#page={a['page']}">{esc(a['title'])}</a></h2>
        {blurb}
      </li>"""

    dateline = ""
    if vol and num:
        dateline += f"<span>Vol. {esc(vol)} &middot; No. {esc(num)}</span>\n        "
    dateline += f"<span>Posted {esc(posted)}</span>"

    disclosure = ("""
    <p class="disclosure">
      The one-line summaries are machine-written from the stories and can miss detail &mdash; the PDF is the source.
    </p>""" if has_blurbs else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Fort Greene Community News &middot; Weekend Dispatch</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>{dp.NEWSPRINT_CSS}</style>
</head>
<body>
<input type="checkbox" id="theme-switch" class="visually-hidden" aria-label="Toggle dark mode">
<div class="wrap">
  <main class="sheet">
    <div class="top-bar">
      <p class="kicker"><a href="news-culture.html#crown-heights">News &amp; Culture</a> &middot; Fort Greene Community News</p>
      <label for="theme-switch" class="theme-toggle">
        <span class="label-night">Night Edition</span>
        <span class="label-day">Day Edition</span>
      </label>
    </div>

    <header class="masthead">
      <div class="vignette" aria-hidden="true">
        {dp.VIGNETTE_SVG}
      </div>

      <h1>
        Fort Greene Community News
        <span class="issue-tag">the {esc(month_name)} issue</span>
      </h1>

      <div class="dateline">
        {dateline}
      </div>
      <div class="dateline-sub" aria-hidden="true"></div>

      <div class="edition-bar edition-bar--solo">
        <div class="links">
          <a class="cta cta--solid" href="{esc(pdf)}">Read the full issue (PDF)</a>
          <a class="cta" href="{esc(issue['post_url'])}">The issue on chcobrooklyn.com</a>
        </div>
      </div>
    </header>

    <div class="section-banner">
      <span>Table of Contents &middot; Printed Order</span>
      <span class="rom">i.</span>
    </div>

    <ol class="broadsheet-cols">{stories}
    </ol>{disclosure}
  </main>

  <footer class="foot">
    <span>Fort Greene &middot; Brooklyn</span>
    <span>Source: <a href="{esc(issue['post_url'])}">Fort Greene Community Organization</a></span>
  </footer>
</div>
</body>
</html>"""








def save_site(pages, saturday, extra_files=()):
    """
    Write every page into the current directory (the git repo), commit and push
    them together with the Fort Greene Community News cache (and any extra
    files already written, such as the envelope picture), and return the
    front page's URL. Falls back to a local file:// URL if GITHUB_PAGES_URL isn't set.
    """
    import pathlib
    for name, html in pages.items():
        (pathlib.Path.cwd() / name).write_text(html, encoding="utf-8")
    print(f"  Saved {len(pages)} pages: {', '.join(sorted(pages))}")
    files = list(pages) + ([CHCO_CACHE_FILE] if os.path.exists(CHCO_CACHE_FILE) else []) + list(extra_files)
    publish_pages(files)
    # The ?v= stamp makes every issue a fresh URL so neither Pages' CDN (10-minute
    # cache) nor a browser that opened last week's link can serve the old page.
    pages_url = os.environ.get("GITHUB_PAGES_URL")
    if pages_url:
        return f"{pages_url}?v={PAGES_VERSION}"
    return f"file://{pathlib.Path.cwd() / dp.HOME_FILE}"


# One stamp per run, appended to every Pages link this run sends out.
PAGES_VERSION = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M")


def publish_pages(files, attempts=3):
    """
    Commit the freshly rendered pages on top of the latest remote branch and
    push them. The checkout this run started from may be behind the remote (a
    re-run of an old run, or a commit landing mid-run), so each attempt fetches
    the tip, moves onto it without touching the working tree, and commits only
    the page files. Raises if the push never lands: sending the email would
    otherwise link to last week's page.
    """
    import subprocess

    def git(*args, check=True):
        return subprocess.run(["git", *args], check=check)

    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    git("config", "user.email", "actions@github.com", check=False)
    git("config", "user.name", "GitHub Actions", check=False)

    for attempt in range(1, attempts + 1):
        try:
            git("fetch", "origin", branch)
            git("reset", "-q", "FETCH_HEAD")
            git("add", *files)
            if git("diff", "--cached", "--quiet", check=False).returncode == 0:
                print("  Pages unchanged from what is already published")
                return
            git("commit", "-q", "-m", "Weekend Dispatch update")
            git("push", "origin", f"HEAD:{branch}")
            print(f"  Pushed {', '.join(files)} to GitHub")
            return
        except subprocess.CalledProcessError as e:
            print(f"  Push attempt {attempt}/{attempts} failed: {e}")
            time.sleep(5 * attempt)
    raise RuntimeError("Could not push the new pages; not sending an email that links to stale ones")


# The envelope is a photo (assets/envelope-placeholder.jpg: cream envelope, ZT wax
# seal, pigeon stamp, on a walnut desk) with the postmark's centre left blank.
# Each issue stamps that Saturday's date into the postmark. The picture is published with the pages
# (assets/envelope/) and the email links to it; if that fails it is attached inline (cid:) instead.
ENVELOPE_PHOTO = "assets/envelope-placeholder.jpg"
POSTMARK_FONT = "assets/fonts/Jost-Regular.ttf"    # SIL OFL, see assets/fonts/Jost-OFL.txt
POSTMARK_X, POSTMARK_MONTH_Y, POSTMARK_DAY_Y, POSTMARK_MAX_W = 809, 351, 370, 58
POSTMARK_INK = (44, 40, 36)
ENVELOPE_CID = "envelope-photo"
# Which envelope email to send: "letter" (the pigeon peeks and the seal glints, then the seal cracks, the flap
# lifts and the letter unfolds out of the envelope inside the GIF, which links to the dispatch) or "peek" (the
# first half of that animation, with the letter's text in a card below). Set ENVELOPE_STYLE in the environment
# to switch without editing code. A letter that fails to build falls back to "peek".
ENVELOPE_STYLE = os.environ.get("ENVELOPE_STYLE", "letter")


def build_envelope_photo(saturday):
    """Return JPEG bytes of the desk envelope with this Saturday's date in the postmark."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    envelope_base_image(saturday).save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


def build_envelope_animation(saturday):
    """Return animated GIF bytes of the same envelope (pigeon peeks, seal glints).
    Frame 1 is the plain dated envelope, so mail apps that do not animate show it."""
    return dm.build_envelope_gif(envelope_base_image(saturday))


def build_envelope_image(saturday):
    """
    The (bytes, subtype) pair the email attaches: the animated GIF, or, if
    building it ever fails, the still photo. The Saturday send must never be
    held up by the animation.
    """
    try:
        return build_envelope_animation(saturday), "gif"
    except Exception as e:
        print(f"  Animated envelope failed ({e}); using the still photo instead")
        return build_envelope_photo(saturday), "jpeg"


def envelope_base_image(saturday):
    """The desk envelope as a PIL image with this Saturday's date in the postmark."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    base = Image.open(ENVELOPE_PHOTO).convert("RGB")
    layer = Image.new("L", base.size, 0)
    draw = ImageDraw.Draw(layer)
    month, day = saturday.strftime("%B").upper(), saturday.strftime("%d")
    size = 14
    while size > 9:
        font = ImageFont.truetype(POSTMARK_FONT, size)
        if draw.textlength(month, font=font) <= POSTMARK_MAX_W:
            break
        size -= 1
    draw.text((POSTMARK_X, POSTMARK_MONTH_Y), month, font=font, fill=235, anchor="mm")
    draw.text((POSTMARK_X, POSTMARK_DAY_Y), day, font=ImageFont.truetype(POSTMARK_FONT, 18), fill=235, anchor="mm")
    layer = layer.filter(ImageFilter.GaussianBlur(0.45))
    # uneven ink, like a real hand stamp (seeded, so a re-run matches)
    rng = np.random.default_rng(int(saturday.strftime("%Y%m%d")))
    alpha = np.array(layer, dtype=float) / 255 * rng.uniform(0.8, 1.0, (base.size[1], base.size[0]))
    arr = np.array(base, dtype=float)
    arr = arr * (1 - alpha[..., None]) + np.array(POSTMARK_INK, dtype=float) * alpha[..., None]
    return Image.fromarray(arr.clip(0, 255).astype("uint8"))


ENVELOPE_BLURB = (
    "Your full dispatch is ready &mdash; weather &amp; Biscuit walk windows, the Greenmarket verdict, what&rsquo;s on at Public Records, what&rsquo;s on sale this weekend, what&rsquo;s going on around Brooklyn, this week&rsquo;s nature report, bird, tree and street-tree map, and the Rockaway tides, then the MTA Weekender &mdash; this weekend&rsquo;s subway changes and three trips by train without a car &mdash; plus a separate News &amp; Culture page, from Fort Greene to the world."
)  # HTML entities; shared by the email text, the letter GIF and its alt text


def build_envelope_email(saturday, sunday, newsletter_url, photo_src=f"cid:{ENVELOPE_CID}"):
    """
    Static envelope email — no JavaScript. Renders pre-opened in Gmail:
    flap folded back, seal as decoration, newsletter peek always visible,
    prominent link to the full newsletter.
    """
    sat_label = saturday.strftime("%B %d")
    sun_label = sunday.strftime("%B %d")
    sat_day   = saturday.strftime("%A")
    year      = saturday.year

    html = f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;1,300;1,400&family=Courier+Prime:wght@400;700&family=Special+Elite&display=swap" rel="stylesheet">
<style>
  :root {{ color-scheme: light only; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #2A1E0A !important;
    margin: 0 !important;
    padding: 32px 16px !important;
    font-family: 'Cormorant Garamond', Georgia, serif !important;
    -webkit-text-size-adjust: 100%;
  }}
  .scene {{
    max-width: 620px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    align-items: center;
  }}
  .pre-label {{
    font-family: 'Courier Prime', monospace;
    font-size: 9px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #6A5030;
    text-align: center;
    margin-bottom: 22px;
  }}

  /* ── ENVELOPE (realistic, pre-opened, single SVG) ── */
  /* Shadow is a box-shadow on the img, not a CSS filter on the wrapper: iOS
     Mail paints a filtered layer before the cid: image arrives and never
     repaints it, so the envelope came through as blank space on iPhone. */
  .env-wrap {{
    width: 100% !important;
    position: relative;
  }}
  .env-photo {{
    width: 100% !important;
    height: auto !important;
    display: block !important;
    border-radius: 4px;
    box-shadow: 0 22px 38px rgba(40,28,10,0.42), 0 4px 10px rgba(40,28,10,0.3);
  }}

  /* ── NEWSLETTER PEEK (always visible below envelope) ── */
  .newsletter-peek {{
    width: calc(100% - 48px) !important;
    background: #F7F0DC !important;
    border: 1px solid #D4B98A !important;
    border-top: none !important;
    padding: 22px 26px !important;
    margin-top: -4px !important;
    position: relative !important;
    box-shadow: 0 14px 36px rgba(0,0,0,0.32) !important;
  }}
  /* Force light mode — prevent Gmail dark mode inversion */
  [data-ogsc] .newsletter-peek {{ background: #F7F0DC !important; color: #1A1208 !important; }}
  [data-ogsc] body {{ background: #2A1E0A !important; }}
  [data-ogsc] .peek-text {{ color: #2A1E0A !important; }}
  [data-ogsc] .peek-title {{ color: #1A1208 !important; }}
  [data-ogsc] .cta-btn {{ background: #1A1208 !important; color: #F5EDD8 !important; }}
  @media (prefers-color-scheme: dark) {{
    .newsletter-peek {{ background: #F7F0DC !important; color: #1A1208 !important; }}
    body {{ background: #2A1E0A !important; }}
    .peek-text {{ color: #2A1E0A !important; }}
    .peek-title {{ color: #1A1208 !important; }}
    .cta-btn {{ background: #1A1208 !important; color: #F5EDD8 !important; }}
  }}

  .peek-eyebrow {{
    font-family: 'Courier Prime', monospace;
    font-size: 7.5px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #8A6A3A;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
    gap: 8px;
    overflow: hidden;
  }}
  .peek-eyebrow span {{ min-width: 0; white-space: nowrap; overflow: hidden; }}

  .peek-title {{
    font-family: 'Special Elite', 'Courier Prime', 'Courier New', monospace;
    font-weight: 400;
    font-style: normal;
    font-size: 46px;
    line-height: 0.95;
    letter-spacing: -0.01em;
    color: #1A1208;
    margin-top: 18px;
    margin-bottom: 12px;
  }}
  .peek-title em {{ color: #9B3A1A; font-style: normal; }}

  .peek-rule {{ height: 1px; background: #C4A46A; margin: 10px 0 14px; }}

  .peek-section-label {{
    font-family: 'Courier Prime', monospace;
    font-size: 7.5px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: #9B3A1A;
    margin-bottom: 6px;
  }}

  .peek-text {{
    font-family: 'Courier Prime', 'Courier New', monospace;
    font-size: 13.5px;
    line-height: 1.75;
    color: #2A1E0A;
    margin-bottom: 16px;
  }}

  /* CTA button */
  .cta-wrap {{
    text-align: center;
    padding-top: 4px;
  }}

  .cta-btn {{
    display: inline-block;
    font-family: 'Courier Prime', monospace;
    font-size: 9px;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    color: #F5EDD8;
    background: #1A1208;
    text-decoration: none;
    padding: 12px 28px;
    border: 1px solid #3A2A10;
  }}

  .bottom-note {{
    font-family: 'Courier Prime', monospace;
    font-size: 8px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: #3A2A10;
    text-align: center;
    margin-top: 22px;
  }}
  a[x-apple-data-detectors] {{ color: inherit !important; text-decoration: none !important; font: inherit !important; }}
</style>
</head>
<body>
<div class="scene">
  <div class="pre-label">Your weekend dispatch has arrived</div>

  <!-- Realistic envelope: single SVG. Flap folded back above body;
       side + bottom panels fold to center; pressed wax seal at junction. -->
  <div class="env-wrap">
    <img class="env-photo" src="{photo_src}" width="620" height="393" alt="A cream envelope sealed with a sample wax seal, addressed to Alex Sample, with a Brooklyn pigeon stamp postmarked {saturday.strftime('%B %d')}, on a walnut desk">
  </div>

  <!-- Newsletter peek — always visible -->
  <div class="newsletter-peek">
    <div class="peek-eyebrow">
      <span>Weekend Dispatch</span>
      <span>Fort Greene &middot; Brooklyn</span>
    </div>
    <div class="peek-title">The<br><em>Week</em>end.</div>
    <div class="peek-rule"></div>
    <div class="peek-section-label">{sat_day}, {sat_label} &mdash; {sun_label} &middot; {year}</div>
    <p class="peek-text">{ENVELOPE_BLURB}</p>
    <div class="cta-wrap">
      <a class="cta-btn" href="{newsletter_url}">&darr;&nbsp;&nbsp;Open Full Newsletter</a>
    </div>
  </div>

  <div class="bottom-note">Weekend Dispatch &middot; Fort Greene &middot; {year}</div>
</div>
</body>
</html>"""
    return html


def build_letter_email(saturday, sunday, newsletter_url, photo_src, size, alt):
    """
    The envelope email for ENVELOPE_STYLE "letter": the animated GIF is the whole message and links to the
    dispatch; the letter's words are in its alt text (shown if images are blocked) and in a plain link below.
    """
    import html as _html
    w, h = size
    shown_h = round(620 * h / w)
    year = saturday.year
    url = _html.escape(newsletter_url, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<link href="https://fonts.googleapis.com/css2?family=Courier+Prime:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {{ color-scheme: light only; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #2A1E0A !important; margin: 0 !important; padding: 32px 16px !important;
         font-family: 'Courier Prime', 'Courier New', monospace !important; -webkit-text-size-adjust: 100%; }}
  .scene {{ max-width: 620px; margin: 0 auto; }}
  .pre-label {{ font-size: 9px; letter-spacing: 0.3em; text-transform: uppercase; color: #6A5030; text-align: center; margin-bottom: 22px; }}
  .env-link {{ display: block; text-decoration: none; }}
  /* box-shadow on the img itself, not a filter on a wrapper (iOS Mail never repaints a filtered layer). The
     colour and font here are what shows, on the dark page, if images are blocked and the alt text appears. */
  .env-photo {{ width: 100% !important; height: auto !important; display: block !important; border: 0; border-radius: 4px;
               box-shadow: 0 22px 38px rgba(40,28,10,0.42), 0 4px 10px rgba(40,28,10,0.3);
               color: #F5EDD8; font-size: 13px; line-height: 1.7; }}
  .open-line {{ text-align: center; margin-top: 20px; font-size: 9px; letter-spacing: 0.22em; text-transform: uppercase; }}
  .open-line a {{ color: #D4B98A; text-decoration: underline; }}
  .bottom-note {{ font-size: 8px; letter-spacing: 0.18em; text-transform: uppercase; color: #3A2A10; text-align: center; margin-top: 22px; }}
  [data-ogsc] body {{ background: #2A1E0A !important; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #2A1E0A !important; }} }}
  a[x-apple-data-detectors] {{ color: inherit !important; text-decoration: none !important; font: inherit !important; }}
</style>
</head>
<body>
<div class="scene">
  <div class="pre-label">Your weekend dispatch has arrived</div>
  <a class="env-link" href="{url}" title="Open the full newsletter">
    <img class="env-photo" src="{photo_src}" width="620" height="{shown_h}" alt="{_html.escape(alt, quote=True)}">
  </a>
  <p class="open-line"><a href="{url}">Open the full newsletter &rarr;</a></p>
  <div class="bottom-note">Weekend Dispatch &middot; Fort Greene &middot; {year}</div>
</div>
</body>
</html>"""


ENVELOPE_HOST_DIR = "assets/envelope"    # each issue's envelope picture is published here, beside the pages


def build_envelope_art(saturday, sunday, style=None):
    """
    Build the envelope picture: {"style", "data", "subtype", "size", "content"}. "letter" is the unfurling-letter
    GIF; if it cannot be built, "peek" (the pigeon and the glint); if that cannot be built, the still photo.
    The Saturday send always has something to send.
    """
    style = style or ENVELOPE_STYLE
    if style == "letter":
        try:
            content = el.letter_content(saturday, sunday, ENVELOPE_BLURB)
            gif, size = el.build_letter_gif(envelope_base_image(saturday), content)
            return {"style": "letter", "data": gif, "subtype": "gif", "size": size, "content": content}
        except Exception as e:
            print(f"  Letter animation failed ({e}); using the peeking pigeon envelope instead")
    data, subtype = build_envelope_image(saturday)
    return {"style": "peek", "data": data, "subtype": subtype, "size": None, "content": None}


def build_envelope_parts(saturday, sunday, newsletter_url, style=None, inline=False, hosted_url=None, art=None):
    """
    Everything the envelope email is made of: {"html", "images", "text", "style"}. images maps the
    Content-ID to (bytes, subtype); text is the plain-text part (None to derive it from the HTML).

    Where the picture lives: hosted_url puts a link to it in the email (nothing attached: the email is a few
    kilobytes and the mail app loads the picture itself, so there is no "tap to download" placeholder);
    inline=True embeds it as a data: URI (for previews that are a single file); otherwise it is attached.
    art is a picture already built by build_envelope_art (so it can be published first).
    """
    import base64
    art = art or build_envelope_art(saturday, sunday, style)
    if hosted_url:
        src = hosted_url
    elif inline:
        src = f"data:image/{art['subtype']};base64,{base64.b64encode(art['data']).decode()}"
    else:
        src = f"cid:{ENVELOPE_CID}"
    attached = {} if (hosted_url or inline) else {ENVELOPE_CID: (art["data"], art["subtype"])}
    if art["style"] == "letter":
        text = el.letter_text(art["content"], newsletter_url)
        html = build_letter_email(saturday, sunday, newsletter_url, src, art["size"], text)
        return {"html": html, "images": attached, "text": text, "style": "letter"}
    html = build_envelope_email(saturday, sunday, newsletter_url, photo_src=src)
    return {"html": html, "images": attached, "text": None, "style": "peek"}


def site_base_url():
    """The folder the pages are served from (GITHUB_PAGES_URL without the page name), or "" when not publishing."""
    u = os.environ.get("GITHUB_PAGES_URL", "")
    return u.rsplit("/", 1)[0] if u else ""


def write_envelope_file(art, saturday):
    """Write the envelope picture into the repo folder it is served from; returns its path relative to the repo root."""
    import pathlib
    rel = f"{ENVELOPE_HOST_DIR}/{saturday.isoformat()}.{'jpg' if art['subtype'] == 'jpeg' else art['subtype']}"
    path = pathlib.Path.cwd() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(art["data"])
    return rel


def wait_until_live(url, expected_bytes, timeout=300, every=8):
    """
    Wait until the published picture can actually be fetched from the site (Pages takes a little while after a
    push). Each check uses a throwaway query string, so the address the email will use is never requested early
    and a "not found" answer can never be cached for it. True once the full file is being served.
    """
    import uuid
    deadline = time.time() + timeout
    while True:
        try:
            r = requests.get(f"{url}?cb={uuid.uuid4().hex[:10]}", timeout=30)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) == expected_bytes:
                return True
        except requests.RequestException:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(every)


def build_email_message(html_body, saturday, sender, recipient, images=None, text=None):
    """
    Build the MIME message without sending it (send_email sends it; the preview
    writes it to a .eml file you can open in a mail app).

    images maps a Content-ID to the image bytes (a JPEG) or to a
    (bytes, subtype) pair such as (gif_bytes, "gif").
    """
    sat_label = saturday.strftime("%B %d")
    subject = f"Weekend Dispatch · {sat_label}"

    # alternative[ text/plain, related[ text/html, inline images ] ]. The HTML
    # must be the first (root) part of the multipart/related that holds the
    # images: iOS Mail won't resolve cid: links into a nested alternative, and
    # showed the envelope photo as blank space while Mac Mail coped.
    from email.mime.image import MIMEImage
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Sample Dispatch Desk", sender))
    msg["To"] = recipient
    if text is None:
        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["style", "script", "head"]):    # the plain-text part should not hold the CSS
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    related = MIMEMultipart("related", type="text/html")
    related.attach(MIMEText(html_body, "html", "utf-8"))
    for cid, item in (images or {}).items():
        data, subtype = item if isinstance(item, tuple) else (item, "jpeg")
        part = MIMEImage(data, subtype)
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("X-Attachment-Id", cid)
        part.add_header("Content-Disposition", "inline", filename=f"{cid}.{'jpg' if subtype == 'jpeg' else subtype}")
        related.attach(part)
    msg.attach(related)
    return msg


def send_email(html_body, saturday, images=None, text=None):
    sender = "".join(os.environ["DISPATCH_EMAIL"].split())
    password = "".join(os.environ["DISPATCH_APP_PASSWORD"].split())
    recipient = "".join(os.environ["DISPATCH_TO"].split())
    msg = build_email_message(html_body, saturday, sender, recipient, images, text)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def _placeholder_narrative():
    """
    Stand-in for the Claude-written sections so --preview can render a full
    layout without an ANTHROPIC_API_KEY. Uses the exact section labels
    build_email_html parses (and one [SOURCE] block for Around Brooklyn). The
    MTA and Public Records sections are unaffected — they are rendered from
    live structured data, not this.
    """
    ph = "[Preview placeholder — set ANTHROPIC_API_KEY to generate this section.]"
    return (
        f"WEATHER + BISCUIT\n{ph}\n\n"
        f"SAMPLE GREENMARKET\n{ph}\n\n"
        f"AROUND BROOKLYN\n[Preview]\nPlaceholder headline — {ph}"
    )


def assemble_newsletter(client):
    """
    Run every fetch and build the full newsletter HTML — the same data and the
    same section rendering the live dispatch uses, so main() and --preview stay
    in lockstep.

    `client` is an Anthropic client for the Claude-written sections (Weather,
    Greenmarket, Around Brooklyn). Pass None to fall back to placeholder
    text for just those sections so the layout still renders. The MTA and
    Public Records sections are always built for real from live data.

    Returns (pages, narrative, saturday, sunday, chco_issue): `pages` is
    {filename: html}: the main page, the separate News & Culture page, the long
    list, the subway System Changes page (when the MTA feed was readable and has
    changes) and the Fort Greene Community News page when there is an issue.
    """
    # Determine weekend dates
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    print(f"Covering weekend: {saturday.strftime('%b %d')} – {sunday.strftime('%b %d')}")

    # 1. Weather
    print("Fetching weather...")
    weekend = {}
    for attempt in (1, 2, 3):
        try:
            weekend = parse_weekend_days(fetch_weekend_weather())
            break
        except Exception as e:
            print(f"  Weather attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(4 * attempt)
    if not weekend:
        print("  Weather unavailable; carrying on without it")
    saturday_data = weekend.get("saturday", {})
    sunday_data = weekend.get("sunday", {})

    # 2. Walk windows
    print("Computing Biscuit walk windows...")
    sat_windows = get_walk_windows(saturday_data, "saturday") if saturday_data else []
    sun_windows = get_walk_windows(sunday_data, "sunday") if sunday_data else []

    # 3. MTA — planned A/C/3 service changes active this weekend (flat list)
    print("Fetching MTA alerts...")
    mta_alerts = fetch_mta_alerts(saturday, sunday)
    print(f"  A/C/3 weekend service changes: {len(mta_alerts)}")
    surface_alerts = fetch_surface_transit_alerts()
    if surface_alerts:
        print(f"  Surface-transit disruptions: "
              f"{', '.join(sorted({a['service'] for a in surface_alerts}))}")
    else:
        print("  No B65/B43/AirTrain/LIRR disruptions — services omitted")

    # 4. Farmers market
    print("Fetching market data...")
    market_list = fetch_market_data()
    market_result = market_go_nogo(saturday_data, market_list)

    # 5. Public Records events
    print("Scraping Public Records events...")
    pr_events = fetch_public_records_events()
    print(f"  Public Records events found: {len(pr_events)}")

    # 6. DICE — the watchlist drives the email, the wider sweep fills the
    #    companion page. The email covers tonight and the *following* Friday,
    #    which is far enough out that only the profile pages reach it.
    friday = saturday + datetime.timedelta(days=6)
    print("Fetching DICE watchlist (tracked venues + artists)...")
    dice_watchlist = fetch_dice_watchlist()
    for _label, _entry in dice_watchlist["venues"].items():
        print(f"  {_label}: {_entry['state']} ({len(_entry['events'])} upcoming)")
    for _label, _entry in dice_watchlist["artists"].items():
        print(f"  {_label} [artist]: {_entry['state']} ({len(_entry['events'])} upcoming)")

    dice_picks = select_headline_events(dice_watchlist, saturday, friday)
    print(f"  Headline picks for {saturday} + {friday}: {len(dice_picks)}")

    going_out = dice_going_out_dates(saturday)
    print(f"  Going-out nights in the window: "
          f"{', '.join(d.strftime('%a %-d %b') for d in going_out)}")
    print("Sweeping DICE for the rest of Brooklyn...")
    dice_sweep = fetch_dice_events(going_out)
    print(f"  Nearby events found: {len(dice_sweep)}")

    # 7. Brooklyn culture news
    print("Fetching Brooklyn news feeds...")
    brooklyn_news = fetch_brooklyn_news()
    slot6 = brooklyn_news.get("_slot6_winner", "?")
    print(f"  Slot 6 winner: {slot6}")
    edible_picks = fetch_edible_picks(today)
    print(f"  Edible Brooklyn picks: "
          f"{', '.join(p['area'] + ': ' + p['title'] for p in edible_picks) or 'none'}")

    # 7b. Fort Greene Community News — the CHCO's monthly paper, own page
    print("Fetching Fort Greene Community News...")
    chco_issue = fetch_chco_issue(today, client) if CHCO_ENABLED else None
    if chco_issue:
        print(f"  {chco_issue['month']} issue: {len(chco_issue['articles'])} stories, "
              f"posted {chco_issue['posted']}")
    chco_html = build_chco_html(chco_issue) if chco_issue else ""

    # 8. Narrative — Claude when we have a client, placeholder otherwise
    if client is not None:
        print("Generating narrative...")
        try:
            narrative = generate_narrative(
                saturday_data, sunday_data,
                sat_windows, sun_windows,
                market_result, market_list,
                brooklyn_news,
                client,
            )
        except Exception as e:
            print(f"  Narrative generation failed: {e}")
            print("  Falling back to placeholder narrative...")
            narrative = _placeholder_narrative()
    else:
        print("No Anthropic client — using placeholder narrative sections...")
        narrative = _placeholder_narrative()

    # 9. News & Culture: the extra sources behind the separate page, plus what the
    #    main page needs to point at it (a headline strip and the Nature Report).
    print("Fetching the News & Culture feeds...")
    pool = dp.fetch_pool(today)
    print(f"  Feeds answering: {sum(1 for v in pool.values() if v)} of {len(pool)}")
    print("Fetching iNaturalist sightings...")
    inat = dp.fetch_inat_report(today, LAT, LON)
    if inat:
        print(f"  {inat['observations']} verified observations of {inat['species']} species this week")
    print("Fetching park/museum events and street permits...")
    events = dp.fetch_jsonld_events(today)
    permits = dp.fetch_permit_events(today)
    print(f"  Events: {len(events)}; permits (Community Boards 8 & 9): {len(permits)}")
    nature = build_nature_pieces(saturday)

    # 9a. Culture listings: BAM's film schedule for the Around Brooklyn section (fails closed).
    print("Fetching culture listings...")
    cinema_films = cl.fetch_bam_films(saturday, sunday)
    print(f"  Cinema listings: {len(cinema_films)}")
    cinema_html = cl.build_culture_html(cinema_films, saturday)
    bam_music = cl.fetch_bam_music(saturday, sunday)
    print(f"  BAM music listings: {len(bam_music)}")
    cinema_html += cl.build_bam_html(bam_music, saturday, "At BAM: music this weekend", cl.BAM_MUSIC_URL,
                                     "The full BAM music schedule")
    weekend_events = cl.fetch_weekend_events(saturday, sunday)
    print(f"  Parks and museums this weekend: {len(weekend_events)}")
    cinema_html += cl.build_events_html(weekend_events)
    import ohreader as ohr
    cinema_html += ohr.build_card_html(*ohr.fetch_latest_issue())  # Oh Reader link card; the magazine itself is not read
    import your_block as yb
    cinema_html += yb.build_block_html(yb.fetch_block_schedule())  # your block's DSNY days; address comes from the environment

    # 9b. MTA Weekender: every planned subway change (for the System Changes page and
    #     the section's summary) and three car-free trips from the hand-vetted library.
    print("Building the MTA Weekender...")
    subway_changes = mw.subway_changes(saturday, sunday)
    print("  Planned subway changes this weekend: "
          + ("feed unavailable" if subway_changes is None else str(len(subway_changes))))
    trips = mw.pick_trips(mw.load_library(), saturday, saturday_data.get("rain_pct_max"))
    print(f"  Trips: {', '.join(t['station'] + ' (' + t['railroad'] + ')' for t in trips) or 'none'}")
    rail_notices = mw.rail_feeds(saturday, sunday)

    # Links already on the main page; the News & Culture page never repeats them.
    main_urls = set(re.findall(r'href="([^"]+)"', narrative))
    for group in (edible_picks, pr_events, dice_picks):
        main_urls |= {e["url"] for e in (group or []) if e.get("url")}

    # 10. Build the pages. Links between them are relative, so they work on Pages
    #     and from a local folder alike. The Community News link is deliberately
    #     unversioned: it stays put until a new issue appears.
    print("Building the long list...")
    picked_urls = {p["url"] for p in dice_picks if p.get("url")}
    long_list_html = build_long_list_html(dice_watchlist, dice_sweep,
                                          saturday, friday, picked_urls)
    q = f"?v={PAGES_VERSION}"
    print("Building News & Culture...")
    news_html, headlines, news_count = dp.build_news_culture({
        "pool": pool, "events": events, "permits": permits, "chco_issue": chco_issue,
        "chco_url": dp.CHCO_FILE if chco_html else "", "saturday": saturday, "sunday": sunday,
        "main_urls": main_urls, "version": PAGES_VERSION,
    })
    print(f"  {news_count} items; {len(headlines)} headlines for the main page")
    print("Building the main page...")
    has_subway_page = bool(subway_changes)
    weekender_html = mw.build_weekender_html(
        subway_changes, trips, rail_notices, f"{mw.SUBWAY_FILE}{q}" if has_subway_page else "")
    main_html = build_email_html(narrative, saturday_data, sunday_data, saturday, sunday,
                                 mta_alerts, surface_alerts, pr_events, dice_picks,
                                 f"events.html{q}", edible_picks,
                                 headlines, f"{dp.NEWS_FILE}{q}", dp.render_inat(inat), nature,
                                 weekender_html, around_extra_html=cinema_html)
    pages = {dp.HOME_FILE: main_html, dp.NEWS_FILE: news_html, "events.html": long_list_html}
    if has_subway_page:
        pages[mw.SUBWAY_FILE] = mw.build_subway_page(
            subway_changes, saturday, sunday,
            datetime.datetime.now().strftime("%-d %B %Y, %-I:%M %p"), PAGES_VERSION)
    if chco_html:
        pages[dp.CHCO_FILE] = chco_html
    return pages, narrative, saturday, sunday, chco_issue


def run_preview(site_dir="site_preview"):
    """
    Local preview: build the whole newspaper exactly as main() does (same
    fetches, same rendering), write every page into `site_dir` and open the
    front page in the browser with `open`. Sends NO email and runs NO git commands.

    Uses ANTHROPIC_API_KEY from the env for the Claude-written sections; if it
    is not set, those sections fall back to placeholder text so the layout
    still renders.
    """
    import pathlib
    import subprocess

    print("Building local preview (no email, no git)...")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        print("  ANTHROPIC_API_KEY found — generating real narrative sections")
        client = anthropic.Anthropic(api_key=api_key)
    else:
        print("  ANTHROPIC_API_KEY not set — placeholder text for narrative sections")
        client = None

    pages, _narrative, _saturday, _sunday, _issue = assemble_newsletter(client)

    out = pathlib.Path(site_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, html in pages.items():
        (out / name).write_text(html, encoding="utf-8")
    print(f"  Wrote {len(pages)} pages to {out}/")
    write_test_email(site_dir, _saturday, dp.HOME_FILE)

    front = out / dp.HOME_FILE
    subprocess.run(["open", str(front)], check=False)
    print(f"✓ Preview opened in browser: {front}")


def write_test_email(out_dir="site_preview", saturday=None, link=None, style=None):
    """
    Build the envelope email exactly as the Saturday run does, but write it to
    disk instead of sending it:

      test-envelope.eml   the real MIME message (open it in Mail / Outlook / Thunderbird)
      test-envelope.html  the same HTML, with the animated GIF as a file beside it
      test-envelope.gif   the animation

    style is "peek" or "letter" (default: ENVELOPE_STYLE). No network, no credentials,
    nothing is sent. The addresses are placeholders.
    """
    import pathlib
    saturday = saturday or (datetime.date.today() + datetime.timedelta(days=(5 - datetime.date.today().weekday()) % 7))
    sunday = saturday + datetime.timedelta(days=1)
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    link = link or "https://your-username.github.io/weekend-dispatch/weekend.html"

    parts = build_envelope_parts(saturday, sunday, link, style)
    (data, subtype), = parts["images"].values()
    msg = build_email_message(parts["html"], saturday, "desk@weekend-dispatch.invalid",
                              "reader@weekend-dispatch.invalid", parts["images"], parts["text"])
    (out / "test-envelope.eml").write_bytes(msg.as_bytes())
    (out / f"test-envelope.{subtype}").write_bytes(data)
    (out / "test-envelope.html").write_text(
        parts["html"].replace(f"cid:{ENVELOPE_CID}", f"test-envelope.{subtype}"), encoding="utf-8")
    print(f"  Test email ({parts['style']} style) written to {out}/test-envelope.eml "
          f"({len(data) // 1024} KB {subtype.upper()}); nothing was sent")
    return out / "test-envelope.eml"


def main():
    print("Initializing Weekend Dispatch...")

    # Check env vars
    required = ["ANTHROPIC_API_KEY", "DISPATCH_EMAIL", "DISPATCH_APP_PASSWORD", "DISPATCH_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"⚠️  Missing env vars: {', '.join(missing)}")
        print("   Set them and re-run. For local testing, you can export them in your shell.")
        return

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Steps 1–10: fetch everything and build every page
    pages, narrative, saturday, sunday, _issue = assemble_newsletter(client)

    # 11. Build the envelope picture, then save + push to GitHub Pages: all pages, the Crown
    #     Heights Community News cache and the envelope picture go up in one commit. The email
    #     links to the published picture (a few kilobytes, nothing to download); if that cannot
    #     be arranged it carries the picture as an attachment instead, as it used to.
    print("Building the envelope picture...")
    art = build_envelope_art(saturday, sunday)
    base, hosted_rel = site_base_url(), None
    if base:
        try:
            hosted_rel = write_envelope_file(art, saturday)
        except Exception as e:
            print(f"  Could not write the envelope picture for the site ({e}); it will be attached instead")
    newsletter_url = save_site(pages, saturday, extra_files=[hosted_rel] if hosted_rel else [])
    print(f"  Newspaper published: {newsletter_url}")

    # 12. Build envelope email + send
    hosted_url = None
    if hosted_rel:
        live = f"{base}/{hosted_rel}"
        print("Waiting for the envelope picture to be live on the site...")
        if wait_until_live(live, len(art["data"])):
            hosted_url = f"{live}?v={PAGES_VERSION}"
        else:
            print("  It was not live in time; attaching the picture to the email instead")
    print("Building envelope email...")
    parts = build_envelope_parts(saturday, sunday, newsletter_url, art=art, hosted_url=hosted_url)
    print(f"  Envelope style: {parts['style']} ({'picture linked from the site' if hosted_url else 'picture attached'})")

    print("Sending...")
    send_email(parts["html"], saturday, parts["images"], parts["text"])

    sat_label = saturday.strftime("%B %d")
    print(f"✓ Weekend Dispatch sent for {sat_label} – {sunday.strftime('%B %d')}")
    print()
    print("─── NARRATIVE PREVIEW ───")
    print(narrative)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fort Greene Weekend Dispatch")
    parser.add_argument(
        "--preview", action="store_true",
        help="Build every page into --site-dir and open the front page locally — no email, no git."
    )
    parser.add_argument("--site-dir", default="site_preview",
                        help="Where --preview writes the pages (default: site_preview).")
    parser.add_argument(
        "--test-email", action="store_true",
        help="Write only the envelope email to --site-dir as test-envelope.eml/.html/.gif — "
             "no fetching, nothing sent."
    )
    parser.add_argument(
        "--envelope-style", choices=["peek", "letter"], default=None,
        help="Which envelope animation --test-email builds (default: ENVELOPE_STYLE, currently " + ENVELOPE_STYLE + ")."
    )
    args = parser.parse_args()

    if args.test_email:
        write_test_email(args.site_dir, style=args.envelope_style)
    elif args.preview:
        run_preview(args.site_dir)
    else:
        main()
