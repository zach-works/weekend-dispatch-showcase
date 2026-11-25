"""
Culture listings for the weekend dispatch: what is on at Brooklyn's screens, stages and walls.

Each source is read from the markup or feed it already publishes. A source that is down or whose
markup changes is left out, and the rest of the section still runs (fail closed).

Sources:
  - BAM's film schedule (bam.org/film) and music schedule (bam.org/music). Each listing carries
    data-sort-title, data-sort-date and data-sort-genre attributes.
  - Schema.org Event data, read with the same reader the News & Culture page uses
    (dispatch_pages.fetch_jsonld_events): Prospect Park and the NYC Transit Museum.
  - An iCal feed (the standard calendar format): Museum of the Moving Image.

Dropped: Green-Wood Cemetery (removed at your request), the Brooklyn Museum and the Brooklyn Public
Library (both block automated requests). Not yet built: a repertory house, classical concert
calendars and gallery openings, which do not publish readable listings yet.
"""
import datetime
import html
import re
import zoneinfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import dispatch_pages as dp

UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter)"}
LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

# ── BAM (film and music share one page layout) ──────────────────────────────
BAM_FILM_URL = "https://www.bam.org/film"
BAM_MUSIC_URL = "https://www.bam.org/music"
CINEMA_LOOKBACK_DAYS = 21   # a film that opened up to three weeks before the weekend is probably still running
BAM_MAX = 5                 # keep each BAM subsection short

# ── Schema.org Event pages (read by dispatch_pages.fetch_jsonld_events) ─────
EVENT_PAGES = [
    ("Prospect Park", "https://www.prospectpark.org/events/", "Prospect Park"),
    ("NYC Transit Museum", "https://www.nytransitmuseum.org/programs/", "NYC Transit Museum"),
]

# ── iCal feeds: (source label, feed URL, place used when an event has no location) ──
ICAL_FEEDS = [
    ("Museum of the Moving Image", "https://www.movingimage.org/events/?ical=1", "Museum of the Moving Image, Astoria"),
]

EVENTS_MAX = 6
EVENTS_PER_SOURCE = 2
# Sources whose pictures do not display in the page (their server refuses them when they are linked from
# elsewhere). Their events still appear, as text only.
NO_IMAGE_SOURCES = {"Museum of the Moving Image"}


# ─────────────────────────────────────────────
# BAM
# ─────────────────────────────────────────────

def fetch_bam_listings(url, genre, saturday, sunday):
    """BAM listings of one genre ('film' or 'music') dated from the lookback window up to Sunday:
    [{title, date, blurb, url}], newest first. Returns [] when the page cannot be read."""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [culture] BAM {genre} unavailable: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    earliest = saturday - datetime.timedelta(days=CINEMA_LOOKBACK_DAYS)
    seen, items = set(), []
    for block in soup.select("[data-sort-title][data-sort-date]"):
        title = " ".join(block["data-sort-title"].split())
        if (block.get("data-sort-genre", "") or "").lower() not in ("", genre):
            continue                      # the same page lists talks, poetry and other genres too
        if not title or title in seen:
            continue                      # the page repeats each listing for desktop and mobile
        try:
            when = datetime.date.fromisoformat(block["data-sort-date"][:10])
        except ValueError:
            continue
        if not (earliest <= when <= sunday):
            continue
        seen.add(title)
        desc = block.select_one(".description")
        pic = block.select_one("img")
        src = pic.get("src", "") if pic else ""
        items.append({
            "title": title,
            "date": when,
            "blurb": " ".join(desc.get_text(" ").split()) if desc else "",
            "url": url,
            "image": usable_image(urljoin(url, src)) if src else "",
        })

    items.sort(key=lambda f: f["date"], reverse=True)
    return items[:BAM_MAX]


def fetch_bam_films(saturday, sunday):
    return fetch_bam_listings(BAM_FILM_URL, "film", saturday, sunday)


def fetch_bam_music(saturday, sunday):
    return fetch_bam_listings(BAM_MUSIC_URL, "music", saturday, sunday)


def build_bam_html(items, saturday, heading, page_url, link_text):
    """A BAM subsection, styled like the other Around Brooklyn items. Empty when there is nothing."""
    if not items:
        return ""
    rows = ""
    for f in items:
        when = f["date"]
        label = (f"Opens {when.strftime('%a %b %-d')}" if when >= saturday
                 else f"Opened {when.strftime('%b %-d')}")
        dek = f"<div class='news-dek'>{html.escape(f['blurb'])}</div>" if f["blurb"] else ""
        rows += f"""
          <div class="news-item">
            <div class="news-source">{html.escape(label)}</div>
            {image_html(f.get('image', ''), f['title'])}
            <div class="news-headline-row">
              <div class="news-hed"><a href="{html.escape(f['url'])}">{html.escape(f['title'])}</a></div>
              {dek}
            </div>
          </div>"""
    return f"""
          <div class="news-subhead">{html.escape(heading)}</div>{rows}
          <p style="margin-top:10px;"><a href="{html.escape(page_url)}">{html.escape(link_text)} &rarr;</a></p>"""


def build_culture_html(films, saturday):
    """Kept for the film subsection's existing call site."""
    return build_bam_html(films, saturday, "At the cinema: BAM, Fort Greene", BAM_FILM_URL,
                          "The full BAM film schedule")


