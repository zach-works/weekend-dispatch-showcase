#!/usr/bin/env python3
"""
Weekend Dispatch: a Saturday-morning personal newsletter (showcase copy).

Covers the full weekend: MTA service changes for the reader's lines, weather and
dog-walk windows, a farmers-market go/no-go, venue and DICE event picks,
Brooklyn culture news, and rotating nature sections. The email itself is a
designed envelope that links to the full newsletter hosted on GitHub Pages.

SHOWCASE COPY: personal settings were replaced with fictional sample values
(see sample_profile.py), keys come from environment variables, and the
GitHub Actions schedule is turned off. It is not wired to send anything.

Environment variables (for a real deployment):
  ANTHROPIC_API_KEY
  DISPATCH_EMAIL        (Gmail sender address)
  DISPATCH_APP_PASSWORD (Gmail App Password)
  DISPATCH_TO           (recipient address)
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

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

from sample_profile import (   # fictional sample values
    LAT, LON, HOME_LATLON, CORRIDORS, HOME_STOPS, HOME_LINES, BANNER_STATIONS,
    ALTERNATE_STATIONS, INTEREST_PROFILE, DICE_TRACKED_VENUES, DICE_TRACKED_ARTISTS,
)
TIMEZONE = "America/New_York"

# Walk-routing corridors (CORRIDORS) come from sample_profile.py

# MTA: every current service alert on the reader's lines is pulled system-wide
# (see fetch_mta_alerts), then split into alerts that touch one of the home
# stops vs. alerts elsewhere on the line. HOME_STOPS and HOME_LINES come from
# sample_profile.py.

# GTFS-realtime Service Alerts, JSON flavor (no API key). Each alert carries an
# informed_entity[] list with route_id and/or stop_id, which is what lets us
# group alerts by line vs. specific stop.
MTA_ALERTS_FEED = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
)

# The home-station status banner is built from exactly the BANNER_STATIONS in
# sample_profile.py: the closest stop on each of the reader's lines.

# Alternate-station lookup for the banner's "go to ___ instead" line comes from
# ALTERNATE_STATIONS in sample_profile.py, ordered nearest first.

# Sample Hall (venue)
SAMPLE_HALL_URL = "https://example.com/sample-hall/"

# Farmers market
MARKET_URL = "https://www.grownyc.org/greenmarket/manhattan-union-square"

# GrowNYC Open Data (Brooklyn markets)
GROWNYC_API = "https://data.cityofnewyork.us/resource/b7kx-qikm.json"

# INTEREST_PROFILE (what the reader likes) comes from sample_profile.py


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
    informs one of HOME_LINES.

    Returns a flat list (no your/elsewhere grouping — the banner and the single
    C/G change list are derived from this list downstream). Each item:
      {
        "text":          alert header (falls back to description),
        "routes":        ["C", "G", ...] limited to HOME_LINES, in HOME_LINES order,
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
        if len(text) < 80 and _re.search(r'take the \[|use the \[', text.lower()):
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
            "routes": [r for r in HOME_LINES if r in matched_routes],
            "stop_ids": sorted(stops),
            "is_line_level": not stops,
            "alert_type": _mercury_alert_type(alert),
        })

    return alerts


def fetch_surface_transit_alerts():
    """
    Conditional alerts for the B38 / B52 buses, the JFK AirTrain (Jamaica and
    Howard Beach), and the Atlantic Terminal LIRR station.

    These services are ONLY surfaced when they are disrupted — a planned
    reroute, suspension, or service cut. If nothing matching is found, this
    returns an empty list and the services are not mentioned in the dispatch
    at all (per the "only if cut or altered" rule). Normal subway info is
    handled separately by fetch_mta_alerts.

    Returns a list of {"service": <label>, "text": <alert text>} dicts.
    """
    # Each watched service: a label plus the substrings that identify it.
    WATCHED = [
        ("B38 bus",            ["b38"]),
        ("B52 bus",            ["b52"]),
        ("JFK AirTrain (Jamaica)",     ["airtrain", "air train"]),
        ("JFK AirTrain (Howard Beach)", ["airtrain", "air train"]),
        ("Atlantic Terminal LIRR", ["atlantic terminal"]),
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
    Go/no-go logic for Union Square Greenmarket.
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

    # Find Union Square specifically in the market list
    gap = next((m for m in market_list if "union square" in str(m.get("marketname", "")).lower()), None)
    market_hours = gap.get("hoursoperations", "8am–3pm") if gap else "8am–3pm"

    return {
        "verdict": verdict,
        "reason": reason,
        "market_hours": market_hours,
        "max_rain_outing": max_rain,
        "peak_temp": peak_temp,
        "avg_temp": avg_temp,
    }


# Northeast seasonal produce calendar for Union Square Greenmarket.
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
    """Return a formatted string of what's in season at Union Square this month."""
    items = _SEASONAL_PRODUCE.get(date.month, [])
    return ", ".join(items) if items else "seasonal produce varies"


# ─────────────────────────────────────────────
# SAMPLE HALL EVENTS
# ─────────────────────────────────────────────

