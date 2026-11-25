"""
"Your block" card for the weekend dispatch: trash, recycling and compost days for one address.

The address is read from the environment (DISPATCH_BLOCK_ADDRESS), so it never lives in the repo.
Days come from DSNY's public collection lookup, the same one its website uses. The lookup returns the
days as names, so nothing is decoded here. Fail closed: if the address is unset or the lookup fails,
the card is left out.
"""
import html
import os

import requests

UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter)"}
LOOKUP_URL = "https://dsnypublic.nyc.gov/dsny/api/geocoder/DSNYCollection"
DSNY_PAGE = "https://www.nyc.gov/site/dsny/index.page"


def fetch_block_schedule(address=None):
    """Regular, recycling, organics and bulk days for the address, or None when unavailable."""
    address = address or os.environ.get("DISPATCH_BLOCK_ADDRESS", "").strip()
    if not address:
        return None
    try:
        r = requests.get(LOOKUP_URL, headers=UA, params={"address": address}, timeout=20)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        print(f"  [block] DSNY lookup unavailable: {e}")
        return None
    if d.get("Errors"):
        print(f"  [block] DSNY lookup returned errors: {d['Errors']}")
        return None
    schedule = {
        "Trash": d.get("RegularCollectionSchedule", ""),
        "Recycling": d.get("RecyclingCollectionSchedule", ""),
        "Compost": d.get("OrganicsCollectionSchedule", ""),
        "Bulk pickup": d.get("BulkPickupCollectionSchedule", ""),
    }
    if not any(schedule.values()):
        return None
    return {"formatted": d.get("FormattedAddress", ""), "days": schedule}


def build_block_html(block):
    """The 'Your block' subsection. Empty when there is no schedule."""
    if not block:
        return ""
    rows = ""
    for label, days in block["days"].items():
        shown = html.escape(days) if days else "No pickup listed"
        rows += (f"<div style='margin:0 0 4px;'><b>{html.escape(label)}:</b> {shown}</div>")
    return f"""
          <div class="news-subhead">Your block: trash, recycling and compost</div>
          <div class="news-item">
            <div class="news-source">DSNY collection schedule</div>
            <div class="news-dek" style="margin-top:4px;">{rows}</div>
            <p style="margin:8px 0 0;font-size:13px;"><a href="{DSNY_PAGE}">Check the DSNY schedule &rarr;</a> Street cleaning is not included; check the alternate-side parking signs.</p>
          </div>"""
