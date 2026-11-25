"""
MTA Weekender and the plain-wording MTA service changes.

Two things live here:

  * The subway service changes the MTA publishes, turned from the MTA's own
    alert wording (full of [A]-style route brackets) into plain sentences, with
    the days and hours each change applies. They feed the "MTA This Weekend"
    section (the A, C and 3 only) and the System Changes page (every line).
  * "MTA Weekender": three car-free day trips by train, one each from
    Metro-North, the LIRR and NJ Transit, drawn from a hand-vetted library
    (excursions.json). Nothing about a trip is written by a model. The live
    Metro-North and LIRR alert feeds are checked for the trip's line, and NJ
    Transit, which publishes no open alerts feed, is pointed to its own site.

Everything fails closed: if a feed cannot be read, its part is left out and the
rest of the page is unaffected.
"""

import datetime
import functools
import html as _html
import json
import os
import re
from zoneinfo import ZoneInfo

import requests

import dispatch_media as dm
import dispatch_pages as dp

TIMEZONE = "America/New_York"
SUBWAY_FILE = "subway.html"
LIBRARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "excursions.json")
FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2F{}-alerts.json"
FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
HOME_LINES = ["A", "C", "3"]
NJT_URL = "https://www.njtransit.com/"
RAILROAD_ORDER = ("Metro-North", "LIRR", "NJ Transit")
RAIN_SKIP_FAIR_WEATHER = 50   # % chance of rain at which outdoor-heavy trips are set aside


def esc(s):
    return _html.escape(str(s if s is not None else ""))


# ─────────────────────────────────────────────
# Alert wording: MTA brackets -> plain sentences
# ─────────────────────────────────────────────

_ROUTE = r"\[([A-Z0-9]{1,3})\]"
_RUN = re.compile(r"(?:" + _ROUTE + r")(?:[ ,]*(?:and )?" + _ROUTE + r")*")
_SINGULAR_TO_PLURAL = {
    "skips": "skip", "stops": "stop", "runs": "run", "is": "are", "makes": "make",
    "operates": "operate", "uses": "use", "terminates": "terminate", "has": "have",
    "does": "do", "bypasses": "bypass", "begins": "begin", "ends": "end",
}
_PLURAL_VERBS = set(_SINGULAR_TO_PLURAL.values()) | {"will", "are"}
_VERBS = set(_SINGULAR_TO_PLURAL) | _PLURAL_VERBS
_SERVICE_WORDS = {"local", "express"}


def _join_routes(routes):
    if len(routes) == 1:
        return routes[0]
    return ", ".join(routes[:-1]) + " and " + routes[-1]


def _flatten(text):
    """One line of text: line breaks become sentence breaks, icon placeholders like [shuttle bus icon] go."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    t = ""
    for ln in lines:
        t += (" " if not t or t[-1] in ".!?:" else ". ") + ln if t else ln
    t = re.sub(r"\s*\[[^\]]*icon\]", "", t, flags=re.I)
    return " ".join(t.split())


def _truncate(text, limit):
    """Cut at the last whole sentence that fits (a numbered-list '1.' does not count), else at a word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"(?<!\b\d)[.!?](?= )", cut)]
    if ends and ends[-1] > limit * 0.4:
        return cut[:ends[-1]]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…"


