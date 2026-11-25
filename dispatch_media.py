"""
Media for the Weekend Dispatch: the animated envelope GIF for the email, and
the open-licence sound sources (public-domain / Creative Commons only) used on
the pages.

Kept apart from weekend_dispatch.py so the main file does not grow, and with
no import of it (weekend_dispatch imports this module, not the other way
round). Every fetcher fails closed: on any error it returns None / [] and the
page simply renders without that piece.
"""

import io
import math
import re

import requests

WIKI_UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter; github.com/zach-works/weekend-dispatch-showcase)"}

# ─────────────────────────────────────────────
# ANIMATED ENVELOPE (email)
# ─────────────────────────────────────────────
# The still photo (assets/envelope-placeholder.jpg, 1200x760) already has this
# Saturday's date stamped into the postmark by weekend_dispatch. The GIF keeps
# that exact image as frame 1 (mail apps that do not animate GIFs, such as
# Outlook desktop, show only the first frame, so it has to be the finished
# envelope) and animates two small regions on top of it:
#   1. the ZT pigeon peeks up over the envelope's top edge and bobs its head
#   2. a glint sweeps across the wax seal
# Only those regions change between frames, so the file stays small.

PIGEON_CUTOUT = "assets/pigeon-grey.png"
# Geometry in the 1200x760 source image: the envelope body (anything inside
# this box is drawn over the pigeon, so it rises from behind the envelope) and
# the wax seal.
ENV_BOX = (157, 127, 1043, 623)           # left, top, right, bottom
SEAL_CENTER, SEAL_RADIUS = (607, 358), 100
STAMP_BOX = (845, 283, 927, 385)          # the postage stamp, whose pigeon is the same blue
PIGEON_X = 905                            # centre of the pigeon, above the pigeon stamp
PIGEON_PEEK_TOP = 8                       # where the top of its head sits when fully up
PIGEON_HEIGHT = 240


def _pigeon_sprite():
    from PIL import Image
    p = Image.open(PIGEON_CUTOUT).convert("RGBA")
    w = round(p.width * PIGEON_HEIGHT / p.height)
    return p.resize((w, PIGEON_HEIGHT), Image.LANCZOS)


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _frame_specs():
    """
    The timeline: (kind, amount, ms). kind is "base", "pigeon" (amount =
    (lift 0..1, head-bob -1..1)) or "glint" (amount = position 0..1).
    """
    seq = [("base", 0, 2200)]
    for i in range(1, 6):                                    # rises from behind the envelope
        seq.append(("pigeon", (_ease_out(i / 5), 0.0), 65))
    for b in (0.6, 1.0, 0.2, -0.5, -1.0, -0.3, 0.5, 0.9, 0.0):   # looks about
        seq.append(("pigeon", (1.0, b), 120))
    seq.append(("pigeon", (1.0, 0.0), 450))
    for i in range(1, 5):                                    # ducks back down
        seq.append(("pigeon", (1 - _ease_out(i / 4) * 0.97, 0.0), 65))
    seq.append(("base", 0, 500))
    for i in range(8):                                       # glint across the seal
        seq.append(("glint", i / 7, 55))
    return seq


