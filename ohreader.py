"""
Oh Reader link card for the weekend dispatch.

Oh Reader's digital edition is for digital subscribers only, so this card never reads or copies the
magazine. It reads only the issue number from the publisher's public "Latest issue" page and links
readers to that page and to the digital-issues page. Fail closed: if the page cannot be read, the
card is left out.
"""
import html
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter)"}
LATEST_URL = "https://www.ohreader.com/upcoming-issue?tag=Latest%20Issue"
DIGITAL_URL = "https://www.ohreader.com/digital-issues"
ISSUE_RE = re.compile(r"Oh Reader Issue (\d{2,4})")


def fetch_latest_issue():
    """(issue number, cover image URL) from the publisher's public page, e.g. ('026', 'https://...').
    Either part is None when it cannot be read."""
    try:
        r = requests.get(LATEST_URL, headers=UA, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [ohreader] latest issue unavailable: {e}")
        return None, None
    m = ISSUE_RE.search(r.text)
    issue = m.group(1).zfill(3) if m else None
    cover = None
    soup = BeautifulSoup(r.text, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if img.get("alt", "").startswith("Oh Reader Issue") and src:
            cover = urljoin(LATEST_URL, src)
            break
    from culture_listings import usable_image      # the cover is only shown if it loads as an image
    return issue, usable_image(cover) or None


def build_card_html(issue, cover=None):
    """The 'From the shelf' card for the weekend page, with the cover when the page has one.
    Empty when the issue is unknown."""
    if not issue:
        return ""
    pic = (f'<img src="{html.escape(cover)}" alt="Oh Reader Issue {html.escape(issue)} cover" width="100%" '
           f'style="display:block;width:100%;max-width:220px;height:auto;margin:6px 0 8px;">') if cover else ""
    return f"""
          <div class="news-subhead">From the shelf: Oh Reader</div>
          <div class="news-item">
            <div class="news-source">Latest issue &middot; Issue {html.escape(issue)}</div>
            {pic}
            <div class="news-hed"><a href="{LATEST_URL}">Oh Reader, Issue {html.escape(issue)}</a></div>
            <div class="news-dek" style="margin-top:6px;">Printed quarterly. <a href="{DIGITAL_URL}">Read online with a digital subscription &rarr;</a></div>
          </div>"""