def fetch_sample_hall_events():
    """
    Scrape upcoming shows from Sample Hall (a sample venue).
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
        r = requests.get("https://example.com/sample-hall/", headers=headers, timeout=15)
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


def fetch_neighborhood_salon_events():
    """
    Fetch upcoming events at Sample Salon (a neighborhood event space).
    Tries Eventbrite search first, then Partiful. Returns list of event dicts
    with 'text' and 'url' fields, same shape as BPL events.
    """
    today = datetime.date.today()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    events = []

    # Strategy 1: Eventbrite search
    for search_url in [
        "https://www.eventbrite.com/d/ny--brooklyn/sample-salon/",
        "https://www.eventbrite.com/d/ny--fort-greene/sample-salon/",
        "https://www.eventbrite.com/d/ny--brooklyn/events/?q=sample+salon",
    ]:
        try:
            r = requests.get(search_url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.find_all(["article", "div"],
                                     class_=lambda c: c and "event" in str(c).lower()):
                text = el.get_text(" ", strip=True)
                anchor = el.find("a", href=True)
                url = anchor["href"] if anchor else ""
                if 20 < len(text) < 500 and "salon" in text.lower():
                    events.append({"text": text[:350], "url": url})
            if events:
                break
        except Exception:
            continue

    # Strategy 2: Partiful search (they post events here)
    if not events:
        try:
            r = requests.get(
                "https://partiful.com/search?q=sample+salon",
                headers=headers, timeout=12
            )
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for el in soup.find_all(["a", "div"]):
                    text = el.get_text(" ", strip=True)
                    if "salon" in text.lower() and "sample" in text.lower() and len(text) > 20:
                        href = el.get("href", "")
                        url = f"https://partiful.com{href}" if href.startswith("/") else href
                        events.append({"text": text[:350], "url": url})
        except Exception:
            pass

    return events[:10]



# ─────────────────────────────────────────────
# DICE EVENTS
# ─────────────────────────────────────────────

# DICE's own Next.js pages embed the full event list as JSON in a
# __NEXT_DATA__ script tag, so no API key and no headless browser are needed.
# The api.dice.fm host sits behind a Cloudflare bot challenge — we deliberately
# do not touch it. dice.fm/robots.txt allows crawling (Allow: /) and permits
# reference use, which is what linking to ticket pages is.
#
# Category-filtered browse pages are sorted by date ascending starting from
# today; the unfiltered page is sorted by popularity and is useless here.
# Pagination is client-side only, so each request yields roughly the next two
# to four days of events. That is fine: the dispatch runs Saturday 7:05am, so
# the events it needs are same-day and next-day and sit well inside page one.
DICE_CITY = "new_york-5bbf4db0f06331478e9b2c59"
DICE_BROWSE = f"https://dice.fm/browse/{DICE_CITY}"
DICE_CATEGORIES = [
    "music/gig", "music/dj", "music/party", "music/playback",
    "culture/comedy", "culture/theatre", "culture/art", "culture/film",
    "culture/social", "culture/talks", "culture/foodanddrink",
    "culture/workshop", "culture/wellbeing", "culture/sport", "culture/podcast",
]

# Sample Hall has its own section (iv.), so its shows are dropped here to
# avoid printing the same gig twice.
DICE_EXCLUDE_VENUES = {"sample hall"}

DICE_MAX_MILES = 5.0      # Fort Greene is the anchor; past this it's a trek
DICE_MAX_PER_VENUE = 2    # keep one busy venue from swallowing the section
DICE_MAX_PER_DAY = 6      # per day, so a busy Saturday can't crowd out Sunday

# ── The watchlist ────────────────────────────────────────────────────────────
# Venue and artist profile pages embed the same __NEXT_DATA__ JSON as the
# browse pages, but they list that room's whole run — one to five months out
# instead of the two-to-four days a browse page reaches. That depth is what
# lets the email name a show on the *following* Friday, six days ahead, and
# lets the companion page run a fortnight ahead.
#
# The tracked venues and artists (DICE_TRACKED_VENUES, DICE_TRACKED_ARTISTS)
# come from sample_profile.py. `slug: None` is honoured if a room ever needs to
# sit on the list unreadable; see DICE_VENUE_NOTES.

# Why a room may be dark, in plain words rather than an error code.
DICE_VENUE_NOTES = {}

# Tracked artists only count when they're playing New York — a tracked artist's date
# in Los Angeles is not actionable from Fort Greene.
DICE_ARTIST_LOCAL_ONLY = True

# Alex goes out on Fridays and Saturdays. Everything else is noise.
DICE_GOING_OUT_WEEKDAYS = (4, 5)   # Monday is 0

# The companion page rolls two weeks forward from the Saturday it's built on,
# so it always shows the next four going-out nights and never a stale tail.
DICE_WINDOW_DAYS = 14

# The email carries five picks total across the two dates; the companion page
# carries the rest.
DICE_HEADLINE_COUNT = 5


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
    "dekalb", "myrtle", "fort greene park",
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
    ("Clinton Hill",  ["clinton hill"]),
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
    # NOTE: The MTA, Sample Hall, and On Sale This Weekend sections are not
    # written by Claude. All three are rendered directly from structured data
    # (see fetch_mta_alerts / build_mta_section_html, fetch_sample_hall_events,
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

    prompt = f"""You are writing the Fort Greene Weekend Dispatch — a single, tightly-written Saturday morning email for Alex, a Fort Greene resident with a small dark-coated dog named Biscuit (~20 lbs).
He lives near DeKalb Ave and Washington Park. Union Square Greenmarket is a 1-hour walk with Biscuit.

Write in plain, direct prose. No markdown headers. No bullet lists. Five sections, each with a clear label line then 2–4 sentences of narrative. Be specific and useful, not generic.

=== WEATHER DATA ===
SATURDAY ({sat_str}):
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
Union Square Greenmarket verdict: {market_result['verdict']}
Reason: {market_result['reason']}
Sustained average temp during outing window: {market_avg_str} (peak {market_peak_str})
Market hours: {market_result['market_hours']}
Other Brooklyn Saturday markets found: {', '.join(market_names)}

IN-SEASON PRODUCE AT UNION SQUARE PLAZA THIS MONTH:
{get_seasonal_produce(saturday)}

=== BROOKLYN CULTURE NEWS ===
Items from six feeds. Each item tagged [BROOKLYN] or [MANHATTAN] where detectable.
Slot 6 this week: {slot6_winner}

Alex's interest profile for filtering (same as above — literary, NYC history, urbanism,
sci-fi, investigative journalism, bikes/surf/street culture, food, left politics):
{INTEREST_PROFILE}