def build_envelope_gif(base_rgb, width=900):
    """
    base_rgb: PIL image (the 1200x760 envelope with the date already in the
    postmark). Returns animated GIF bytes, width x (width*760/1200) pixels.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    base = base_rgb.convert("RGB")
    W, H = base.size
    sprite = _pigeon_sprite()
    sprite_top_up = PIGEON_PEEK_TOP - 3        # the cutout is cropped tight: the head starts ~3px down
    env_mask = Image.new("L", base.size, 0)
    env_mask.paste(255, ENV_BOX)
    env_mask = env_mask.filter(ImageFilter.GaussianBlur(0.8))

    yy, xx = np.mgrid[0:H, 0:W]
    sx, sy = SEAL_CENTER
    seal_dist = np.hypot(xx - sx, yy - sy)
    arr = np.asarray(base, dtype=float)
    # wax is red-brown; the paper around it is cream (R close to G)
    wax = np.clip((arr[..., 0] - arr[..., 1] - 28) / 22, 0, 1) * np.clip((SEAL_RADIUS + 14 - seal_dist) / 10, 0, 1)

    def pigeon_frame(lift, bob):
        frame = base.copy()
        spr = sprite
        if bob:
            spr = sprite.rotate(-3.5 * bob, resample=Image.BICUBIC,
                                center=(sprite.width // 2, sprite.height - 20), expand=False)
        # lift 0 = hidden behind the envelope, 1 = fully up
        y = round(sprite_top_up + (1 - lift) * (ENV_BOX[1] - sprite_top_up + 10))
        x = round(PIGEON_X - spr.width / 2 + 9 * bob)
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        layer.paste(spr, (x, y))
        frame.paste(layer.convert("RGB"), (0, 0), layer.getchannel("A"))
        frame.paste(base, (0, 0), env_mask)                  # envelope covers the pigeon's lower half
        return frame

    def glint_frame(pos):
        # a soft diagonal band travelling left to right across the seal
        centre = (pos * 2.4 - 0.7) * SEAL_RADIUS
        band = np.exp(-(((xx - sx) + (yy - sy) * 0.55 - centre) / (SEAL_RADIUS * 0.28)) ** 2)
        lift = (band * wax * 0.55)[..., None]
        out = arr * (1 - lift) + 255 * lift
        return Image.fromarray(out.clip(0, 255).astype("uint8"))

    specs = _frame_specs()
    frames = []
    for kind, amount, ms in specs:
        if kind == "base":
            frames.append(base)
        elif kind == "pigeon":
            frames.append(pigeon_frame(*amount))
        else:
            frames.append(glint_frame(amount))

    out_size = (width, round(width * H / W))

    # One shared 256-colour palette for every frame (quantising frames one by
    # one would flicker). A single median-cut over the whole picture drops the
    # blue jacket, because blue is a sliver of the pixels, so the palette is
    # assembled in three parts: the photo, the pigeon sprite, and the pigeon
    # on the stamp (which is the same blue).
    def part(img, colors):
        q = img.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        return q.getpalette()[:colors * 3]

    sp_px = np.asarray(sprite)
    sp_px = sp_px[sp_px[..., 3] > 128][:, :3].astype("uint8")
    sprite_img = Image.fromarray(sp_px.reshape(1, -1, 3))
    stamp_img = base.crop(STAMP_BOX)
    pal = (part(base.resize(out_size, Image.LANCZOS), 168)
           + part(sprite_img, 56) + part(stamp_img, 32))
    pal += [0] * (768 - len(pal))
    palette = Image.new("P", (1, 1))
    palette.putpalette(pal)

    p_base = base.resize(out_size, Image.LANCZOS).quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
    seq = []
    for (kind, _a, ms), f in zip(specs, frames):
        if kind == "base":
            seq.append(p_base.copy())
            continue
        small = f.resize(out_size, Image.LANCZOS)
        # Quantise only where this frame differs from the base, so the unchanged
        # pixels stay byte-identical to the base frame and the GIF stores almost
        # nothing for them.
        diff = np.abs(np.asarray(small, dtype=int) - np.asarray(base.resize(out_size, Image.LANCZOS), dtype=int)).sum(-1)
        ys, xs = np.where(diff > 6)
        pf = p_base.copy()
        if len(xs):
            box = (max(xs.min() - 2, 0), max(ys.min() - 2, 0), min(xs.max() + 3, out_size[0]), min(ys.max() + 3, out_size[1]))
            patch = small.crop(box).quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
            pf.paste(patch, box[:2])
        seq.append(pf)

    buf = io.BytesIO()
    seq[0].save(buf, "GIF", save_all=True, append_images=seq[1:], duration=[s[2] for s in specs],
                loop=3, optimize=False, disposal=1)
    return buf.getvalue()


# ─────────────────────────────────────────────
# OPEN-LICENCE AUDIO (Wikimedia Commons)
# ─────────────────────────────────────────────
# Sound on the pages comes only from Wikimedia Commons files whose own licence
# metadata says public domain, CC0, CC BY or CC BY-SA. The licence is read from
# Commons at build time (not assumed), and anything NonCommercial or
# NoDerivatives is refused. Each clip is credited from that same metadata.

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_OPEN_LICENCE = re.compile(r"^(?:public domain|pd\b|cc0|cc[- ]zero|cc[- ]by(?:[- ]sa)?\b)", re.I)
_RESTRICTED = re.compile(r"\b(?:nc|nd)\b", re.I)
_clip_cache = {}


def is_open_licence(name):
    name = (name or "").strip()
    return bool(_OPEN_LICENCE.match(name)) and not _RESTRICTED.search(name)


def _strip_tags(s):
    import html
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _wiki_api(url, params, tries=5):
    """GET a Wikimedia API as JSON. Rate limits (HTTP 429) are waited out, using the server's Retry-After."""
    import time
    r = None
    for attempt in range(tries):
        r = requests.get(url, headers=WIKI_UA, timeout=20, params=params)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        try:
            asked = float(r.headers.get("Retry-After") or 0) if r.status_code == 429 else 0
        except ValueError:
            asked = 0
        time.sleep(min(max(asked + 1, 4 * (attempt + 1)), 60))
    raise RuntimeError(f"Wikimedia did not answer ({r.status_code if r is not None else 'no response'})")


