"""
Weekend Dispatch: the News & Culture page.

The main dispatch page stays as it is. This module fetches the extra sources
(feeds across nine areas, iNaturalist sightings, park and museum events, city
permits) and renders a separate News & Culture page in nine sections, plus two
small pieces the main page uses to point at it: a five-headline strip and the
iNaturalist Nature Report block.

Facts are quoted from their source (headline, source, date, venue, link), never
paraphrased by a model. Every source is fail-closed: if a feed or API is down
its items are simply missing, and a section with nothing to show says so.
"""

import concurrent.futures as cf
import datetime
import html as _html
import json
import re
import urllib.parse as _up

import feedparser
import requests
from bs4 import BeautifulSoup

import dispatch_media as dm

# A plain, honest client name. Some publishers refuse unfamiliar agents, so a
# feed that returns nothing is retried once with feedparser's own default.
UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter)"}

# ─────────────────────────────────────────────
# THE NINE PAGES
# ─────────────────────────────────────────────

# Nine sections, in print order. `feeds` are the NEW sources for the section;
# `pointer` says what the same topic already has on the main page, so nothing
# is repeated. The Fort Greene section also carries the Fort Greene
# Community News sub-page, neighborhood mentions and street permits; Music &
# Events carries park and museum events; Books also draws on The New Yorker's
# book and poetry items.
SECTIONS = [
    {"slug": "crown-heights", "title": "Fort Greene", "short": "Fort Greene", "feeds": [], "pointer": ""},
    {"slug": "arts", "title": "Arts", "short": "Arts", "feeds": ["colossal", "bsa"], "pointer": ""},
    {"slug": "culture", "title": "Culture", "short": "Culture",
     "feeds": ["newyorker", "aeon", "quanta", "nautilus", "pdr", "ephemeral"],
     "pointer": "Then &amp; Now, the weekly side-by-side of the city, is on the main page."},
    {"slug": "music-events", "title": "Music &amp; Events", "short": "Music & Events",
     "feeds": ["pitchfork", "stereogum", "bandcamp"],
     "pointer": "Tonight's rooms (Public Records and the DICE picks) are on the main page, with the Long List."},
    {"slug": "parks-transit-nature", "title": "Parks, Transit, and Nature", "short": "Parks, Transit & Nature",
     "feeds": ["mongabay", "grist", "e360", "gcaptain"],
     "pointer": "This weekend's train changes, the Nature Report, and Bird, Tree and Map of the Week are on the main page."},
    {"slug": "food-travel", "title": "Food &amp; Travel", "short": "Food & Travel", "feeds": ["eater", "atlas"],
     "pointer": "The Greenmarket verdict and Edible Brooklyn's picks are on the main page."},
    {"slug": "books", "title": "Books", "short": "Books", "feeds": ["lithub", "electric", "bookriot"], "pointer": ""},
    {"slug": "brooklyn", "title": "Brooklyn", "short": "Brooklyn", "feeds": ["brownstoner", "brooklynpaper"],
     "pointer": "The Around Brooklyn picks are on the main page."},
    {"slug": "new-york-world", "title": "New York and World News", "short": "NY & World",
     "feeds": ["nyt_ny", "bbc", "guardian", "nyt_world"], "pointer": ""},
]
ROMAN = ["i.", "ii.", "iii.", "iv.", "v.", "vi.", "vii.", "viii.", "ix."]
ITEMS_PER_SECTION = 8
NEWS_FILE = "news-culture.html"
CHCO_FILE = "chco.html"
HOME_FILE = "weekend.html"


# ─────────────────────────────────────────────
# FEEDS
# ─────────────────────────────────────────────
# id: (publication, feed url, how many days back an item may be)
FEEDS = {
    "hyperallergic":  ("Hyperallergic", "https://hyperallergic.com/feed/", 21),
    "colossal":       ("Colossal", "https://www.thisiscolossal.com/feed/", 30),
    "bsa":            ("Brooklyn Street Art", "https://www.brooklynstreetart.com/feed/", 30),
    "newyorker":      ("The New Yorker", "https://www.newyorker.com/feed/culture", 14),
    "drift":          ("The Drift", "https://thedriftmag.com/feed", 45),
    "aeon":           ("Aeon", "https://aeon.co/feed.rss", 21),
    "quanta":         ("Quanta Magazine", "https://api.quantamagazine.org/feed/", 21),
    "nautilus":       ("Nautilus", "https://nautil.us/feed/", 21),
    "pdr":            ("Public Domain Review", "https://publicdomainreview.org/rss.xml", 60),
    "ephemeral":      ("Ephemeral New York", "https://ephemeralnewyork.wordpress.com/feed/", 30),
    "pitchfork":      ("Pitchfork", "https://pitchfork.com/feed/feed-news/rss", 10),
    "stereogum":      ("Stereogum", "https://www.stereogum.com/feed/", 10),
    "bandcamp":       ("Bandcamp Daily", "https://daily.bandcamp.com/feed", 14),
    "streetsblog":    ("Streetsblog NYC", "https://nyc.streetsblog.org/feed/", 14),
    "mongabay":       ("Mongabay", "https://news.mongabay.com/feed/", 14),
    "grist":          ("Grist", "https://grist.org/feed/", 14),
    "e360":           ("Yale Environment 360", "https://e360.yale.edu/feed.xml", 21),
    "gcaptain":       ("gCaptain", "https://gcaptain.com/feed/", 14),
    "eater":          ("Eater NY", "https://ny.eater.com/rss/index.xml", 14),
    "atlas":          ("Atlas Obscura", "https://www.atlasobscura.com/feeds/latest", 21),
    "lithub":         ("Literary Hub", "https://lithub.com/feed/", 14),
    "electric":       ("Electric Literature", "https://electricliterature.com/feed/", 21),
    "bookriot":       ("Book Riot", "https://bookriot.com/feed/", 14),
    "brooklynmag":    ("Brooklyn Magazine", "https://brooklynmagazine.org/feed/", 21),
    "brooklynpaper":  ("Brooklyn Paper", "https://www.brooklynpaper.com/feed/", 14),
    "brownstoner":    ("Brownstoner", "https://www.brownstoner.com/feed/", 14),
    "bushwick":       ("Bushwick Daily", "https://bushwickdaily.com/feed/", 21),
    "gothamist":      ("Gothamist", "https://gothamist.com/feed", 7),
    "thecity":        ("THE CITY", "https://thecity.nyc/feed", 10),
    "nyt_ny":         ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/NYRegion.xml", 5),
    "bbc":            ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml", 4),
    "guardian":       ("The Guardian", "https://www.theguardian.com/world/rss", 4),
    "nyt_world":      ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", 4),
    "npr":            ("NPR", "https://feeds.npr.org/1001/rss.xml", 4),
}