{news_text}

=== INSTRUCTIONS ===

Write the dispatch in three labeled sections (the MTA and Sample Hall sections are generated separately — do NOT write them):

WEATHER + BISCUIT
2–3 sentences covering both days. Alex's default is leaving home between 10am and noon on weekends — he is not getting up at 6am unless forced. Work from that assumption. If the walk window data shows [RAIN FORCES EARLIER START], acknowledge that plainly and tell him rain is arriving mid-morning so he'll need to move if he wants to get Biscuit out dry. If the 10am–noon window is just hot (not rainy), tell him what to expect at that hour and how to manage it with Biscuit. Always include the evening window as a second option. Be specific about Saturday vs Sunday if they differ.

UNION SQUARE GREENMARKET
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
    C/G line letters in mention order (deduped); other lines are ignored
    because the alternate lookup only covers C/G.
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
      (c) otherwise the nearest other station on a running C/G service;
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

    # (c) nearest other station on a running C/G service
    for entry in ALTERNATE_STATIONS:
        if entry["stop_id"] == home_id:
            continue
        if usable(entry):
            running = "/".join(l for l in entry["lines"] if line_running(l))
            return (f'Nearest running alternative: {entry["name"]} '
                    f'([{running}], {entry["walk"]}).')

    # (d) nothing nearby is running
    return ("Every nearby C/G station is disrupted this weekend — "
            "check the MTA app before you head out.")