def _commons_json(params, tries=4):
    return _wiki_api(COMMONS_API, params, tries)


def commons_audio(filename):
    """
    Resolve a Commons audio file: {"sources": [(url, mime), ...], "title", "artist",
    "date", "description", "licence", "page", "credit"}. The MP3 transcode that
    Commons makes comes first when there is one (Safari and iPhones do not play
    every Ogg file); the original follows. None if the file is missing, is not
    audio, or is not openly licensed, so the page just leaves the clip out.
    """
    if filename in _clip_cache:
        return _clip_cache[filename]
    clip = None
    try:
        data = _commons_json({"action": "query", "format": "json", "titles": f"File:{filename}", "prop": "videoinfo",
                              "viprop": "url|mime|mediatype|extmetadata|derivatives"})
        page = next(iter(data["query"]["pages"].values()))
        info = page["videoinfo"][0]
        meta = info.get("extmetadata", {})
        val = lambda k: _strip_tags(meta.get(k, {}).get("value"))
        licence = val("LicenseShortName")
        if info.get("mediatype") == "AUDIO" and is_open_licence(licence):
            mime = info["mime"] if info["mime"].startswith("audio/") else "audio/ogg"
            sources = [(d["src"].split("?")[0], "audio/mpeg") for d in info.get("derivatives", [])
                       if "mp3" in (d.get("transcodekey", "") + d.get("type", "")).lower()]
            sources.append((info["url"].split("?")[0], mime))      # without Commons' utm_ tracking parameters
            artist = (val("Artist") or "Unknown")[:90]
            credit_artist = re.sub(r"^Recording\s*:\s*", "", artist)     # computed here: Python 3.11 f-strings cannot hold a backslash
            clip = {"sources": sources, "title": filename.rsplit(".", 1)[0], "artist": artist,
                    "date": val("DateTimeOriginal") or val("DateTime"), "description": val("ImageDescription"),
                    "licence": licence, "page": info.get("descriptionurl", ""),
                    "credit": f"Recording: {credit_artist}, {licence}, via Wikimedia Commons"}
        else:
            print(f"  [audio] {filename} skipped: {info.get('mediatype')}, licence {licence!r}")
    except Exception as e:
        print(f"  [audio] lookup failed for {filename}: {e}")
    _clip_cache[filename] = clip
    return clip


def audio_tag(clip):
    import html
    srcs = "".join(f'<source src="{html.escape(u)}" type="{html.escape(m)}">' for u, m in clip["sources"])
    return f'<audio controls preload="none">{srcs}</audio>'


# Shore Leave: a different open-licence sea recording each week. Add more files here
# (any Commons audio file works; the licence is checked when the page is built, so a
# file that is not open is skipped and the next one is tried).
SHORE_SOUNDS = [
    ("On a pebble beach.ogg", "waves on a pebble beach"),
    ("Oceanwavescrushing.ogg", "ocean waves crashing"),
    ("Sea waves.wav", "sea waves"),
]