# ─────────────────────────────────────────────
# Schema.org Event pages and iCal feeds (weekend events)
# ─────────────────────────────────────────────

def _ics_unescape(s):
    return s.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_ics(text):
    """Each VEVENT as {NAME: (params, value)} with line folding undone."""
    text = re.sub(r"\r?\n[ \t]", "", text)
    events, current = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            current[name] = (dict(p.split("=", 1) for p in params if "=" in p), value)
    return events


def _ics_start(params, value):
    """A timezone-aware local start for DTSTART, or None when it cannot be read."""
    try:
        if params.get("VALUE") == "DATE" or len(value) == 8:
            return datetime.datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=LOCAL_TZ)
        if value.endswith("Z"):
            utc = datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
            return utc.astimezone(LOCAL_TZ)
        tz = zoneinfo.ZoneInfo(params["TZID"]) if "TZID" in params else LOCAL_TZ
        return datetime.datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz).astimezone(LOCAL_TZ)
    except (KeyError, ValueError, zoneinfo.ZoneInfoNotFoundError):
        return None


def fetch_ical_events(source, url, default_place, saturday, sunday):
    """Events from one iCal feed that start on Saturday or Sunday: [{source, name, start, url, place}]."""
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    if "BEGIN:VCALENDAR" not in r.text:
        raise ValueError("not an iCal feed")
    out = []
    for ev in _parse_ics(r.text):
        if "DTSTART" not in ev or "SUMMARY" not in ev:
            continue
        start = _ics_start(*ev["DTSTART"])
        if start is None or not (saturday <= start.date() <= sunday):
            continue
        link = ev.get("URL", ({}, ""))[1].strip()
        loc = ev.get("LOCATION", ({}, ""))[1]
        out.append({
            "source": source,
            "name": _ics_unescape(ev["SUMMARY"][1]).strip(),
            "start": start,
            "url": link or url,
            "place": _ics_unescape(loc).strip() or default_place,
        })
    return out


def usable_image(url):
    """The URL when it loads as an image (following redirects), otherwise ''. Broken pictures are left out."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=15, stream=True, allow_redirects=True)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image/")
        r.close()
        return url if ok else ""
    except Exception:
        return ""


def image_html(src, alt):
    """A responsive picture for an item, or '' when there is no image URL. Alt text is the item's title."""
    if not src:
        return ""
    return (f'<img src="{html.escape(src)}" alt="{html.escape(alt)}" width="100%" '
            f'style="display:block;width:100%;height:auto;margin:6px 0 8px;">')


def page_image(url):
    """The page's main picture (og:image), absolute, or '' when it cannot be read."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        meta = BeautifulSoup(r.text, "html.parser").find("meta", property="og:image")
        return urljoin(url, meta["content"]) if meta and meta.get("content") else ""
    except Exception:
        return ""


def _cap(events):
    """Time order, at most EVENTS_PER_SOURCE per source and EVENTS_MAX in all."""
    shown, per_source = [], {}
    for e in events:
        if per_source.get(e["source"], 0) >= EVENTS_PER_SOURCE or len(shown) >= EVENTS_MAX:
            continue
        per_source[e["source"]] = per_source.get(e["source"], 0) + 1
        shown.append(e)
    return shown


def fetch_weekend_events(saturday, sunday):
    """Events from the schema.org pages and iCal feeds dated Saturday or Sunday, in time order, capped
    for display: [{source, name, start, url, place, image}]. Each source is read on its own; a failure
    drops only that source. Images are looked up only for the events that will be shown."""
    events = []
    try:
        events += [e for e in dp.fetch_jsonld_events(saturday, days=(sunday - saturday).days, pages=EVENT_PAGES)
                   if e["start"].date() <= sunday]
    except Exception as e:
        print(f"  [culture] event pages unavailable: {e}")
    for source, url, default_place in ICAL_FEEDS:
        try:
            events += fetch_ical_events(source, url, default_place, saturday, sunday)
        except Exception as e:
            print(f"  [culture] {source} feed unavailable: {e}")
    events.sort(key=lambda e: e["start"])
    shown = _cap(events)
    for e in shown:
        if e["source"] in NO_IMAGE_SOURCES:
            e["image"] = ""              # the source's pictures don't display when hotlinked, so show the text only
            continue
        candidate = e.get("image") or page_image(e.get("url", ""))
        e["image"] = usable_image(candidate)
    return shown


def _when(dt):
    hour = dt.hour % 12 or 12
    return f"{dt.strftime('%a %b %-d')}, {hour}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def build_events_html(events):
    """The 'This weekend in parks, museums and cemeteries' subsection. Empty when there is nothing."""
    if not events:
        return ""
    items = ""
    for e in _cap(events):                # already capped by fetch_weekend_events; capping again is harmless
        place = f"<div class='news-dek'>{html.escape(e['place'])}</div>" if e.get("place") else ""
        items += f"""
          <div class="news-item">
            <div class="news-source">{html.escape(e['source'])} &middot; {html.escape(_when(e['start']))}</div>
            {image_html(e.get('image', ''), e['name'])}
            <div class="news-headline-row">
              <div class="news-hed"><a href="{html.escape(e['url'])}">{html.escape(e['name'])}</a></div>
              {place}
            </div>
          </div>"""
    return f"""
          <div class="news-subhead">This weekend in parks and museums</div>{items}"""