def build_home_station_banner(alerts):
    """
    Build the home-station status banner from the weekend-active C/G alerts,
    considering EXACTLY the two BANNER_STATIONS (Lafayette Av [C] and
    Fulton St [G]).

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
    Render the MTA section body: a home-station status banner on top, then a
    single 'Service changes on the home lines' list of every weekend-active
    change (flat — no your/elsewhere grouping), reusing the .news-item cards.
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
        return bool(mentioned - set(HOME_LINES))
    change_alerts = [a for a in alerts if not _is_system_wide(a)]
    if change_alerts:
        cards = ""
        for a in change_alerts:
            # One card per alert; each route gets its own label row (no "A/C" mashup).
            route_labels = "".join(
                f'<div class="news-source">{_html.escape(r)} line</div>'
                for r in (a.get("routes", []) or [])
            )
            cards += f"""
            <div class="news-item">
              {route_labels}
              <div class="news-hed">{_html.escape(a["text"])}</div>
            </div>"""
        if cards:
            html += f"""
          <div class="mta-group">
            <div class="mta-group-label">Service changes on the {' &amp; '.join(HOME_LINES)}</div>
            <div class="news-feed">{cards}</div>
          </div>"""

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
#   pigeon-grey.png — also used on the envelope stamp in this copy
ASSET_BASE_URL = os.environ.get(
    "WEEKEND_ASSET_BASE_URL",
    "https://YOUR-ORG.github.io/weekend-dispatch-showcase/assets",
).rstrip("/")
PIGEON_GREY_URL = f"{ASSET_BASE_URL}/pigeon-grey.png"
PIGEON_STAMP_URL = f"{ASSET_BASE_URL}/pigeon-grey.png"
CREST_URL = f"{ASSET_BASE_URL}/crest-placeholder.png"   # neutral placeholder crest for the newsletter footer


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
            ("East vs west", "Eastern birds flash yellow under the wings; western birds, like the ones on the Pacific coast, flash red."),
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
            ("Where", "Damp, mossy conifer forest. Pacific Spirit Regional Park in Vancouver is classic habitat, especially in winter."),
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
            ("Where", "Conifer forests and leafy neighbourhoods across coastal B.C., including Vancouver&rsquo;s parks."),
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
            ("Where", "Closer than you think: several line the blocks around Fort Greene Park."),
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
            ("Where", "Moist lowland forest across coastal B.C., including Pacific Spirit Regional Park in Vancouver."),
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
        "then": "Central Brooklyn sits on the <strong>Harbor Hill terminal moraine</strong>, the ridge of rock and gravel the last ice sheet dumped when it stopped here roughly 20,000 years ago. Eastern Parkway rides its crest. South of it lies Flatbush&rsquo;s flat outwash plain. This was Lenape land. In the 1800s the ridge was called <strong>Crow Hill</strong>. At its eastern edge, <strong>Weeksville</strong> (founded 1838) became one of the country&rsquo;s largest free Black communities.",
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
# starred. Rotation alternates boroughs, alphabetical within each (78 weeks).
ROTATION_START = datetime.date(2026, 10, 3)
TREE_MAP_ROTATION = [
    ("BK27", "Bath Beach"), ("MN25", "Battery Park City-Lower Manhattan"), ("BK31", "Bay Ridge"),
    ("MN03", "Central Harlem North-Polo Grounds"), ("BK75", "Bedford"), ("MN11", "Central Harlem South"),
    ("BK29", "Bensonhurst East"), ("MN27", "Chinatown"), ("BK28", "Bensonhurst West"),
    ("MN15", "Clinton"), ("BK88", "Borough Park"), ("MN34", "East Harlem North"),
    ("BK19", "Brighton Beach"), ("MN33", "East Harlem South"), ("BK09", "Brooklyn Heights-Cobble Hill"),
    ("MN22", "East Village"), ("BK81", "Brownsville"), ("MN21", "Gramercy"),
    ("BK77", "Bushwick North"), ("MN04", "Hamilton Heights"), ("BK78", "Bushwick South"),
    ("MN13", "Hudson Yards-Chelsea-Flatiron-Union Square"), ("BK50", "Canarsie"), ("MN31", "Lenox Hill-Roosevelt Island"),
    ("BK33", "Carroll Gardens-Columbia Street-Red Hook"), ("MN14", "Lincoln Square"), ("BK69", "Clinton Hill"),
    ("MN28", "Lower East Side"), ("BK61", "Fort Greene North"), ("MN06", "Manhattanville"),
    ("BK63", "Fort Greene South"), ("MN01", "Marble Hill-Inwood"), ("BK83", "Cypress Hills-City Line"),
    ("MN17", "Midtown-Midtown South"), ("BK38", "DUMBO-Vinegar Hill-Downtown Brooklyn-Boerum Hill"), ("MN09", "Morningside Heights"),
    ("BK30", "Dyker Heights"), ("MN20", "Murray Hill-Kips Bay"), ("BK91", "East Flatbush-Farragut"),
    ("MN24", "SoHo-TriBeCa-Civic Center-Little Italy"), ("BK82", "East New York"), ("MN50", "Stuyvesant Town-Cooper Village"),
    ("BK85", "East New York (Pennsylvania Ave)"), ("MN19", "Turtle Bay-East Midtown"), ("BK90", "East Williamsburg"),
    ("MN40", "Upper East Side-Carnegie Hill"), ("BK95", "Erasmus"), ("MN12", "Upper West Side"),
    ("BK42", "Flatbush"), ("MN35", "Washington Heights North"), ("BK58", "Flatlands"),
    ("MN36", "Washington Heights South"), ("BK68", "Fort Greene"), ("MN23", "West Village"),
    ("BK45", "Georgetown-Marine Park-Bergen Beach-Mill Basin"), ("MN32", "Yorkville"), ("BK26", "Gravesend"),
    ("BK76", "Greenpoint"), ("BK25", "Homecrest"), ("BK41", "Kensington-Ocean Parkway"),
    ("BK44", "Madison"), ("BK43", "Midwood"), ("BK73", "North Side-South Side"),
    ("BK79", "Ocean Hill"), ("BK46", "Ocean Parkway South"), ("BK37", "Park Slope-Gowanus"),
    ("BK64", "Prospect Heights"), ("BK60", "Prospect Lefferts Gardens-Wingate"), ("BK96", "Rugby-Remsen Village"),
    ("BK21", "Seagate-Coney Island"), ("BK17", "Sheepshead Bay-Gerritsen Beach-Manhattan Beach"), ("BK93", "Starrett City"),
    ("BK35", "Stuyvesant Heights"), ("BK34", "Sunset Park East"), ("BK32", "Sunset Park West"),
    ("BK23", "West Brighton"), ("BK72", "Williamsburg"), ("BK40", "Windsor Terrace"),
]
# HOME_LATLON comes from sample_profile.py
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
    body += f"""
          <div class="sl-kicker">{entry["kicker"]}</div>
          <div class="xs-hed">{entry["name"]}</div>
          <div class="xs-sub">{entry["tagline"]}</div>
          {photo_html}
          <dl class="xs-kv">{facts}</dl>{cons}"""
    return body


def build_nature_sections_html(saturday):
    """
    Bird of the Week, Tree of the Week, Map of the Week, Then & Now.
    Each is independent and fail-closed: an error drops that one section.
    """
    out = []
    romans = iter(["vii.", "viii.", "ix.", "x.", "xi.", "xii."])
    month = saturday.month

    birds = [b for b in NATURE_BIRDS if month in b["months"]]
    for label, bank, salt in (("Bird of the Week", birds, 0), ("Tree of the Week", NATURE_TREES, 3)):
        try:
            entry = _week_pick(bank, saturday, salt)
            if entry:
                out.append(_nature_section(label, next(romans), _species_card(entry)))
        except Exception as e:
            print(f"  [nature] {label} skipped: {e}")

    try:
        out.append(_nature_section("Map of the Week", next(romans),
                                   build_tree_map_html(map_of_the_week(saturday))))
    except Exception as e:
        print(f"  [nature] Map of the Week skipped: {e}")

    try:
        tn = _week_pick(THEN_AND_NOW, saturday, 1)
        out.append(_nature_section("Then &amp; Now", next(romans), f"""
          <div class="xs-hed">{tn["hed"]}</div>
          <div class="xs-cols">
            <div><div class="xs-col-label">Then</div><p>{tn["then"]}</p></div>
            <div><div class="xs-col-label">Now</div><p>{tn["now"]}</p></div>
          </div>"""))
    except Exception as e:
        print(f"  [nature] Then & Now skipped: {e}")

    try:
        shore = build_shore_leave_html(saturday)
        if shore.strip():
            out.append(_nature_section("Shore Leave", next(romans), shore))
    except Exception as e:
        print(f"  [nature] Shore Leave skipped: {e}")

    return "".join(out)


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


def build_email_html(narrative, saturday_data, sunday_data, saturday, sunday,
                     mta_alerts, surface_alerts, pr_events, dice_picks=None,
                     long_list_url="", edible_picks=None):
    sat_label = saturday.strftime("%A, %B %d")
    sun_label = sunday.strftime("%B %d")
    sat_date_top = saturday.strftime("%B %d")
    sun_date_top = sunday.strftime("%B %d")
    year = saturday.year

    # Parse narrative into labeled sections
    section_order = [
        "MTA THIS WEEKEND",
        "WEATHER + BISCUIT",
        "UNION SQUARE GREENMARKET",
        "SAMPLE HALL",
        "ON SALE THIS WEEKEND",
        "AROUND BROOKLYN",
    ]
    section_labels_display = [
        ("MTA This Weekend", "i."),
        ("Weather + Biscuit", "ii."),
        ("Union Square Greenmarket", "iii."),
        ("Sample Hall", "iv."),
        ("On Sale This Weekend", "v."),
        ("Around Brooklyn", "vi."),
    ]

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
        label, roman = section_labels_display[i]
        content = parsed.get(key, "")

        if key == "MTA THIS WEEKEND":
            # MTA is rendered from structured alert data, not Claude prose:
            # a home-station status banner plus a single C/G change list.
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

            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          <div class="news-feed">{items_html}</div>
        </div>"""
        elif key == "SAMPLE HALL":
            # Rendered directly from scraped show data — no Claude prose, no
            # commentary. Same card anatomy as Around Brooklyn: small-caps
            # eyebrow (date · type), serif headline (artists, linked to
            # tickets), dek (time @ room).
            import html as _html
            items_html = ""
            for e in pr_events:
                # e["text"] is "Sat Jul 5 — Club — 3:00 pm @ Sound Room — Artists"
                parts = [p.strip() for p in e.get("text", "").split(" — ")]
                date_str  = parts[0] if parts else ""
                show_type = e.get("type", "") or (parts[1] if len(parts) > 1 else "")
                time_room = parts[2] if len(parts) > 2 else ""
                artists   = " — ".join(parts[3:]) if len(parts) > 3 else ""
                source_label = " &middot; ".join(
                    _html.escape(p) for p in (date_str, show_type) if p
                )
                hed_text = _html.escape(artists if artists else time_room)
                url = e.get("url", "")
                hed = f'<a href="{_html.escape(url)}">{hed_text}</a>' if url else hed_text
                dek = _html.escape(time_room) if artists else ""
                items_html += f"""
          <div class="news-item">
            <div class="news-source">{source_label}</div>
            <div class="news-headline-row">
              <div class="news-hed">{hed}</div>
              {"<div class='news-dek'>" + dek + "</div>" if dek else ""}
            </div>
          </div>"""
            if not items_html:
                items_html = "<p>No Saturday shows at Sample Hall this week.</p>"
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
            for e in (dice_picks or []):
                eyebrow_bits = [
                    e["start"].strftime("%a %-d %b &middot; %-I:%M %p"),
                    e.get("tracked_venue") or e.get("venue", ""),
                ]
                eyebrow = " &middot; ".join(b for b in eyebrow_bits if b)

                hed_text = _html.escape(e["name"])
                url = e.get("url", "")
                hed = f'<a href="{_html.escape(url)}">{hed_text}</a>' if url else hed_text

                dek_bits = []
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
        else:
            paras = [p.strip() for p in content.split("\n\n") if p.strip()]
            paras_html = "".join(f"<p>{p}</p>" for p in paras) if paras else f"<p>{content}</p>"
            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          {paras_html}
        </div>"""

    # Pigeon silhouette (reused via <use>)
    nature_html = build_nature_sections_html(saturday)

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
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
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
{NATURE_CSS}</style>
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
    <img class="b4-crest" src="{CREST_URL}" width="88" height="88" alt="Dispatch crest">
  </div>

</div>
</body>
</html>"""
    return html