def shore_sound_html(saturday):
    """The weekly sea recording for Shore Leave, or "" if none of them resolve."""
    n = saturday.isocalendar()[1] + saturday.year * 53
    for k in range(len(SHORE_SOUNDS)):
        filename, note = SHORE_SOUNDS[(n + k) % len(SHORE_SOUNDS)]
        clip = commons_audio(filename)
        if clip:
            import html
            return (f'<figure class="xs-audio"><div class="xs-audio-label">&#9835; Listen &middot; {html.escape(note)}</div>'
                    f'{audio_tag(clip)}<figcaption>{html.escape(clip["credit"])}</figcaption></figure>')
    return ""


# ─────────────────────────────────────────────
# MOTION on the main page
# ─────────────────────────────────────────────
# All of it is decoration or a live picture; all of it goes still under
# prefers-reduced-motion, and none of it can block the page if it fails.

RADAR_URL = "https://radar.weather.gov/ridge/standard/KOKX_loop.gif"      # NWS Upton NY, covers NYC
RADAR_PAGE = "https://radar.weather.gov/station/kokx/standard"


def radar_html():
    """The live NWS radar loop for the Weather section. It is fetched when the page is opened,
    so it shows the hour before you look, not the hour the page was built."""
    return (f'<figure class="radar"><img src="{RADAR_URL}" width="600" height="550" loading="lazy" decoding="async" '
            f'referrerpolicy="no-referrer" onerror="this.closest(\'figure\').remove()" '
            f'alt="Weather radar loop for the New York City area showing the last hour of rain">'
            f'<figcaption>Live radar, the last hour &middot; National Weather Service, Upton NY &middot; public domain '
            f'&middot; <a href="{RADAR_PAGE}">full radar &rarr;</a></figcaption></figure>')


def pigeon_walk_html(pigeon_url):
    """A small pigeon that strolls along the rule under the masthead now and then."""
    return (f'<div class="pigeon-walk" aria-hidden="true"><span class="pw-path">'
            f'<img class="pw-bird" src="{pigeon_url}" alt="" width="20" height="17"></span></div>')


def waves_html():
    """A strip of slow waves for the top of Shore Leave."""
    d = "M0 22 Q37.5 9 75 22" + "".join(f" T{75 * k} 22" for k in range(2, 17)) + " V40 H0 Z"
    return (f'<div class="sl-waves" aria-hidden="true"><svg viewBox="0 0 1200 40" preserveAspectRatio="none">'
            f'<path class="sl-w2" d="{d}"/><path class="sl-w1" d="{d}"/></svg></div>')


MOTION_CSS = """
  .radar { margin: 12px 0 4px; }
  .radar img { display: block; width: 100%; height: auto; border: 1px solid #D4B98A; background: #EFE3C3; }
  .radar figcaption { font-family: 'Courier Prime', monospace; font-size: 7.5px; letter-spacing: .1em; text-transform: uppercase; color: #8A6A3A; margin-top: 6px; line-height: 1.6; }
  .radar figcaption a { color: #9B3A1A; }

  .pigeon-walk { position: absolute; left: 0; right: 0; bottom: 1px; height: 18px; overflow: hidden; pointer-events: none; z-index: 1; }
  .pw-path { position: absolute; bottom: 0; left: 0; animation: pw-stroll 70s linear infinite; }
  .pw-bird { display: block; width: 20px; height: auto; opacity: .6; transform-origin: 50% 100%; animation: pw-bob .55s ease-in-out infinite alternate; }
  @keyframes pw-stroll { 0% { transform: translateX(640px); } 45% { transform: translateX(-40px); } 100% { transform: translateX(-40px); } }
  @keyframes pw-bob { from { transform: translateY(0) rotate(-3deg); } to { transform: translateY(-1.5px) rotate(3deg); } }

  .sl-waves { height: 26px; overflow: hidden; margin: 0 0 12px; border-bottom: 1px solid #B9CBD6; }
  .sl-waves svg { display: block; width: 200%; height: 26px; animation: sl-drift 22s linear infinite; }
  .sl-w1 { fill: #3F6A86; opacity: .30; }
  .sl-w2 { fill: #3F6A86; opacity: .16; transform: translateX(-25px); }
  @keyframes sl-drift { from { transform: translateX(0); } to { transform: translateX(-50%); } }

  @media (prefers-reduced-motion: reduce) {
    .radar img { display: none; }
    .pigeon-walk { display: none; }
    .sl-waves svg { animation: none; }
  }
"""