def plain_alert_text(text, detail=False):
    """
    The MTA's alert wording in plain sentences.

    The MTA marks every route with brackets ("no [C] between 145 St and 168 St")
    and joins sentences with a spaced hyphen. This takes the brackets off, adds
    "trains" where the bracket stands for the trains themselves and makes the
    verb agree, and ends up with "No C trains between 145 St and 168 St". Only
    punctuation and agreement change: every station, line and direction is the
    MTA's own. Anything the rules do not recognise is left as the MTA wrote it,
    minus the brackets.
    """
    t = _flatten(text)
    if not t:
        return ""
    t = re.sub(r"\s+-\s+", ". ", t)            # "suspended - Take the [J]" -> "suspended. Take the [J]"

    out, pos = [], 0
    for m in _RUN.finditer(t):
        out.append(t[pos:m.start()])
        before = t[:m.start()]
        after = t[m.end():]
        routes = re.findall(_ROUTE, m.group(0))
        label = _join_routes(routes)

        service = re.match(r"\s+(local|express)\b", after)
        tail = after[service.end():] if service else after
        verb = re.match(r"\s+([a-z]+)\b", tail)
        verb = verb.group(1) if verb else None
        after_service = tail if service else after

        noun_before = bool(re.search(r"(?:\bno\s|-bound\s|\b(?:uptown|downtown|local|express)\s)$", before, re.I))
        take = bool(re.search(r"\b(?:take|use|via|on|to|by|from|instead of)\s+(?:the\s+)?$", before, re.I))
        and_next = bool(re.match(r"\s+and\s+", tail))
        add_noun = ((not take) and not re.match(r"\s+trains?\b", after_service)
                    and (verb in _VERBS or noun_before or (service and and_next)))
        if detail:
            add_noun = False
            label = "/".join(routes)

        piece = label
        if service:
            piece += " " + service.group(1)
        if add_noun:
            piece += " trains"
        elif not take and re.match(r"\s+[A-Z]", after_service) and not service:
            piece = "the " + piece          # "[H] Rockaway Park Shuttle" -> "the H Rockaway Park Shuttle"
        out.append(piece)
        pos = m.end() + (service.end() if service else 0)

        if add_noun and verb in _SINGULAR_TO_PLURAL:
            vm = re.match(r"(\s+)" + verb + r"\b", t[pos:])
            if vm:
                out.append(vm.group(1) + _SINGULAR_TO_PLURAL[verb])
                pos += vm.end()
    out.append(t[pos:])
    t = "".join(out)
    if " trains" in t:
        t = re.sub(r"\band (skips|stops|runs|makes|bypasses)\b", lambda m: "and " + _SINGULAR_TO_PLURAL[m.group(1)], t)
    t = re.sub(r"\[([A-Z0-9]{1,3})\]", r"\1", t)   # any bracket the rules did not reach
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t[0].upper() + t[1:] if t else t


# ─────────────────────────────────────────────
# When a change applies
# ─────────────────────────────────────────────

def _tz():
    return ZoneInfo(TIMEZONE)


def weekend_window(saturday, sunday):
    """Epoch bounds of the weekend: Saturday 00:00 to Sunday 23:59:59, local time."""
    tz = _tz()
    start = datetime.datetime(saturday.year, saturday.month, saturday.day, 0, 0, 0, tzinfo=tz)
    end = datetime.datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _time(dt):
    h = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{h} {suffix}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {suffix}"


def _span(start, end):
    tz = _tz()
    a = datetime.datetime.fromtimestamp(start, tz)
    b = datetime.datetime.fromtimestamp(end, tz)
    first = f"{a.strftime('%a')} {_time(a)}"
    if a.date() == b.date():
        return f"{first} – {_time(b)}"
    return f"{first} – {b.strftime('%a')} {_time(b)}"


def _translated(field, lang="en"):
    items = (field or {}).get("translation", []) or []
    for t in items:
        if (t.get("language") or "").lower() == lang:
            return (t.get("text") or "").strip()
    for t in items:
        if (t.get("language") or "").lower().startswith("en") and "html" not in (t.get("language") or ""):
            return (t.get("text") or "").strip()
    return (items[0].get("text") or "").strip() if items else ""


def alert_when(alert, saturday, sunday):
    """
    When a change applies this weekend, from the alert's own active periods
    ("All weekend", or "Sat 5:30 AM – 11:30 PM; Sun 7 AM – 11:30 PM"). The MTA's
    own wording lists every weekend a job runs ("Nov 20 - 23 and Nov 27 - 30"),
    which says nothing about this one, so the times come from the periods
    themselves. A change running for weeks or months gets the MTA's own words
    ("Through Spring 2027").
    """
    win_start, win_end = weekend_window(saturday, sunday)
    mta_words = _translated(((alert.get("transit_realtime.mercury_alert") or {})
                             .get("human_readable_active_period")))
    spans = []
    for p in alert.get("active_period") or []:
        s, e = p.get("start") or 0, p.get("end")
        if s <= win_end and (e is None or e >= win_start):
            spans.append((s, e))
    if not spans:
        return ""
    if any(e is None or (e - s) > 21 * 86400 for s, e in spans):
        return mta_words or "Ongoing"
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1] + 60:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if len(merged) == 1 and merged[0][0] <= win_start + 3600 and merged[0][1] >= win_end - 3600:
        return "All weekend"
    if len(merged) > 4:
        return mta_words or "Several times this weekend"
    return "; ".join(_span(s, e) for s, e in merged)