def build_long_list_html(watchlist, sweep_events, saturday, friday, picked_urls=()):
    """
    The companion page: everything the email had no room for.

    Scoped the way Alex actually goes out — Fridays and Saturdays only, rolling
    two weeks forward from the Saturday this was built on. Four nights, no
    weekday filler, no stale tail.

    Two passes over the same data, because they answer different questions.
    The ledger comes first — every tracked show in date order, with the five
    that made the email marked so it's clear what was left out. Then the rooms,
    one card per venue, for when the question is "what's on at Sample Venue A" rather
    than "what's on Friday".
    """
    import html as _html

    window = dice_going_out_dates(saturday)
    window_set = set(window)

    def esc(s):
        return _html.escape(str(s or ""))

    def event_row(e, cls="lrow", stamp="time"):
        """
        `stamp` picks the left column. The ledger is already grouped under a
        date heading, so a time is enough there; a room's card spans weeks, so
        it has to carry the date or every row reads as the same night.
        """
        picked = ' is-picked' if e.get("url") and e["url"] in picked_urls else ''
        name = f'<a href="{esc(e["url"])}">{esc(e["name"])}</a>' if e.get("url") else esc(e["name"])
        bits = [b for b in (e.get("price"), "Sold out" if e.get("sold_out") else "") if b]
        meta = f'<span class="lmeta">{esc(" · ".join(bits))}</span>' if bits else ""
        when = (e['start'].strftime('%-I:%M %p').lower() if stamp == "time"
                else e['start'].strftime('%a %-d %b'))
        return f"""
          <li class="{cls}{picked}">
            <span class="ltime">{when}</span>
            <span class="lname">{name}{meta}</span>
            <span class="lvenue">{esc(e.get('tracked_venue') or e.get('venue'))}</span>
          </li>"""

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
        rows = ""
        for e in in_window:
            rows += event_row(e, "rrow", stamp="date")
        if not rows:
            rows = f'<li class="rempty">{esc(entry.get("note") or "Nothing on a Friday or Saturday in the next fortnight.")}</li>'
        rooms_html += f"""
      <article class="room room--{state}">
        <header class="roomhead">
          <h3>{esc(label)}</h3><span class="pill pill--{state}">{esc(pill)}</span>
        </header>
        <p class="roomwhere">{esc(where)}</p>
        <ul class="rows">{rows}</ul>
      </article>"""

    for label, entry in watchlist["artists"].items():
        rows = ""
        for e in entry["events"][:5]:
            city = ""
            parts = [p.strip() for p in (e.get("address") or "").split(",")]
            if len(parts) > 2:
                city = parts[-3]
            rows += f"""
          <li class="rrow">
            <span class="ltime">{e['start'].strftime('%-d %b')}</span>
            <span class="lname"><a href="{esc(e['url'])}">{esc(e['venue'])}</a></span>
            <span class="lvenue">{esc(city)}</span>
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
        <ul class="rows">{rows}</ul>
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
    # gets a heading. A missing Friday should read as "nothing booked", not as
    # a night that quietly didn't exist.
    ledger_html = ""
    for d in window:
        headline = d in (saturday, friday)
        tag = '<span class="daytag">in the email</span>' if headline else ""
        rows = "".join(event_row(r) for r in by_date.get(d, []))
        if not rows:
            rows = '<li class="rempty">Nothing booked at the tracked rooms.</li>'
        ledger_html += f"""
      <section class="daygroup{' is-headline' if headline else ''}">
        <h3 class="dayhead"><span>{d.strftime('%A, %B %-d')}</span>{tag}</h3>
        <ul class="rows">{rows}</ul>
      </section>"""

    # ── the wider sweep ──────────────────────────────────────────────────
    sweep_html = ""
    sweep_by_date = {}
    for e in (sweep_events or []):
        sweep_by_date.setdefault(e["date"], []).append(e)
    for d in sorted(sweep_by_date):
        rows = "".join(event_row(e) for e in sweep_by_date[d])
        sweep_html += f"""
      <section class="daygroup">
        <h3 class="dayhead"><span>{d.strftime('%A, %B %-d')}</span></h3>
        <ul class="rows">{rows}</ul>
      </section>"""
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
<meta name="robots" content="noindex, nofollow">
<title>The Long List &middot; Weekend Dispatch</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
  :root {{
    --ground:#EDE0C0; --card:#F5EDD8; --panel:#EFE3C3;
    --ink:#1A1208; --body:#2A1E0A; --muted:#6A5030; --faint:#B89A5A;
    --accent:#9B3A1A; --rule:#D4B98A; --rule-soft:#C4A46A;
    --serif:'Cormorant Garamond',Georgia,'Times New Roman',serif;
    --mono:'Courier Prime','Courier New',ui-monospace,monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --ground:#141009; --card:#1E1710; --panel:#262017;
      --ink:#F3E9D2; --body:#E4D7BC; --muted:#B49F76; --faint:#8A7550;
      --accent:#E07A4E; --rule:#3A2F20; --rule-soft:#4A3D2A;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--ground); color:var(--body);
          font-family:var(--serif); font-size:17px; line-height:1.6;
          -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:26px 18px 64px; }}
  a {{ color:var(--accent); }}
  .masthead {{ background:var(--card); border:1px solid var(--rule); padding:24px; }}
  .kicker {{ font-family:var(--mono); font-size:9px; letter-spacing:.3em;
             text-transform:uppercase; color:var(--muted); margin:0 0 10px; }}
  .masthead h1 {{ font-size:clamp(28px,5.5vw,42px); line-height:1.06; margin:0;
                  color:var(--ink); font-weight:600; text-wrap:balance; }}
  .masthead h1 em {{ color:var(--accent); font-style:normal; }}
  .standfirst {{ margin:12px 0 0; color:var(--muted); font-style:italic; max-width:60ch; }}
  .rule-sm {{ width:32px; height:2px; background:var(--accent); margin:14px 0 0; }}
  .seclabel {{ display:flex; align-items:center; gap:12px; margin:46px 0 14px; }}
  .seclabel span:first-child {{ font-family:var(--mono); font-size:9px;
      letter-spacing:.26em; text-transform:uppercase; color:var(--accent); white-space:nowrap; }}
  .seclabel .bar {{ flex:1; height:1px; background:var(--rule); }}
  .seclabel .rom {{ font-style:italic; font-size:13px; color:var(--faint); }}
  .panel {{ background:var(--card); border:1px solid var(--rule); padding:20px; }}
  .roomgrid {{ display:grid; gap:14px;
               grid-template-columns:repeat(auto-fit,minmax(252px,1fr)); }}
  .room {{ background:var(--card); border:1px solid var(--rule); padding:15px 17px 17px; }}
  .room--quiet, .room--unlisted {{ background:var(--panel); }}
  .roomhead {{ display:flex; align-items:center; justify-content:space-between; gap:9px; }}
  .roomhead h3 {{ margin:0; font-size:19px; color:var(--ink); font-weight:600; }}
  .pill {{ font-family:var(--mono); font-size:7.5px; letter-spacing:.14em;
           text-transform:uppercase; padding:3px 7px; white-space:nowrap; }}
  .pill--live {{ background:var(--accent); color:var(--card); }}
  .pill--quiet {{ background:var(--rule); color:var(--muted); }}
  .pill--unlisted {{ border:1px solid var(--rule-soft); color:var(--muted); }}
  .roomwhere {{ font-family:var(--mono); font-size:8.5px; letter-spacing:.16em;
                text-transform:uppercase; color:var(--faint); margin:4px 0 11px; }}
  .rows {{ list-style:none; margin:0; padding:0; display:flex;
           flex-direction:column; gap:8px; }}
  .lrow, .rrow {{ display:grid; grid-template-columns:76px 1fr auto; gap:12px;
                  align-items:baseline; padding-left:11px;
                  border-left:2px solid var(--rule); }}
  .rrow {{ grid-template-columns:62px 1fr auto; }}
  /* Inside a room's own card the venue name is the heading, so repeating it on
     every row is just noise. Artist cards keep it — there it's the city. */
  .room:not(.room--artist) .rrow .lvenue {{ display:none; }}
  .room:not(.room--artist) .rrow {{ grid-template-columns:76px 1fr; }}
  .rrow .ltime {{ white-space:nowrap; }}
  .lrow.is-picked {{ border-left-color:var(--accent); }}
  .ltime {{ font-family:var(--mono); font-size:9.5px; letter-spacing:.06em;
            color:var(--faint); font-variant-numeric:tabular-nums; }}
  .lname a {{ color:var(--ink); text-decoration:none;
              border-bottom:1px solid var(--rule); font-weight:600; }}
  .lname a:hover {{ color:var(--accent); border-bottom-color:var(--accent); }}
  .lmeta {{ display:block; font-style:italic; font-size:13.5px; color:var(--muted); }}
  .lvenue {{ font-family:var(--mono); font-size:8.5px; letter-spacing:.14em;
             text-transform:uppercase; color:var(--muted); white-space:nowrap; }}
  .rempty {{ font-style:italic; color:var(--muted); font-size:14.5px; }}
  .daygroup + .daygroup {{ margin-top:20px; padding-top:18px;
                           border-top:1px solid var(--rule); }}
  .dayhead {{ display:flex; align-items:baseline; gap:10px; margin:0 0 11px;
              font-family:var(--mono); font-size:9.5px; letter-spacing:.18em;
              text-transform:uppercase; color:var(--muted); font-weight:400; }}
  .is-headline .dayhead {{ color:var(--accent); }}
  .daytag {{ font-family:var(--mono); font-size:7.5px; letter-spacing:.14em;
             text-transform:uppercase; color:var(--card); background:var(--accent);
             padding:3px 6px; }}
  .foot {{ margin-top:48px; padding-top:16px; border-top:1px solid var(--rule);
           font-family:var(--mono); font-size:8.5px; letter-spacing:.16em;
           text-transform:uppercase; color:var(--faint);
           display:flex; flex-wrap:wrap; gap:10px; justify-content:space-between; }}
  a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
  @media (max-width:560px) {{
    .lrow, .rrow {{ grid-template-columns:66px 1fr; }}
    .lvenue {{ grid-column:2; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <p class="kicker">Weekend Dispatch &middot; Companion Page</p>
    <h1>The Long List<br><em>every room, every night</em></h1>
    <p class="standfirst">The five picks in the email came from here. This is the
      rest of it &mdash; every Friday and Saturday at the tracked rooms between
      now and {esc(window[-1].strftime('%-d %B'))}, plus whatever else DICE has
      within five miles of Fort Greene.</p>
    <div class="rule-sm"></div>
  </header>

  <div class="seclabel"><span>The Ledger</span><div class="bar"></div><span class="rom">i.</span></div>
  <div class="panel">{ledger_html}
  </div>

  <div class="seclabel"><span>The Rooms</span><div class="bar"></div><span class="rom">ii.</span></div>
  <div class="roomgrid">{rooms_html}
  </div>

  <div class="seclabel"><span>Elsewhere in Brooklyn</span><div class="bar"></div><span class="rom">iii.</span></div>
  <div class="panel">{sweep_html}
  </div>

  <div class="foot">
    <span>Fort Greene &middot; Brooklyn</span>
    <span>Source: DICE &middot; generated {esc(generated)}</span>
  </div>
</div>
</body>
</html>"""