# ─────────────────────────────────────────────
# RECORD OF THE WEEK (News & Culture, Music & Events)
# ─────────────────────────────────────────────
# One public-domain recording a week, from Wikimedia Commons: Library of Congress
# National Jukebox discs and US-government (Marine Band) recordings. US sound
# recordings are only public domain once they are old enough (those published
# through 1925, as of 2026), so a file whose name carries a year after
# RECORD_MAX_YEAR is refused here even if Commons tags it public domain; raise
# the cap by one each January 1st. The licence is also re-read from Commons when
# the page is built. The text shown is Commons' own metadata (title, performers,
# year); the one line of note is written here, where marked.

RECORD_MAX_YEAR = 1925
RECORDS = [
    # (Commons file name, optional note)
    ("Manhattan Beach march (1923 sound recording).mp3",
     "Sousa named this march for the Manhattan Beach resort in Brooklyn, where his band played."),
    ("Second Hand Rose (1921 recording).mp3", ""),
    ("Farewell Blues (1923 sound recording).mp3", ""),
    ("My isle of golden dreams (1920 recording).mp3", ""),
    ("Home again blues (1921 sound recording).mp3", ""),
    ("All By Myself (1921 sound recording).mp3", ""),
    ("Medley (Oh me! Oh my!, Two little girls in blue, Dolly) 1921 recording.mp3", ""),
    ("Why is the ocean so near the shore (Why, why, why?) 1914 sound recording.mp3", ""),
    ("At the jazz band ball (1918 sound recording).mp3", ""),
    ("John Philip Sousa Band - Southern Hospitality.mp3", ""),
]


def _record_card(filename, note, clip):
    import html
    year = re.search(r"\b(1[89]\d\d)\b", filename)
    title = re.sub(r"\s*\((?:\d{4} )?(?:sound )?recording\)|\s+\d{4} (?:sound )?recording|\.\w+$", "", filename, flags=re.I).strip()
    # Commons' credit field runs "Recording: X Composition: Y" together once its markup is stripped
    who = [w.strip(" ;·") for w in re.split(r"\s*(?=\b(?:Recording|Composition|Performer|Lyrics|Words|Music)\s*:)", clip["artist"])]
    meta = " &middot; ".join(html.escape(w) for w in who if w)[:300]
    return (f'<aside class="record"><div class="b-story-meta"><span class="b-page">Record of the Week &middot; public domain</span>'
            f'<span class="b-byline">{html.escape(year.group(1)) if year else ""}</span></div>'
            f'<h3 class="b-title">{html.escape(title)}</h3>'
            + (f'<p class="b-blurb">{meta}</p>' if meta else "")
            + (f'<p class="b-blurb record-note">{html.escape(note)}</p>' if note else "")
            + f'{audio_tag(clip)}'
            f'<p class="b-credit">{html.escape(clip["licence"])} &middot; <a href="{html.escape(clip["page"])}">via Wikimedia Commons</a></p></aside>')


def record_html(saturday):
    """This week's record as a card for the News & Culture page, or "" if none resolve."""
    n = saturday.isocalendar()[1] + saturday.year * 53
    for k in range(len(RECORDS)):
        filename, note = RECORDS[(n + k) % len(RECORDS)]
        year = re.search(r"\b(1[89]\d\d)\b", filename)
        if year and int(year.group(1)) > RECORD_MAX_YEAR:
            print(f"  [record] {filename} skipped: newer than {RECORD_MAX_YEAR}")
            continue
        clip = commons_audio(filename)
        if clip:
            return _record_card(filename, note, clip)
    return ""