def alert_detail(alert):
    """The first paragraph of the MTA's description (usually what to do instead), in plain wording."""
    text = _translated(alert.get("description_text"))
    first = text.split("\n\n")[0].strip() if text else ""
    return _truncate(plain_alert_text(first, detail=True), 260) if first else ""


# ─────────────────────────────────────────────
# Feeds
# ─────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _feed(kind):
    """The MTA's GTFS-realtime service-alerts feed as JSON, or None if it cannot be read."""
    for attempt in (1, 2):
        try:
            r = requests.get(FEED_URL.format(kind), headers=FEED_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  {kind} alerts feed attempt {attempt}/2 failed: {e}")
    return None


def parse_alerts(feed, saturday, sunday, accept):
    """Alerts in the feed that `accept(alert, type)` allows and that apply this weekend, de-duplicated."""
    if feed is None:
        return None
    win_start, win_end = weekend_window(saturday, sunday)
    out, seen = [], set()
    for entity in feed.get("entity", []):
        alert = entity.get("alert")
        if not alert:
            continue
        mercury = alert.get("transit_realtime.mercury_alert") or {}
        kind = mercury.get("alert_type", "")
        if not accept(alert, kind):
            continue
        periods = alert.get("active_period") or []
        if periods and not any((p.get("start") or 0) <= win_end and (p.get("end") or 1 << 62) >= win_start
                               for p in periods):
            continue
        header = _translated(alert.get("header_text")) or _translated(alert.get("description_text"))
        if not header:
            continue
        key = header[:160]
        if key in seen:
            continue
        seen.add(key)
        informed = alert.get("informed_entity") or []
        out.append({
            "id": entity.get("id", key),
            "raw": header,
            "plain": plain_alert_text(header),
            "detail": alert_detail(alert),
            "when": alert_when(alert, saturday, sunday),
            "routes": sorted({i.get("route_id") for i in informed if i.get("route_id")}),
            "has_stops": any(i.get("stop_id") for i in informed),
            "alert_type": kind,
            "_alert": alert,
        })
    return out


def subway_changes(saturday, sunday):
    """Every planned subway change that applies this weekend, all lines; None if the feed is down."""
    return parse_alerts(_feed("subway"), saturday, sunday, lambda a, kind: kind.startswith("Planned"))


# ─────────────────────────────────────────────
# Route badges and grouping
# ─────────────────────────────────────────────

LINE_ORDER = ["1", "2", "3", "4", "5", "6", "7", "A", "C", "E", "B", "D", "F", "M", "G", "J", "Z",
              "L", "N", "Q", "R", "W", "H", "FS", "GS", "SI"]
_FAMILY = {
    **dict.fromkeys(["1", "2", "3"], "rt-red"), **dict.fromkeys(["4", "5", "6"], "rt-green"),
    "7": "rt-purple", **dict.fromkeys(["A", "C", "E"], "rt-blue"),
    **dict.fromkeys(["B", "D", "F", "M"], "rt-orange"), "G": "rt-lime",
    **dict.fromkeys(["J", "Z"], "rt-brown"), "L": "rt-grey",
    **dict.fromkeys(["N", "Q", "R", "W"], "rt-yellow"),
    **dict.fromkeys(["H", "FS", "GS"], "rt-grey"), "SI": "rt-blue",
}
_ROUTE_NAMES = {"FS": "Franklin Av Shuttle", "GS": "42 St Shuttle", "H": "Rockaway Park Shuttle", "SI": "Staten Island Railway"}


def badge(route):
    """The MTA's lettered circle for a route, in its trunk-line colour."""
    base = re.sub(r"X$", "", route)
    cls = _FAMILY.get(route) or _FAMILY.get(base) or "rt-grey"
    label = "SIR" if route == "SI" else route
    return f'<span class="rt {cls}" role="img" aria-label="{esc(_ROUTE_NAMES.get(route, route + " train"))}">{esc(label)}</span>'


def _line_rank(route):
    base = re.sub(r"X$", "", route)
    return LINE_ORDER.index(base) if base in LINE_ORDER else len(LINE_ORDER)


def group_by_line(changes, only=None):
    """{route: [change, ...]} in line order; a change on several lines is listed under each."""
    groups = {}
    for c in changes:
        for r in c["routes"]:
            if only is None or r in only:
                groups.setdefault(r, []).append(c)
    return dict(sorted(groups.items(), key=lambda kv: _line_rank(kv[0])))


def group_once(changes):
    """
    {route: [change, ...]} with every change listed once: under the first of your
    lines (A, C, 3) it touches, else under its lowest-numbered line. The other
    lines it touches are shown beside it (see _change_html), so a skip at 86 St
    on the 4, 5 and 6 appears one time, not three.
    """
    groups = {}
    for c in changes:
        routes = c["routes"]
        if not routes:
            continue
        home = [r for r in HOME_LINES if r in routes]
        primary = home[0] if home else min(routes, key=_line_rank)
        groups.setdefault(primary, []).append(c)
    return dict(sorted(groups.items(), key=lambda kv: _line_rank(kv[0])))


def _change_html(c, with_detail=True, primary=None):
    when = f'<div class="mta-when">{esc(c["when"])}</div>' if c.get("when") else ""
    detail = (f'<div class="news-dek">{esc(c["detail"])}</div>'
              if with_detail and c.get("detail") and c["detail"] != c["plain"] else "")
    others = [r for r in c.get("routes", []) if primary and r != primary]
    also = (f'<div class="mta-also">also {"".join(badge(r) for r in others)}</div>' if others else "")
    return f"""
            <div class="news-item">
              <div class="news-hed">{esc(c["plain"])}</div>
              {when}{also}{detail}
            </div>"""


def render_home_lines(changes, lines=HOME_LINES):
    """
    The 'Service changes' block of the MTA This Weekend section: one heading per
    home line, its changes in plain wording with when each applies, or a calm
    line when the line has none. `changes` are the A/C/3 alerts (extra-service
    notices already removed).
    """
    groups = group_by_line(changes, set(lines))
    html = ""
    for line in lines:
        items = groups.get(line, [])
        count = (f'{len(items)} change{"s" if len(items) != 1 else ""}' if items
                 else "No planned changes this weekend")
        body = "".join(_change_html(c, with_detail=False) for c in items)
        html += f"""
          <div class="mta-group">
            <div class="mta-line">{badge(line)}<span class="mta-line-n">{esc(count)}</span></div>
            <div class="news-feed">{body}</div>
          </div>"""
    return html


# ─────────────────────────────────────────────
# Excursions
# ─────────────────────────────────────────────

def load_library(path=LIBRARY_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("trips", [])
    except Exception as e:
        print(f"  Excursion library unreadable: {e}")
        return []


def pick_trips(library, saturday, rain_pct=None):
    """
    Three trips, one per railroad in a fixed order, each rotating through its
    in-season trips week by week. Outdoor-heavy trips are set aside when rain is
    likely, unless that would leave the railroad with nothing.
    """
    week = saturday.isocalendar()[1] + saturday.year * 53
    picks = []
    for i, railroad in enumerate(RAILROAD_ORDER):
        pool = [t for t in library
                if t.get("railroad") == railroad and saturday.month in t.get("months", range(1, 13))]
        if rain_pct is not None and rain_pct >= RAIN_SKIP_FAIR_WEATHER:
            pool = [t for t in pool if not t.get("fair_weather")] or pool
        if pool:
            picks.append(pool[(week + 3 * i) % len(pool)])
    return picks


_RAIL_FEED = {"Metro-North": "mnr", "LIRR": "lirr"}
_RAIL_SKIP = {"Special Notice", "Boarding Change", "Some Delays", "Delays", "Expect Delays"}


def rail_feeds(saturday, sunday):
    """Weekend alerts for each railroad that publishes a feed; None where a feed could not be read."""
    def accept(alert, kind):
        return kind not in _RAIL_SKIP

    out = {}
    for railroad, kind in _RAIL_FEED.items():
        alerts = parse_alerts(_feed(kind), saturday, sunday, accept)
        if alerts is not None:
            win_start, _ = weekend_window(saturday, sunday)
            # A notice that ended in the small hours of Saturday is about Friday, not the weekend.
            alerts = [a for a in alerts if _ends_after(a["_alert"], win_start + 6 * 3600)]
        out[railroad] = alerts
    return out


def _ends_after(alert, ts):
    periods = alert.get("active_period") or []
    return not periods or any(p.get("end") is None or p["end"] > ts for p in periods)


def trip_notices(trip, feeds):
    """
    What the railroad's own alerts say about this trip's line this weekend.
    Returns (state, notices): state is "notices" or "clear" for Metro-North and
    the LIRR, "none" when the feed could not be read, and "njt" for NJ Transit.
    Alerts that name specific stops are only shown when they name this station.
    """
    railroad = trip.get("railroad")
    if railroad not in _RAIL_FEED:
        return "njt", []
    alerts = feeds.get(railroad)
    if alerts is None:
        return "none", []
    ids = set(trip.get("route_ids") or [])
    stop = (trip.get("stop") or "").lower()
    hits = []
    for a in alerts:
        if not (ids & set(a["routes"])):
            continue
        if a["has_stops"] and stop not in a["raw"].lower() and stop not in a["detail"].lower():
            continue
        hits.append(a)
    return ("notices" if hits else "clear"), hits[:3]


# ─────────────────────────────────────────────
# HTML: the Weekender section
# ─────────────────────────────────────────────

def _trip_html(n, trip, feeds):
    state, notices = trip_notices(trip, feeds)
    line = trip.get("line", "")
    if state == "notices":
        live = "<br>".join(
            f'{esc(a["plain"])}' + (f' <span class="trip-when">({esc(a["when"])})</span>' if a.get("when") else "")
            for a in notices)
    elif state == "clear":
        live = f"No planned changes on the {esc(line)} are listed for this weekend."
    elif state == "njt":
        live = (f'NJ Transit publishes no alerts feed we can read: check '
                f'<a href="{esc(NJT_URL)}">njtransit.com</a> for weekend service changes.')
    else:
        live = ""
    rows = [("Ride", esc(trip.get("ride", ""))), ("On foot", esc(trip.get("on_foot", "")))]
    if live:
        rows.append(("This weekend", live))
    if trip.get("check"):
        rows.append(("Check", esc(trip["check"])))
    rows_html = "".join(
        f'<p class="trip-row"><span class="trip-k">{k}</span>{v}</p>' for k, v in rows if v)
    links = " &middot; ".join(f'<a href="{esc(u)}">{esc(t)}</a>' for t, u in trip.get("links", []))
    links_html = f'<p class="trip-row trip-links">{links}</p>' if links else ""
    tag = " &middot; ".join(esc(b) for b in (f"Trip {n}", trip.get("railroad", ""), line) if b)
    big = ' <span class="trip-big">Long day</span>' if trip.get("big_day") else ""
    photo_html = ""
    try:
        photo = dm.trip_photo(trip)       # a photo of the place: open-licence Commons first, then the place's own picture
    except Exception as e:
        photo = None
        print(f"  [trip photo] {trip.get('id')} skipped: {type(e).__name__}: {e}")
    if photo:
        # no referrer, and if it fails to load in the browser the figure removes itself and the trip reads as text
        photo_html = (f'<figure class="trip-photo"><img src="{esc(photo["url"])}" alt="{esc(trip.get("station", ""))}" '
                      f'loading="lazy" decoding="async" referrerpolicy="no-referrer" '
                      f'onerror="this.closest(\'figure\').remove()">'
                      f'<figcaption>{esc(photo["credit"])} &middot; <a href="{esc(photo["page"])}">source</a></figcaption></figure>')
    return f"""
          <div class="news-item trip">
            {photo_html}
            <div class="news-source">{tag}{big}</div>
            <div class="trip-name">{esc(trip.get("station", ""))}</div>
            <div class="news-hed">{esc(trip.get("headline", ""))}</div>
            <p class="trip-pitch">{esc(trip.get("pitch", ""))}</p>
            {rows_html}{links_html}
          </div>"""


def _nogo(changes):
    return [c for c in changes if c["alert_type"] in ("Planned - Suspended", "Planned - Part Suspended")]


def build_weekender_html(changes, trips, feeds, subway_url=""):
    """
    The MTA Weekender section body: where subway trains are not running this
    weekend (from the planned changes), a link to the full list by line, then
    the three trips. Either part is left out when its data is missing.
    """
    html = ""
    if changes is not None:
        lines = sorted({r for c in changes for r in c["routes"]}, key=_line_rank)
        nogo = sorted(_nogo(changes), key=lambda c: (
            0 if set(c["routes"]) & set(HOME_LINES) else 1, _line_rank(c["routes"][0]) if c["routes"] else 99))
        if changes:
            lead = (f'{len(changes)} planned subway change{"s" if len(changes) != 1 else ""} '
                    f'on {len(lines)} line{"s" if len(lines) != 1 else ""} this weekend.')
        else:
            lead = "No planned subway changes are listed for this weekend."
        html += f'<p class="wk-lead">{esc(lead)}</p>'
        if nogo:
            rows = ""
            for c in nogo[:6]:
                rows += f"""
            <div class="wk-nogo-row">
              <span class="wk-badges">{"".join(badge(r) for r in c["routes"][:3])}</span>
              <span class="wk-nogo-text">{esc(c["plain"])}<span class="mta-when">{esc(c["when"])}</span></span>
            </div>"""
            html += f'<div class="wk-nogo"><div class="mta-group-label">Where trains are not running</div>{rows}</div>'
        if subway_url and changes:
            html += f'<p class="wk-link"><a href="{esc(subway_url)}">Every change, line by line &rarr;</a></p>'
    if trips:
        html += '<div class="mta-group-label wk-trips-label">Three trips, no car</div>'
        html += '<div class="news-feed">' + "".join(
            _trip_html(i, t, feeds) for i, t in enumerate(trips, 1)) + "</div>"
    return html


# ─────────────────────────────────────────────
# CSS shared by the main page and the System Changes page
# ─────────────────────────────────────────────

RT_CSS = """  .rt { display:inline-flex; align-items:center; justify-content:center; box-sizing:border-box;
        min-width:20px; height:20px; padding:0 5px; border-radius:10px; flex:none;
        font:700 11px/1 Helvetica, Arial, sans-serif; letter-spacing:0; color:#fff; vertical-align:middle; }
  .rt-red { background:#EE352E; } .rt-green { background:#00933C; } .rt-purple { background:#B933AD; }
  .rt-blue { background:#0039A6; } .rt-orange { background:#FF6319; } .rt-lime { background:#6CBE45; color:#1A1208; }
  .rt-brown { background:#996633; } .rt-grey { background:#808183; } .rt-yellow { background:#FCCC0A; color:#1A1208; }
"""

MTA_CSS = RT_CSS + """
  .mta-line { display:flex; align-items:center; gap:10px; margin:0 0 10px; }
  .mta-also { font-family:'Courier Prime','Courier New',monospace; font-size:9px; letter-spacing:.14em;
              text-transform:uppercase; color:#8A6A3A; margin:4px 0 0; display:flex; align-items:center; gap:4px; }
  .mta-line-n { font-family:'Courier Prime','Courier New',monospace; font-size:9px; letter-spacing:.18em;
                text-transform:uppercase; color:#8A6A3A; }
  .mta-when { font-family:'Courier Prime','Courier New',monospace; font-size:10px; letter-spacing:.06em;
              color:#8A6A3A; margin:2px 0 0; display:block; }
  .wk-lead { margin:0 0 12px; }
  .wk-nogo { margin:0 0 14px; }
  .wk-nogo-row { display:flex; gap:10px; align-items:flex-start; padding:7px 0; border-top:1px solid #D4B98A; }
  .wk-badges { display:inline-flex; gap:3px; min-width:24px; padding-top:2px; }
  .wk-nogo-text { font-family:'Cormorant Garamond',Georgia,serif; font-size:15px; line-height:1.35; color:#1A1208; }
  .wk-link { margin:0 0 18px; }
  .wk-trips-label { margin-top:20px; }
  .trip-name { font-family:'Cormorant Garamond',Georgia,serif; font-style:italic; font-weight:300;
               font-size:28px; line-height:1.05; color:#1A1208; margin:0 0 4px; }
  .trip-big { font-family:'Courier Prime','Courier New',monospace; font-size:7.5px; letter-spacing:.14em;
              text-transform:uppercase; background:#9B3A1A; color:#F5EDD8; padding:2px 5px; margin-left:6px; }
  .trip-photo { margin:0 0 12px; }
  .trip-photo img { display:block; width:100%; height:auto; aspect-ratio:16/10; object-fit:cover; border:1px solid #D4B98A; background:#EFE3C3; }
  .trip-photo figcaption { font-family:'Courier Prime','Courier New',monospace; font-size:7.5px; letter-spacing:.1em;
                           text-transform:uppercase; color:#B89A5A; margin-top:6px; line-height:1.5; }
  .trip-photo figcaption a { color:inherit; }
  .b4-section p.trip-pitch { font-size:15.5px; line-height:1.55; margin:0 0 10px; }
  .b4-section p.trip-row { font-size:14px; line-height:1.5; color:#4A3820; margin:0 0 5px; }
  .trip-k { font-family:'Courier Prime','Courier New',monospace; font-size:8px; letter-spacing:.2em;
            text-transform:uppercase; color:#9B3A1A; margin-right:9px; }
  .trip-when { color:#8A6A3A; }
  .b4-section p.trip-links { font-size:13.5px; margin-top:8px; }
"""


# ─────────────────────────────────────────────
# HTML: the Subway Changes page, in the News & Culture design
# ─────────────────────────────────────────────

_TRUNKS = [
    ["1", "2", "3"], ["4", "5", "6"], ["7"], ["A", "C", "E"], ["B", "D", "F", "M"], ["G"],
    ["J", "Z"], ["L"], ["N", "Q", "R", "W"],
]
_ROMANS = ["i.", "ii.", "iii.", "iv.", "v.", "vi.", "vii.", "viii.", "ix.", "x.", "xi.", "xii."]

SUBWAY_CSS = RT_CSS + r"""
  .b-page .rt + .rt, .b-page .rt + .rt-sep + .rt { margin-left: 3px; }
  .b-page { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 3px; }
  .b-when { margin: 3px 0 0; font-family: var(--mono); font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase;
            color: var(--accent); line-height: 1.5; }
"""


def subway_panels(changes):
    """
    The Subway Changes tabs: your lines (A, C, 3) first, then the rest of the
    system by trunk line, only the ones with a change. Each change is in exactly
    one tab. Returns [(slug, tab label, banner title, [change, ...])].
    """
    once = group_once(changes)
    panels = [("your-lines", "Your lines", "Your lines",
               [c for r in HOME_LINES for c in once.get(r, [])])]
    used = set(HOME_LINES)
    for members in _TRUNKS:
        present = [r for r in members if r in once and r not in HOME_LINES]
        if not present:
            continue
        shown = [r for r in members if r not in HOME_LINES]
        label = " · ".join(shown)
        panels.append(("line-" + "-".join(r.lower() for r in shown), label, f"Trains {label}",
                       [c for r in present for c in once[r]]))
        used.update(present)
    others = [r for r in once if r not in used]
    if others:
        panels.append(("line-shuttles", "Shuttles", "Shuttles and the Staten Island Railway",
                       [c for r in others for c in once[r]]))
    return panels


def _subway_story(c):
    badges = "".join(badge(r) for r in c["routes"])
    when = c.get("when", "")
    short = len(when) <= 26
    byline = f'<span class="b-byline">{esc(when)}</span>' if when and short else ""
    when_line = f'<p class="b-when">{esc(when)}</p>' if when and not short else ""
    detail = (f'<p class="b-blurb">{esc(c["detail"])}</p>'
              if c.get("detail") and c["detail"] != c["plain"] else "")
    return (f'<li class="b-story"><div class="b-story-meta"><span class="b-page">{badges}</span>{byline}</div>'
            f'<h3 class="b-title">{esc(c["plain"])}</h3>{when_line}{detail}</li>')


def build_subway_page(changes, saturday, sunday, generated="", version=""):
    """
    The Subway Changes companion page, in the same design as News & Culture:
    section tabs that switch in place (CSS only, each with its own address), a
    pinned tab bar, previous/next links and a Night/Day toggle. Every planned
    change this weekend appears once, in plain wording with when it applies.
    """
    panels = subway_panels(changes)
    home = dp.HOME_FILE + (f"?v={version}" if version else "")
    count = len({c["id"] for c in changes})
    lines = len({r for c in changes for r in c["routes"]})
    body = ""
    for n, (slug, label, title, items) in enumerate(panels):
        listing = (f'<ol class="broadsheet-cols">{"".join(_subway_story(c) for c in items)}</ol>' if items
                   else '<p class="empty-note">No planned changes on the A, C or 3 this weekend.</p>')
        prev_p = panels[n - 1] if n > 0 else None
        next_p = panels[n + 1] if n < len(panels) - 1 else None
        step = ((f'<a class="prev" href="#{prev_p[0]}">&larr; {esc(prev_p[1])}</a>' if prev_p else "")
                + (f'<a class="next" href="#{next_p[0]}">{esc(next_p[1])} &rarr;</a>' if next_p else ""))
        body += f"""
      <section class="panel" id="p-{slug}" aria-labelledby="h-{slug}">
        <h2 class="section-banner" id="h-{slug}"><span>{esc(title)}</span><span class="rom">{_ROMANS[min(n, len(_ROMANS) - 1)]}</span></h2>
        {listing}
        <div class="step">{step}</div>
      </section>"""

    anchors = "".join(f'<span class="anchor" id="{p[0]}"></span>' for p in panels)
    jump = "".join(f'<a href="#{p[0]}">{esc(p[1])}</a>' for p in panels)
    active = "background: var(--ink); border-color: var(--ink); color: var(--card);"
    tab_state = (
        ", ".join(f'#{p[0]}:target ~ .jump a[href="#{p[0]}"]' for p in panels)
        + f", .sheet:not(:has(.anchor:target)) .jump a:first-child {{ {active} }}\n  "
        + ", ".join(f'#{p[0]}:target ~ .panels #p-{p[0]}' for p in panels) + " { display: block; }")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Subway Changes &middot; Weekend Dispatch</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>{dp.NEWSPRINT_CSS}{dp.NC_CSS.replace('@@TAB_STATE@@', tab_state)}{SUBWAY_CSS}</style>
</head>
<body>
<input type="checkbox" id="theme-switch" class="visually-hidden" aria-label="Toggle dark mode">
<div class="wrap">
  <main class="sheet">
    <div class="top-bar">
      <p class="kicker"><a href="{home}">Weekend Dispatch</a> &middot; Subway Changes</p>
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
        Subway Changes
        <span class="issue-tag">the weekend of {esc(saturday.strftime("%B %-d"))}</span>
      </h1>
      <div class="dateline">
        <span>Saturday {esc(saturday.strftime("%b %-d"))} &middot; Sunday {esc(sunday.strftime("%-d"))}</span>
        <span>{count} changes &middot; {lines} lines</span>
        <span>Source: MTA service alerts</span>
      </div>
      <div class="dateline-sub" aria-hidden="true"></div>
      <div class="edition-bar edition-bar--solo">
        <div class="links">
          <a class="cta cta--solid" href="{home}">Back to the dispatch</a>
        </div>
      </div>
    </header>

    {anchors}
    <nav class="jump" aria-label="Lines">{jump}</nav>
    <div class="panels">{body}
    </div>

    <p class="nc-note">Wording and times are the MTA&rsquo;s own for this weekend, with the route brackets taken off.</p>
  </main>

  <footer class="foot">
    <span>Fort Greene &middot; Brooklyn</span>
    <span><a href="{home}">The dispatch</a>{(" &middot; generated " + esc(generated)) if generated else ""}</span>
  </footer>
</div>
</body>
</html>"""