def save_long_list_html(html):
    """Write the companion page next to the newsletter and return its URL."""
    import pathlib
    path = pathlib.Path.cwd() / "events.html"
    path.write_text(html, encoding="utf-8")
    print(f"  Long list saved: {path}")
    pages_url = os.environ.get("GITHUB_PAGES_URL")
    if pages_url:
        return pages_url.rsplit("/", 1)[0] + f"/events.html?v={PAGES_VERSION}"
    return f"file://{path}"


def save_newsletter_html(html, saturday):
    """
    Save newsletter as weekend.html in the current directory (git repo),
    commit and push to GitHub, then return the GitHub Pages URL.
    Falls back to local file:// URL if GITHUB_PAGES_URL not set.
    """
    import pathlib

    # Save to current working directory as weekend.html
    path = pathlib.Path.cwd() / "weekend.html"
    path.write_text(html, encoding="utf-8")
    print(f"  Newsletter saved: {path}")

    publish_pages(["weekend.html", "events.html"])

    # Return GitHub Pages URL if set, otherwise local file path. The ?v= stamp
    # makes every issue a fresh URL so neither Pages' CDN (10-minute cache) nor
    # a browser that opened last week's link can serve the old page.
    pages_url = os.environ.get("GITHUB_PAGES_URL")
    if pages_url:
        return f"{pages_url}?v={PAGES_VERSION}"
    return f"file://{path}"


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