# Paid/sponsored posts mixed into some feeds
_SPONSORED_LINKS = ("/resources-guide/",)
_SPONSORED_TAGS = {"sponsored", "resources guide"}

# Words that put a story in the Fort Greene page's "In the neighborhood" block
CH_KEYWORDS = ("fort greene", "franklin avenue", "franklin ave", "eastern parkway", "nostrand avenue",
               "utica avenue", "kingston avenue", "brower park", "weeksville", "lincoln terrace",
               "grand army plaza", "bergen street", "atlantic yards")


def esc(s):
    return _html.escape(str(s if s is not None else ""))


def _text(raw, limit=0):
    t = BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*The (?:post|article) .{0,200}? appeared first on .*$", "", t).strip()
    t = re.sub(r"\s*Do stories and artists like this matter to you\?.*$", "", t).strip()   # Colossal's membership appeal
    t = re.sub(r"\s*Read (?:more|the full story|full story) (?:on|at|from) .*$", "", t).strip()   # trailing "Read more on Grist"
    if limit and len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0].rstrip(",;:- ")
        t = cut + "…"
    return t


# Pictures: most feeds carry one per item, in one of four places. Images are the
# publisher's own, linked from their site (as a feed reader shows them), credited
# to the source and linking back to the story; nothing is copied or re-hosted.
_IMG_EXT = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:[?#]|$)", re.I)
# tracking pixels, icons, avatars, ads and the like inside article HTML
_IMG_JUNK = re.compile(r"(?:pixel|beacon|tracker|doubleclick|feedburner|gravatar|emoji|spacer|/ads?/|sprite|logo|avatar|\.gif(?:[?#]|$)|\.svg(?:[?#]|$))", re.I)
NO_IMAGE_FEEDS = set()   # feed ids whose pictures should not be shown, if a publisher ever asks


def _img_url(u):
    u = (u or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[7:]          # the pages are https; mixed content would be blocked
    return u if u.startswith("https://") else ""


def _entry_image(e):
    """Best picture URL for a feed entry, or "" if there is none worth showing."""
    cands = []   # (width_or_0, url)

    def add(url, width=0):
        url = _img_url(url)
        if url and not _IMG_JUNK.search(url):
            try:
                cands.append((int(float(width or 0)), url))
            except (TypeError, ValueError):
                cands.append((0, url))
    for m in e.get("media_thumbnail") or []:
        add(m.get("url"), m.get("width"))
    for m in e.get("media_content") or []:
        if m.get("medium") == "image" or str(m.get("type", "")).startswith("image") or _IMG_EXT.search(m.get("url", "")):
            add(m.get("url"), m.get("width"))
    for l in e.get("enclosures") or []:
        if str(l.get("type", "")).startswith("image"):
            add(l.get("href") or l.get("url"))
    if not cands:
        html = (e.get("summary", "") or "") + " ".join(c.get("value", "") for c in e.get("content") or [])
        for tag in BeautifulSoup(html, "html.parser").find_all("img"):
            w, h = str(tag.get("width", "")).strip(), str(tag.get("height", "")).strip()
            if (w.isdigit() and int(w) < 120) or (h.isdigit() and int(h) < 120):
                continue                 # an icon or tracking pixel
            add(tag.get("src") or tag.get("data-src"), w if w.isdigit() else 0)
            if cands:
                break
    if not cands:
        return ""
    # the widest one that states a width (up to 1400px: more is wasted on a column this size), else the first
    sized = [c for c in cands if 0 < c[0] <= 1400]
    return max(sized)[1] if sized else cands[0][1]


IMG_BYTE_BUDGET = 700_000   # a thumbnail in a column this size should not cost more than this
_BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
               "Accept": "image/avif,image/webp,image/*,*/*"}


def _probe_image(url):
    """(loads, bytes) for an image URL, asked the way a browser asks (no referrer). bytes is 0 if unstated."""
    try:
        r = requests.get(url, headers=_BROWSER_UA, timeout=15, stream=True)
        try:
            ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image/")
            return ok, int(r.headers.get("content-length") or 0)
        finally:
            r.close()
    except Exception:
        return False, 0


def vet_image(url):
    """
    The URL to show, or "" to show no picture. Checked at build time so a dead,
    blocked or enormous image never reaches the page: it must load, and must be
    under IMG_BYTE_BUDGET. An oversized WordPress upload is served through
    WordPress.com's image CDN (i0.wp.com), which resizes it; any other oversized
    image is skipped rather than shipped at full size.
    """
    if not url:
        return ""
    ok, size = _probe_image(url)
    if not ok:
        return ""
    if size <= IMG_BYTE_BUDGET:
        return url
    p = _up.urlparse(url)
    if "/wp-content/uploads/" in p.path:
        small = f"https://i0.wp.com/{p.netloc}{p.path}?w=720&quality=78"
        if _probe_image(small)[0]:
            return small
    return ""


def vet_items(items):
    """Vet every item's picture in parallel (in place)."""
    todo = [i for i in items if i.get("image")]
    if todo:
        with cf.ThreadPoolExecutor(8) as ex:
            for it, url in zip(todo, ex.map(lambda i: vet_image(i["image"]), todo)):
                it["image"] = url


def fetch_feed(name, url, max_age_days, today, with_images=True):
    """Recent items from one feed, newest first: [{source,title,url,dek,date,tags}]. [] on any failure."""
    feed = None
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.ok:
            feed = feedparser.parse(r.content)
    except Exception:
        pass
    if not feed or not feed.entries:
        try:
            feed = feedparser.parse(url)   # feedparser's own agent; a few publishers only answer that
        except Exception:
            return []
    cutoff = today - datetime.timedelta(days=max_age_days)
    out = []
    for e in feed.entries[:60]:
        link = e.get("link", "")
        tags = [(t.get("term") or "") for t in e.get("tags", [])]
        if any(m in link for m in _SPONSORED_LINKS) or any(t.lower() in _SPONSORED_TAGS for t in tags):
            continue
        title = _text(e.get("title", ""))
        if not title or not link:
            continue
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        d = datetime.date(*parsed[:3]) if parsed else None
        if d and (d < cutoff or d > today + datetime.timedelta(days=1)):
            continue
        raw = e.get("summary", "") or ""
        if not raw and e.get("content"):
            raw = e["content"][0].get("value", "")
        dek = _text(raw, 210)
        if dek.lower().startswith(title.lower()[:40]):
            dek = ""
        out.append({"source": name, "title": title, "url": link, "dek": dek, "date": d, "tags": tags,
                    "image": _entry_image(e) if with_images else ""})
    out.sort(key=lambda i: i["date"] or datetime.date.min, reverse=True)
    return out


def fetch_pool(today, feeds=None):
    """Fetch every feed once, in parallel. {feed_id: [items]}."""
    feeds = feeds or FEEDS

    def one(fid):
        name, url, age = feeds[fid]
        try:
            return fid, fetch_feed(name, url, age, today, with_images=fid not in NO_IMAGE_FEEDS)
        except Exception:
            return fid, []
    with cf.ThreadPoolExecutor(8) as ex:
        return dict(ex.map(one, feeds))


# ─────────────────────────────────────────────
# iNATURALIST: what has been seen nearby this week
# ─────────────────────────────────────────────
INAT_API = "https://api.inaturalist.org/v1"
_INAT_GROUPS = [
    ("Birds", ("Aves",), 6),
    ("Plants & trees", ("Plantae",), 6),
    ("Fungi", ("Fungi",), 4),
    ("Insects & spiders", ("Insecta", "Arachnida"), 4),
    ("Mammals, reptiles & amphibians", ("Mammalia", "Reptilia", "Amphibia"), 3),
]


def fetch_inat_report(today, lat, lon, radius_km=2, days=7):
    """
    Verified ("research grade") iNaturalist observations within radius_km of the
    neighborhood in the past `days` days: totals, the most-recorded species in
    each group, and the newest sightings. Observers' names are not collected.
    None if the API is down or there is nothing to report.
    """
    try:
        d1 = (today - datetime.timedelta(days=days)).isoformat()
        base = {"lat": lat, "lng": lon, "radius": radius_km, "d1": d1, "d2": today.isoformat(),
                "quality_grade": "research", "locale": "en"}
        r = requests.get(f"{INAT_API}/observations/species_counts", headers=UA, timeout=30,
                         params={**base, "per_page": 200})
        r.raise_for_status()
        sc = r.json()
        results = sc.get("results", [])
        if not results:
            return None
        r2 = requests.get(f"{INAT_API}/observations", headers=UA, timeout=30,
                          params={**base, "per_page": 8, "order_by": "observed_on", "order": "desc"})
        r2.raise_for_status()
        recent = []
        for o in r2.json().get("results", []):
            t = o.get("taxon") or {}
            if not (t.get("preferred_common_name") or t.get("name")) or not o.get("observed_on"):
                continue
            nm = t.get("preferred_common_name") or t.get("name")
            recent.append({"id": o["id"], "name": nm.title() if nm == nm.lower() else nm,
                           "sci": t.get("name", ""), "date": datetime.date.fromisoformat(o["observed_on"])})
    except Exception as e:
        print(f"  [inat] skipped: {type(e).__name__}: {e}")
        return None

    groups = []
    for title, taxa, cap in _INAT_GROUPS:
        rows = [x for x in results if (x.get("taxon") or {}).get("iconic_taxon_name") in taxa]
        rows.sort(key=lambda x: -x["count"])
        picked = []
        for x in rows[:cap]:
            t = x["taxon"]
            photo = t.get("default_photo") or {}
            nm = t.get("preferred_common_name") or t.get("name")
            nm = nm.title() if nm == nm.lower() else nm
            picked.append({"id": t["id"], "name": nm,
                           "sci": t.get("name", ""), "count": x["count"],
                           # only photos that carry an open licence
                           "photo": photo.get("square_url") if photo.get("license_code") else None})
        if picked:
            groups.append((title, picked))
    return {"days": days, "radius_km": radius_km,
            "observations": sum(x["count"] for x in results), "species": len(results),
            "groups": groups, "recent": recent}


def render_inat(rep):
    """The Nature Report block."""
    if not rep:
        return ""
    lede = (f'<p class="inat-lede">In the past {rep["days"]} days, people within {rep["radius_km"]} km of '
            f'Fort Greene logged <b>{rep["observations"]:,}</b> verified observations of '
            f'<b>{rep["species"]}</b> species on iNaturalist.</p>')
    out = lede
    for title, rows in rep["groups"]:
        lis = ""
        for x in rows:
            img = (f'<img class="inat-thumb" src="{esc(x["photo"])}" width="40" height="40" alt="" loading="lazy">'
                   if x["photo"] else '<span class="inat-thumb inat-thumb--none"></span>')
            sci = f' <i>{esc(x["sci"])}</i>' if x["sci"] and x["sci"] != x["name"] else ""
            lis += (f'<li>{img}<span class="inat-name"><a href="https://www.inaturalist.org/taxa/{x["id"]}">'
                    f'{esc(x["name"])}</a>{sci}</span><span class="inat-count">&times;{x["count"]}</span></li>')
        out += f'<div class="news-subhead">{esc(title)}</div><ul class="inat-list">{lis}</ul>'
    if rep["recent"]:
        lis = "".join(
            f'<li><span class="inat-name"><a href="https://www.inaturalist.org/observations/{x["id"]}">'
            f'{esc(x["name"])}</a></span><span class="inat-count">{x["date"].strftime("%b %-d")}</span></li>'
            for x in rep["recent"][:6])
        out += f'<div class="news-subhead">Just logged</div><ul class="inat-list inat-list--plain">{lis}</ul>'
    out += ('<p class="page-note">Counts are verified community observations, so they show what people '
            'noticed and photographed, not everything that is there. Photos belong to their contributors '
            'under Creative Commons licenses. Source: iNaturalist.</p>')
    return out


# ─────────────────────────────────────────────
# STRUCTURED EVENTS (schema.org JSON-LD on a page) AND CITY PERMITS
# ─────────────────────────────────────────────
EVENT_PAGES = [
    ("Prospect Park", "https://www.prospectpark.org/events/", "Prospect Park"),
    ("NYC Transit Museum", "https://www.nytransitmuseum.org/programs/", ""),
]
_EVENT_SKIP = ("mobile medical", "greenmarket")


def fetch_jsonld_events(today, days=21, pages=EVENT_PAGES):
    """Upcoming events published as schema.org Event data: [{source,name,start,url,place}]."""
    out, seen = [], set()
    for source, url, default_place in pages:
        try:
            h = requests.get(url, headers=UA, timeout=20).text
            for s in BeautifulSoup(h, "html.parser").find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(s.string or "")
                except ValueError:
                    continue
                stack = [data]
                while stack:
                    o = stack.pop()
                    if isinstance(o, list):
                        stack += o
                    elif isinstance(o, dict):
                        t = o.get("@type")
                        t = t if isinstance(t, list) else [t]
                        if any(str(x).endswith("Event") for x in t) and o.get("name") and o.get("startDate"):
                            try:
                                start = datetime.datetime.fromisoformat(o["startDate"])
                            except ValueError:
                                start = None
                            name = _text(o["name"])
                            if (start and today <= start.date() <= today + datetime.timedelta(days=days)
                                    and not any(k in name.lower() for k in _EVENT_SKIP)
                                    and (source, name, start.date()) not in seen):
                                seen.add((source, name, start.date()))
                                loc = o.get("location")
                                place = (loc.get("name") if isinstance(loc, dict) else loc) or default_place
                                img = o.get("image")
                                img = img[0] if isinstance(img, list) and img else img
                                img = img.get("url", "") if isinstance(img, dict) else img
                                out.append({"source": source, "name": name, "start": start,
                                            "url": o.get("url", ""), "place": place or source,
                                            "image": img if isinstance(img, str) else ""})
                        stack += [v for v in o.values() if isinstance(v, (dict, list))]
        except Exception as e:
            print(f"  [events] {source} skipped: {type(e).__name__}: {e}")
    out.sort(key=lambda e: e["start"])
    return out


def fetch_permit_events(today, days=14, boards=("8", "9")):
    """
    Street and park event permits in Community Boards 8 and 9 (Fort Greene),
    from NYC Open Data, leaving out the youth/adult sports bookings that make up
    most of the dataset. [] if the dataset is down. Thin in winter, busier in
    warm months (block parties, parades, street fairs).
    """
    try:
        r = requests.get(
            "https://data.cityofnewyork.us/resource/tvpp-9vvx.json", headers=UA, timeout=40,
            params={"$select": "event_name,event_type,start_date_time,event_location,community_board",
                    "$where": ("event_borough='Brooklyn' AND start_date_time >= '%sT00:00:00' "
                               "AND start_date_time < '%sT00:00:00'") % (
                                   today.isoformat(), (today + datetime.timedelta(days=days)).isoformat()),
                    "$order": "start_date_time", "$limit": 3000})
        r.raise_for_status()
        out = []
        for x in r.json():
            if x.get("event_type", "").startswith(("Sport", "Shooting", "Production")):
                continue
            if not set(re.findall(r"\d+", x.get("community_board", ""))) & set(boards):
                continue
            out.append({"name": _text(x.get("event_name", "")), "type": x.get("event_type", ""),
                        "start": datetime.datetime.fromisoformat(x["start_date_time"]),
                        "place": _text(x.get("event_location", ""), 90)})
        return out
    except Exception as e:
        print(f"  [permits] skipped: {type(e).__name__}: {e}")
        return []


# ─── STYLES ───
# The Gemini "Matte Bright Newsprint" design, shared by this page and the
# Fort Greene Community News page, then what the News & Culture page adds.
NEWSPRINT_CSS = r"""  :root {
    --ground: #EFEFE9;
    --card: #F9F9F6;
    --panel: #F2F2EC;
    --ink: #111111;
    --body: #1E1E1E;
    --muted: #4A4A48;
    --faint: #636360;
    --accent: #9B111E;
    --rule: #CCCCCC;
    --rule-soft: #DFDFD9;
    --serif: 'Cormorant Garamond', Georgia, 'Times New Roman', serif;
    --mono: 'Courier Prime', 'Courier New', ui-monospace, monospace;
  }

  /* Pure-CSS Dark Mode Toggle (Default is Light Mode) */
  #theme-switch:checked ~ .wrap {
    --ground: #0E0E0D;
    --card: #161615;
    --panel: #1F1F1D;
    --ink: #F4F4F0;
    --body: #E2E2DC;
    --muted: #A8A8A2;
    --faint: #8E8E88;
    --accent: #E45565;
    --rule: #333330;
    --rule-soft: #262624;
  }

  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--ground);
    color: var(--body);
    font-family: var(--serif);
    font-size: 17px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }

  /* Ensure outer viewport background also switches in dark mode */
  html:has(#theme-switch:checked),
  body:has(#theme-switch:checked) {
    background: #0E0E0D;
    color: #E2E2DC;
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .wrap {
    max-width: 900px;
    margin: 0 auto;
    padding: 26px 18px 64px;
  }

  a { color: var(--accent); }
  a:focus-visible,
  #theme-switch:focus-visible + .wrap .theme-toggle {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }

  /* Broadsheet Sheet Container with Double Border */
  .sheet {
    background: var(--card);
    border: 1px solid var(--rule);
    padding: 32px 32px 36px;
    position: relative;
  }
  .sheet::after {
    content: "";
    position: absolute;
    inset: 5px;
    border: 1px solid var(--rule-soft);
    pointer-events: none;
  }

  /* Top Utility Row & Pure-CSS Theme Toggle */
  .top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin: 0 0 18px;
    position: relative;
    z-index: 2;
  }
  .kicker {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0;
  }
  .theme-toggle {
    background: transparent;
    border: 1px solid var(--rule);
    color: var(--muted);
    font-family: var(--mono);
    font-size: 8.5px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    padding: 6px 12px;
    min-height: 44px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    user-select: none;
  }
  .theme-toggle:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .theme-toggle .label-day { display: none; }
  #theme-switch:checked ~ .wrap .theme-toggle .label-night { display: none; }
  #theme-switch:checked ~ .wrap .theme-toggle .label-day { display: inline; }

  /* Illustrated Masthead */
  .masthead {
    text-align: center;
  }
  .vignette {
    max-width: 340px;
    margin: 0 auto 14px;
    color: var(--ink);
  }
  .vignette svg {
    width: 100%;
    height: auto;
    display: block;
  }
  .masthead h1 {
    font-size: clamp(32px, 6.2vw, 52px);
    line-height: 1.02;
    margin: 0;
    color: var(--ink);
    font-weight: 600;
    letter-spacing: -0.015em;
    text-wrap: balance;
  }
  .masthead .issue-tag {
    display: block;
    font-size: clamp(21px, 3.8vw, 27px);
    font-weight: 400;
    font-style: italic;
    color: var(--accent);
    margin-top: 6px;
  }

  /* Classic Double-Rule Dateline Bar */
  .dateline {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px 16px;
    margin: 20px 0 0;
    padding: 7px 6px;
    border-top: 2px solid var(--ink);
    border-bottom: 1px solid var(--ink);
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--ink);
  }
  .dateline-sub {
    height: 2px;
    border-bottom: 1px solid var(--rule);
    margin-bottom: 20px;
  }

  /* Edition Deck & Action Buttons */
  .edition-bar {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 20px 28px;
    align-items: center;
    padding-bottom: 22px;
    border-bottom: 1px solid var(--rule);
    text-align: left;
    position: relative;
    z-index: 1;
  }
  .standfirst {
    margin: 0;
    color: var(--muted);
    font-style: italic;
    font-size: 16.5px;
    line-height: 1.52;
  }
  .links {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 245px;
  }
  .cta {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 44px;
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    text-decoration: none;
    padding: 10px 16px;
    border: 1px solid var(--ink);
    color: var(--ink);
    text-align: center;
  }
  .cta--solid {
    background: var(--ink);
    color: var(--card);
  }
  .cta:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .cta--solid:hover {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--card);
  }

  /* Section Banner */
  .section-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 22px 0 20px;
    padding: 6px 10px;
    background: var(--panel);
    border-top: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    color: var(--accent);
  }
  .section-banner .rom {
    font-family: var(--serif);
    font-style: italic;
    font-size: 14px;
    letter-spacing: 0;
    text-transform: none;
    color: var(--faint);
  }

  /* Two-Column Broadsheet Flow */
  .broadsheet-cols {
    list-style: none;
    margin: 0;
    padding: 0;
    column-count: 2;
    column-gap: 36px;
    column-rule: 1px solid var(--rule);
    position: relative;
    z-index: 1;
  }
  .b-story {
    break-inside: avoid;
    padding: 0 0 16px;
    margin: 0 0 16px;
    border-bottom: 1px solid var(--rule-soft);
  }
  .b-story-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 4px;
  }
  .b-page {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .b-byline {
    font-family: var(--mono);
    font-size: 8.5px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--faint);
    text-align: right;
  }
  .b-title {
    margin: 0;
    font-size: 18.5px;
    line-height: 1.24;
    font-weight: 600;
  }
  .b-title a {
    color: var(--ink);
    text-decoration: none;
    display: inline-block;
    min-height: 44px;
    padding: 2px 0;
  }
  .b-title a:hover {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent);
    text-underline-offset: 3px;
  }
  .b-blurb {
    margin: 3px 0 0;
    font-size: 15.5px;
    line-height: 1.46;
    color: var(--body);
  }

  /* Disclosure & Footer */
  .disclosure {
    margin: 18px 0 0;
    padding-top: 12px;
    border-top: 1px solid var(--rule);
    font-style: italic;
    font-size: 13.5px;
    color: var(--muted);
  }
  .foot {
    margin-top: 28px;
    padding-top: 14px;
    border-top: 1px solid var(--rule);
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--faint);
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: space-between;
  }
  .foot a {
    color: var(--faint);
    text-decoration: none;
    border-bottom: 1px solid var(--rule);
  }
  .foot a:hover {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  @media (max-width: 680px) {
    .sheet {
      padding: 24px 16px 26px;
    }
    .top-bar {
      flex-direction: column;
      align-items: flex-start;
      gap: 10px;
    }
    .edition-bar {
      grid-template-columns: 1fr;
    }
    .links {
      min-width: 0;
    }
    .broadsheet-cols {
      column-count: 1;
      column-rule: none;
    }
  }
  /* Edition bar with buttons only (no intro paragraph) */
  .edition-bar--solo { grid-template-columns: 1fr; justify-items: center; }
  .edition-bar--solo .links { flex-direction: row; flex-wrap: wrap; justify-content: center; min-width: 0; }
  .edition-bar--solo .cta { min-width: 230px; }
  @media (max-width: 680px) { .edition-bar--solo .links { flex-direction: column; width: 100%; } .edition-bar--solo .cta { min-width: 0; } }
"""
VIGNETTE_SVG = """<svg viewBox="0 0 320 68" fill="none" xmlns="http://www.w3.org/2000/svg">
          <line x1="8" y1="64" x2="312" y2="64" stroke="currentColor" stroke-width="1"/>
          <rect x="28" y="18" width="84" height="46" stroke="currentColor" stroke-width="1"/>
          <line x1="24" y1="18" x2="116" y2="18" stroke="currentColor" stroke-width="1.5"/>
          <line x1="28" y1="13" x2="112" y2="13" stroke="currentColor" stroke-width="1"/>
          <rect x="40" y="25" width="12" height="16" stroke="currentColor" stroke-width="0.9"/>
          <rect x="64" y="25" width="12" height="16" stroke="currentColor" stroke-width="0.9"/>
          <rect x="88" y="25" width="12" height="16" stroke="currentColor" stroke-width="0.9"/>
          <rect x="40" y="45" width="12" height="13" stroke="currentColor" stroke-width="0.9"/>
          <rect x="64" y="45" width="12" height="13" stroke="currentColor" stroke-width="0.9"/>
          <path d="M88 64V48C88 45.8 90.7 44 94 44C97.3 44 100 45.8 100 48V64" stroke="currentColor" stroke-width="0.9"/>
          <rect x="112" y="8" width="96" height="56" stroke="currentColor" stroke-width="1"/>
          <path d="M108 8H212M112 4H208" stroke="currentColor" stroke-width="1.3"/>
          <rect x="126" y="16" width="13" height="17" stroke="currentColor" stroke-width="0.9"/>
          <rect x="153.5" y="16" width="13" height="17" stroke="currentColor" stroke-width="0.9"/>
          <rect x="181" y="16" width="13" height="17" stroke="currentColor" stroke-width="0.9"/>
          <rect x="126" y="39" width="13" height="17" stroke="currentColor" stroke-width="0.9"/>
          <path d="M153.5 64V44C153.5 41.2 156.4 39 160 39C163.6 39 166.5 41.2 166.5 44V64" stroke="currentColor" stroke-width="1"/>
          <rect x="181" y="39" width="13" height="17" stroke="currentColor" stroke-width="0.9"/>
          <rect x="208" y="20" width="84" height="44" stroke="currentColor" stroke-width="1"/>
          <line x1="204" y1="20" x2="296" y2="20" stroke="currentColor" stroke-width="1.5"/>
          <rect x="220" y="27" width="12" height="15" stroke="currentColor" stroke-width="0.9"/>
          <rect x="244" y="27" width="12" height="15" stroke="currentColor" stroke-width="0.9"/>
          <rect x="268" y="27" width="12" height="15" stroke="currentColor" stroke-width="0.9"/>
          <path d="M220 64V49C220 46.8 222.7 45 226 45C229.3 45 232 46.8 232 49V64" stroke="currentColor" stroke-width="0.9"/>
          <rect x="244" y="46" width="12" height="12" stroke="currentColor" stroke-width="0.9"/>
          <rect x="268" y="46" width="12" height="12" stroke="currentColor" stroke-width="0.9"/>
        </svg>"""

NC_CSS = r"""
  /* Section tabs, CSS only. Every tab is a link to #slug. An empty anchor with that id sits just above the
     pinned tab bar, so following the link lands right under the tabs, and :target shows its panel and
     marks its tab. With no section in the address, the first one (Fort Greene) is shown. */
  .anchor { display: block; height: 0; overflow: hidden; }
  .jump { position: sticky; top: 0; z-index: 20; display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 0 12px; margin: 0 0 22px;
          background: var(--card); border-bottom: 1px solid var(--rule); box-shadow: 0 8px 8px -8px rgba(0, 0, 0, 0.22); }
  .jump a { display: inline-flex; align-items: center; min-height: 40px; padding: 6px 11px; border: 1px solid var(--rule); white-space: nowrap;
            font-family: var(--mono); font-size: 8.5px; letter-spacing: 0.16em; text-transform: uppercase;
            color: var(--muted); text-decoration: none; background: var(--card); }
  .jump a:hover { border-color: var(--accent); color: var(--accent); }
  @media (min-width: 681px) { .jump { gap: 4px; } .jump a { padding: 6px 8px; letter-spacing: 0.12em; } }
  .panel { display: none; }
  .sheet:not(:has(.anchor:target)) .panel:first-child { display: block; }
  @@TAB_STATE@@
  @supports not selector(:has(*)) { .panel { display: block; } }
  .section-banner h2 { margin: 0; font: inherit; letter-spacing: inherit; text-transform: inherit; color: inherit; }
  .pointer { margin: -4px 0 16px; font-style: italic; font-size: 14.5px; color: var(--muted); position: relative; z-index: 1; }
  .pointer a { color: var(--accent); }
  .feature { border: 1px solid var(--rule); border-left: 3px solid var(--accent); padding: 14px 16px 12px; margin: 0 0 20px; background: var(--panel); position: relative; z-index: 1; }
  .feature .b-title a { min-height: 0; }
  .feature .b-blurb { margin-top: 2px; }
  .empty-note { margin: 0 0 20px; font-style: italic; color: var(--muted); position: relative; z-index: 1; }
  .step { display: flex; justify-content: space-between; gap: 12px; margin-top: 6px; padding-top: 4px; border-top: 1px solid var(--rule); position: relative; z-index: 1; }
  .step a { display: inline-flex; align-items: center; min-height: 44px; font-family: var(--mono); font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent); text-decoration: none; }
  .step a:hover { text-decoration: underline; }
  .step .next { margin-left: auto; text-align: right; }
  /* Pictures: the publisher's own, cropped to a wide frame and toned down to sit on newsprint (colour on hover). */
  .b-fig { margin: 0 0 8px; }
  .b-photo { display: block; aspect-ratio: 16 / 9; overflow: hidden; background: var(--panel); border: 1px solid var(--rule-soft); }
  .b-photo img { display: block; width: 100%; height: 100%; object-fit: cover; filter: grayscale(0.3) contrast(1.04); transition: filter 0.25s; }
  .b-photo:hover img { filter: none; }
  #theme-switch:checked ~ .wrap .b-photo img { filter: grayscale(0.3) contrast(1.04) brightness(0.88); }
  #theme-switch:checked ~ .wrap .b-photo:hover img { filter: brightness(0.95); }
  .b-credit { margin-top: 3px; font-family: var(--mono); font-size: 7.5px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--faint); }
  @media (prefers-reduced-motion: reduce) { .b-photo img { transition: none; } }
  .nc-note { margin: 28px 0 0; padding-top: 12px; border-top: 1px solid var(--rule); font-style: italic; font-size: 13.5px; color: var(--muted); }
  @media (max-width: 680px) {
    /* phones: one swipeable row instead of five wrapped rows */
    .jump { flex-wrap: nowrap; overflow-x: auto; padding: 8px 0 10px; scrollbar-width: none; -webkit-overflow-scrolling: touch;
            -webkit-mask-image: linear-gradient(to right, #000 90%, transparent); mask-image: linear-gradient(to right, #000 90%, transparent); }
    .jump::-webkit-scrollbar { display: none; }
    .jump a { flex: none; }
  }
"""

# The Nature Report block sits on the main page (warm-paper palette)
INAT_CSS = r"""  .page-note { font-style: italic; font-size: 13px !important; color: #8A6A3A !important; line-height: 1.5 !important; margin-top: 14px !important; }
  .inat-lede { margin-bottom: 4px !important; }
  .inat-list { list-style: none; margin: 0 0 6px; padding: 0; }
  .inat-list li { display: flex; align-items: center; gap: 12px; padding: 6px 0; border-bottom: 1px dotted #D4B98A; }
  .inat-list li:last-child { border-bottom: none; }
  .inat-thumb { width: 40px; height: 40px; flex: none; object-fit: cover; border-radius: 2px; background: #EFE3C3; }
  .inat-thumb--none { display: inline-block; }
  .inat-list--plain li { padding: 5px 0; }
  .inat-name { flex: 1; font-size: 15.5px; line-height: 1.25; min-width: 0; overflow-wrap: break-word; }
  .inat-name a { color: #1A1208; font-weight: 600; text-decoration: none; border-bottom: 1px solid #D4B98A; }
  .inat-name a:hover { color: #9B3A1A; border-bottom-color: #9B3A1A; }
  .inat-name i { color: #8A6A3A; font-size: 13px; margin-left: 4px; }
  .inat-count { font-family: 'Courier Prime', monospace; font-size: 9.5px; letter-spacing: 0.06em; color: #8A6A3A; white-space: nowrap; }"""


# ─── HELPERS ───

def story(left, right, title, url="", dek="", image="", credit=""):
    """One broadsheet entry: small caps source, date at the right, picture, headline, publisher's summary."""
    hed = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
    r = f'<span class="b-byline">{esc(right)}</span>' if right else ""
    d = f'<p class="b-blurb">{esc(dek)}</p>' if dek else ""
    pic = ""
    if image and url:
        # no referrer, so publishers that block hotlinking by Referer still serve it; if it fails
        # to load anyway the whole figure removes itself and the story reads as text
        pic = (f'<figure class="b-fig"><a class="b-photo" href="{esc(url)}" tabindex="-1" aria-hidden="true">'
               f'<img src="{esc(image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" '
               f'onerror="this.closest(\'.b-fig\').remove()"></a>'
               f'<figcaption class="b-credit">Image: {esc(credit or left)}</figcaption></figure>')
    return (f'<li class="b-story"><div class="b-story-meta"><span class="b-page">{esc(left)}</span>{r}</div>'
            f'{pic}<h3 class="b-title">{hed}</h3>{d}</li>')


def round_robin(pool, feed_ids, total, seen, filt=None):
    """Up to `total` fresh items, taking from each feed in turn so one source can't fill the section."""
    queues = [[it for it in pool.get(fid, []) if not filt or filt(it)] for fid in feed_ids]
    out = []
    while len(out) < total and any(queues):
        for q in queues:
            while q:
                it = q.pop(0)
                if it["url"] in seen or it["title"].lower() in seen:
                    continue
                seen.add(it["url"]); seen.add(it["title"].lower())
                out.append(it)
                break
            if len(out) >= total:
                break
    return out


def _when(dt):
    s = dt.strftime("%a %b %-d")
    return s if dt.hour == 0 and dt.minute == 0 else f"{s} \u00b7 {dt.strftime('%-I:%M %p')}"


# ─── THE PAGE ───

def build_news_culture(ctx):
    """
    The separate News & Culture page. Returns (html, headlines, count): the page,
    the five-headline strip the main page shows (one headline per section, the
    newest five, feed items only), and how many items the page carries.

    ctx: pool (feed items), events, permits, chco_issue, chco_url, saturday,
    sunday, main_urls (links already on the main page: never repeated here),
    version (cache-busting stamp for the link back to the main page).
    """
    pool = ctx["pool"]
    seen = set(ctx.get("main_urls") or ())
    sat, sun = ctx["saturday"], ctx["sunday"]
    issue, chco_url = ctx.get("chco_issue"), ctx.get("chco_url")
    q = f"?v={ctx['version']}" if ctx.get("version") else ""
    body, headlines_by_section, count = "", {}, 0

    for n, sec in enumerate(SECTIONS):
        items_html, feed_items, lead = "", [], ""
        slots = ITEMS_PER_SECTION
        try:
            if sec["slug"] == "crown-heights":
                if issue and chco_url:
                    vol = f"Vol. {issue['volume']} \u00b7 No. {issue['number']}" if issue.get("volume") and issue.get("number") else ""
                    lead = (f'<div class="feature"><div class="b-story-meta"><span class="b-page">Fort Greene Community News</span>'
                            f'<span class="b-byline">{esc(vol)}</span></div>'
                            f'<h3 class="b-title"><a href="{esc(chco_url)}">The {esc(issue["month"].split()[0])} Issue</a></h3>'
                            f'<p class="b-blurb">Posted {esc(issue["posted"].strftime("%B %-d"))}</p></div>')
                    count += 1
                permits = (ctx.get("permits") or [])[:4]
                for e in permits:
                    items_html += story(e["type"] or "Permit", _when(e["start"]), e["name"], "", e["place"])
                slots -= len(permits)
                count += len(permits)
                mentions = []
                for items in pool.values():
                    for it in items:
                        text = f"{it['title']} {it['dek']}".lower()
                        if any(k in text for k in CH_KEYWORDS) and it["url"] not in seen:
                            mentions.append(it)
                mentions.sort(key=lambda i: i["date"] or datetime.date.min, reverse=True)
                for it in mentions[:max(slots, 0)]:
                    seen.add(it["url"]); feed_items.append(it)
            elif sec["slug"] == "music-events":
                try:
                    lead = dm.record_html(sat)        # a public-domain recording, playable on the page
                    count += 1 if lead else 0
                except Exception as e:
                    print(f"  [news-culture] record of the week skipped: {type(e).__name__}: {e}")
                evs = [e for e in (ctx.get("events") or []) if e["url"] not in seen][:4]
                for e in evs:
                    items_html += story(e["place"], _when(e["start"]), e["name"], e["url"])
                count += len(evs)
                feed_items = round_robin(pool, sec["feeds"], slots - len(evs), seen)
            elif sec["slug"] == "books":
                wanted = ("book", "fiction", "poem", "poetry", "novel")
                feed_items = round_robin(pool, sec["feeds"] + ["newyorker"], slots, seen,
                                         filt=lambda it: it["source"] != "The New Yorker"
                                         or any(w in " ".join(it["tags"]).lower() for w in wanted))
            else:
                feed_items = round_robin(pool, sec["feeds"], slots, seen)
        except Exception as e:  # one broken source must never take the page down
            print(f"  [news-culture] {sec['slug']} partly failed: {type(e).__name__}: {e}")
        vet_items(feed_items)
        for it in feed_items:
            items_html += story(it["source"], it["date"].strftime("%b %-d") if it["date"] else "", it["title"], it["url"], it["dek"],
                                it.get("image", ""), it["source"])
        count += len(feed_items)
        dated = [i for i in feed_items if i["date"]]
        if dated:
            top = max(dated, key=lambda i: i["date"])
            headlines_by_section[sec["slug"]] = {"category": _html.unescape(sec["title"]), "source": top["source"], "title": top["title"],
                                                 "url": top["url"], "dek": top["dek"], "date": top["date"]}
        pointer = f'<p class="pointer">{sec["pointer"]} <a href="{HOME_FILE}{q}">Main page &rarr;</a></p>' if sec["pointer"] else ""
        listing = f'<ol class="broadsheet-cols">{items_html}</ol>' if items_html else ""
        if not (lead or listing):
            listing = '<p class="empty-note">Nothing new from these sources this week.</p>'
        prev_s = SECTIONS[n - 1] if n > 0 else None
        next_s = SECTIONS[n + 1] if n < len(SECTIONS) - 1 else None
        step = ((f'<a class="prev" href="#{prev_s["slug"]}">&larr; {esc(prev_s["short"])}</a>' if prev_s else "")
                + (f'<a class="next" href="#{next_s["slug"]}">{esc(next_s["short"])} &rarr;</a>' if next_s else ""))
        body += f"""
      <section class="panel" id="p-{sec['slug']}" aria-labelledby="h-{sec['slug']}">
        <h2 class="section-banner" id="h-{sec['slug']}"><span>{sec['title']}</span><span class="rom">{ROMAN[n]}</span></h2>
        {pointer}{lead}{listing}
        <div class="step">{step}</div>
      </section>"""

    headlines = sorted(headlines_by_section.values(), key=lambda h: h["date"], reverse=True)[:5]
    anchors = "".join(f'<span class="anchor" id="{s["slug"]}"></span>' for s in SECTIONS)
    jump = "".join(f'<a href="#{s["slug"]}">{esc(s["short"])}</a>' for s in SECTIONS)
    # which panel shows and which tab is marked, for each section in the address
    active = "background: var(--ink); border-color: var(--ink); color: var(--card);"
    tab_state = (
        ", ".join(f'#{s["slug"]}:target ~ .jump a[href="#{s["slug"]}"]' for s in SECTIONS)
        + f", .sheet:not(:has(.anchor:target)) .jump a:first-child {{ {active} }}\n  "
        + ", ".join(f'#{s["slug"]}:target ~ .panels #p-{s["slug"]}' for s in SECTIONS) + " { display: block; }")
    chco_cta = (f'<a class="cta" href="{esc(chco_url)}">Fort Greene Community News</a>' if issue and chco_url else "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>News &amp; Culture &middot; Weekend Dispatch</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>{NEWSPRINT_CSS}{NC_CSS.replace('@@TAB_STATE@@', tab_state)}{dm.RECORD_CSS}</style>
</head>
<body>
<input type="checkbox" id="theme-switch" class="visually-hidden" aria-label="Toggle dark mode">
<div class="wrap">
  <main class="sheet">
    <div class="top-bar">
      <p class="kicker"><a href="{HOME_FILE}{q}">Weekend Dispatch</a> &middot; News &amp; Culture</p>
      <label for="theme-switch" class="theme-toggle">
        <span class="label-night">Night Edition</span>
        <span class="label-day">Day Edition</span>
      </label>
    </div>

    <header class="masthead">
      <div class="vignette" aria-hidden="true">
        {VIGNETTE_SVG}
      </div>
      <h1>
        News &amp; Culture
        <span class="issue-tag">the weekend of {esc(sat.strftime("%B %-d"))}</span>
      </h1>
      <div class="dateline">
        <span>Saturday {esc(sat.strftime("%b %-d"))} &middot; Sunday {esc(sun.strftime("%-d"))}</span>
        <span>{count} items &middot; {len(SECTIONS)} sections</span>
        <span>Refreshed every Saturday</span>
      </div>
      <div class="dateline-sub" aria-hidden="true"></div>
      <div class="edition-bar edition-bar--solo">
        <div class="links">
          <a class="cta cta--solid" href="{HOME_FILE}{q}">Back to the dispatch</a>
          {chco_cta}
        </div>
      </div>
    </header>

    {anchors}
    <nav class="jump" aria-label="Sections">{jump}</nav>
    <div class="panels">{body}
    </div>

    <p class="nc-note">Summaries and pictures are each publication&rsquo;s own, shown as a feed reader would and linking back to the story. Paywalled sources show only their headline and standfirst.</p>
  </main>

  <footer class="foot">
    <span>Fort Greene &middot; Brooklyn</span>
    <span><a href="{HOME_FILE}{q}">The dispatch</a></span>
  </footer>
</div>
</body>
</html>"""
    return html, headlines, count