RECORD_CSS = """
  .record { border: 1px solid var(--rule); border-left: 3px solid var(--accent); padding: 14px 16px 12px; margin: 0 0 20px; background: var(--panel); position: relative; z-index: 1; }
  .record .b-title { margin-bottom: 2px; }
  .record .b-blurb { margin-top: 2px; }
  .record .record-note { font-style: italic; color: var(--muted); }
  .record audio { display: block; width: 100%; margin: 10px 0 6px; }
  .record .b-credit { margin: 0; font-family: var(--mono); font-size: 7.5px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--faint); }
  .record .b-credit a { color: var(--accent); }
"""


# ─────────────────────────────────────────────
# PLACE PHOTOS (the Three Trips)
# ─────────────────────────────────────────────
# A photo of the place a trip goes to. First choice: an open-licence photo from Wikimedia
# Commons, found from the trip's Wikipedia article (photo_wiki: its lead image) or a named
# Commons file (photo_file), with the licence read from Commons at build time like the sound.
# Backup: the place's own picture (the og:image its official site publishes for sharing),
# checked to load and to be a sensible size, credited to the site and linking to it.
# Anything that fails leaves the trip without a photo.

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
_photo_cache = {}


def commons_photo(title=None, file=None, width=960):
    """{"url","page","credit","source"} for an open-licence Commons photo, or None."""
    key = (title, file)
    if key in _photo_cache:
        return _photo_cache[key]
    out = None
    try:
        if not file and title:
            data = _wiki_api(WIKIPEDIA_API, {"action": "query", "titles": title, "prop": "pageimages",
                                             "piprop": "name", "format": "json", "redirects": 1})
            file = next(iter(data["query"]["pages"].values())).get("pageimage")
        if file:
            data = _commons_json({"action": "query", "format": "json", "titles": f"File:{file}", "prop": "imageinfo",
                                  "iiprop": "url|mime|size|extmetadata", "iiurlwidth": width})
            info = next(iter(data["query"]["pages"].values()))["imageinfo"][0]
            meta = info.get("extmetadata", {})
            val = lambda k: _strip_tags(meta.get(k, {}).get("value"))
            licence = val("LicenseShortName")
            if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                print(f"  [photo] {file} skipped: {info.get('mime')}")
            elif info.get("width", 0) < 700:
                print(f"  [photo] {file} skipped: only {info.get('width')}px wide")
            elif not is_open_licence(licence):
                print(f"  [photo] {file} skipped: licence {licence!r}")
            else:
                artist = (val("Artist") or "Unknown")[:80]
                out = {"url": (info.get("thumburl") or info["url"]).split("?")[0], "page": info.get("descriptionurl", ""),
                       "credit": f"Photo: {artist}, {licence}, via Wikimedia Commons", "source": "commons", "file": file}
    except Exception as e:
        print(f"  [photo] lookup failed for {title or file}: {e}")
    _photo_cache[key] = out
    return out


def site_photo(url):
    """The picture a page publishes for sharing (og:image), vetted to load and be a sensible size, or None."""
    import dispatch_pages as dp          # late import: dispatch_pages imports this module
    from urllib.parse import urljoin, urlparse
    from bs4 import BeautifulSoup
    try:
        r = requests.get(url, headers=dp._BROWSER_UA, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for attrs in ({"property": "og:image"}, {"property": "og:image:secure_url"}, {"name": "twitter:image"}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                good = dp.vet_image(dp._img_url(urljoin(r.url, tag["content"].strip())))
                if good:
                    host = re.sub(r"^www\.", "", urlparse(url).netloc)
                    return {"url": good, "page": url, "credit": f"Image: {host}", "source": "site"}
    except Exception as e:
        print(f"  [photo] site image failed for {url}: {type(e).__name__}")
    return None


def trip_photo(trip):
    """The photo for one trip: Commons first, then the official site's own picture. None if neither works."""
    for kwargs in ({"file": trip.get("photo_file")}, {"title": trip.get("photo_wiki")}):    # a named Commons file, then the article's lead image
        if any(kwargs.values()):
            p = commons_photo(**kwargs)
            if p:
                return p
    for _name, url in (trip.get("links") or [])[:2]:
        p = site_photo(url)
        if p:
            return p
    return None