# The envelope is a photo (assets/envelope-placeholder.jpg: placeholder envelope photo; the
# private copy used a custom envelope image) with the postmark's centre left blank.
# Each issue stamps that Saturday's date into the postmark and the result is
# attached to the email inline (cid:), so it shows without waiting on Pages.
ENVELOPE_PHOTO = "assets/envelope-placeholder.jpg"
POSTMARK_FONT = "assets/fonts/Jost-Regular.ttf"    # SIL OFL, see assets/fonts/Jost-OFL.txt
POSTMARK_X, POSTMARK_MONTH_Y, POSTMARK_DAY_Y, POSTMARK_MAX_W = 809, 351, 370, 58
POSTMARK_INK = (44, 40, 36)
ENVELOPE_CID = "envelope-photo"


def build_envelope_photo(saturday):
    """Return JPEG bytes of the desk envelope with this Saturday's date in the postmark."""
    import io
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
    buf = io.BytesIO()
    Image.fromarray(arr.clip(0, 255).astype("uint8")).save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


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
    <img class="env-photo" src="{photo_src}" width="620" height="393" alt="A cream envelope sealed with a wax seal, addressed to Alex Sample, with a Brooklyn pigeon stamp postmarked {saturday.strftime('%B %d')}, on a walnut desk">
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
    <p class="peek-text">Your full dispatch is ready &mdash; weather &amp; Biscuit walk windows, the Greenmarket verdict, what&rsquo;s on at Sample Hall, what&rsquo;s on sale this weekend, what&rsquo;s going on around Brooklyn, this week&rsquo;s bird, tree and street-tree map, and the Rockaway tides.</p>
    <div class="cta-wrap">
      <a class="cta-btn" href="{newsletter_url}">&darr;&nbsp;&nbsp;Open Full Newsletter</a>
    </div>
  </div>

  <div class="bottom-note">Weekend Dispatch &middot; Fort Greene &middot; {year}</div>
</div>
</body>
</html>"""
    return html


def send_email(html_body, saturday, images=None):
    sender = "".join(os.environ["DISPATCH_EMAIL"].split())
    password = "".join(os.environ["DISPATCH_APP_PASSWORD"].split())
    recipient = "".join(os.environ["DISPATCH_TO"].split())

    sat_label = saturday.strftime("%B %d")
    subject = f"Weekend Dispatch · {sat_label}"

    # alternative[ text/plain, related[ text/html, inline images ] ]. The HTML
    # must be the first (root) part of the multipart/related that holds the
    # images: iOS Mail won't resolve cid: links into a nested alternative, and
    # showed the envelope photo as blank space while Mac Mail coped.
    from email.mime.image import MIMEImage
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("The Dispatch Desk", sender))
    msg["To"] = recipient
    text = BeautifulSoup(html_body, "html.parser").get_text("\n", strip=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    related = MIMEMultipart("related", type="text/html")
    related.attach(MIMEText(html_body, "html", "utf-8"))
    for cid, data in (images or {}).items():
        part = MIMEImage(data, "jpeg")
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("X-Attachment-Id", cid)
        part.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
        related.attach(part)
    msg.attach(related)

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
    MTA and Sample Hall sections are unaffected — they are rendered from
    live structured data, not this.
    """
    ph = "[Preview placeholder — set ANTHROPIC_API_KEY to generate this section.]"
    return (
        f"WEATHER + BISCUIT\n{ph}\n\n"
        f"UNION SQUARE GREENMARKET\n{ph}\n\n"
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
    Sample Hall sections are always built for real from live data.

    Returns (newsletter_html, narrative, saturday, sunday).
    """
    # Determine weekend dates
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    print(f"Covering weekend: {saturday.strftime('%b %d')} – {sunday.strftime('%b %d')}")

    # 1. Weather
    print("Fetching weather...")
    weather_data = fetch_weekend_weather()
    weekend = parse_weekend_days(weather_data)
    saturday_data = weekend.get("saturday", {})
    sunday_data = weekend.get("sunday", {})

    # 2. Walk windows
    print("Computing Biscuit walk windows...")
    sat_windows = get_walk_windows(saturday_data, "saturday") if saturday_data else []
    sun_windows = get_walk_windows(sunday_data, "sunday") if sunday_data else []

    # 3. MTA — planned C/G service changes active this weekend (flat list)
    print("Fetching MTA alerts...")
    mta_alerts = fetch_mta_alerts(saturday, sunday)
    print(f"  C/G weekend service changes: {len(mta_alerts)}")
    surface_alerts = fetch_surface_transit_alerts()
    if surface_alerts:
        print(f"  Surface-transit disruptions: "
              f"{', '.join(sorted({a['service'] for a in surface_alerts}))}")
    else:
        print("  No B38/B52/AirTrain/LIRR disruptions — services omitted")

    # 4. Farmers market
    print("Fetching market data...")
    market_list = fetch_market_data()
    market_result = market_go_nogo(saturday_data, market_list)

    # 5. Sample Hall events
    print("Scraping Sample Hall events...")
    pr_events = fetch_sample_hall_events()
    print(f"  Sample Hall events found: {len(pr_events)}")

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

    # 9. Companion page first — the email needs its URL to link to.
    print("Building the long list...")
    picked_urls = {p["url"] for p in dice_picks if p.get("url")}
    long_list_html = build_long_list_html(dice_watchlist, dice_sweep,
                                          saturday, friday, picked_urls)

    # 10. Build newsletter HTML
    print("Building newsletter...")
    pages_url = os.environ.get("GITHUB_PAGES_URL")
    long_list_url = (pages_url.rsplit("/", 1)[0] + f"/events.html?v={PAGES_VERSION}") if pages_url else ""
    newsletter_html = build_email_html(narrative, saturday_data, sunday_data,
                                       saturday, sunday, mta_alerts, surface_alerts,
                                       pr_events, dice_picks, long_list_url,
                                       edible_picks)
    return newsletter_html, narrative, saturday, sunday, long_list_html


def run_preview():
    """
    Local preview: build the full newsletter exactly as main() does — same
    fetches, same section rendering — but write it to weekend_preview.html and
    open it in the browser with `open`. Sends NO email and runs NO git commands.

    Uses ANTHROPIC_API_KEY from the env for the Claude-written sections; if it
    is not set, those sections fall back to placeholder text so the layout
    still renders. The MTA section always uses live data.
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

    newsletter_html, _narrative, _saturday, _sunday, long_list_html = \
        assemble_newsletter(client)

    path = pathlib.Path.cwd() / "weekend_preview.html"
    path.write_text(newsletter_html, encoding="utf-8")
    print(f"  Wrote {path}")

    long_path = pathlib.Path.cwd() / "events_preview.html"
    long_path.write_text(long_list_html, encoding="utf-8")
    print(f"  Wrote {long_path}")

    subprocess.run(["open", str(path)], check=False)
    subprocess.run(["open", str(long_path)], check=False)
    print(f"✓ Preview opened in browser: {path}")


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

    # Steps 1–10: fetch everything and build both pages
    newsletter_html, narrative, saturday, sunday, long_list_html = \
        assemble_newsletter(client)

    # 11. Save + push to GitHub Pages. The long list is written first so it is
    #     part of the same commit the newsletter push carries.
    long_list_url = save_long_list_html(long_list_html)
    print(f"  Long list: {long_list_url}")
    newsletter_url = save_newsletter_html(newsletter_html, saturday)
    print(f"  Newsletter saved: {newsletter_url}")

    # 12. Build envelope email + send
    print("Building envelope email...")
    envelope_html = build_envelope_email(saturday, sunday, newsletter_url)
    envelope_photo = build_envelope_photo(saturday)

    print("Sending...")
    send_email(envelope_html, saturday, {ENVELOPE_CID: envelope_photo})

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
        help="Build weekend_preview.html and open it locally — no email, no git."
    )
    args = parser.parse_args()

    if args.preview:
        run_preview()
    else:
        main()
