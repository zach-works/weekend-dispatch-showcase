"""
Real recipes for the Greenmarket section.

Each week one main ingredient is chosen from the Greenmarket's in-season list for the month
(weekend_dispatch._SEASONAL_PRODUCE). Its BBC Good Food collection page lists recipes; one of
them is read from its own page through the schema.org "Recipe" data the page publishes, and
the email shows the title, timings, the ingredient names, and a link to the original recipe.

Nothing here is written by a model or typed in by hand. Method steps are not reproduced
(they belong to the publisher); the email links to the full recipe instead.

Fail closed: if the collection or recipe pages cannot be read, the subsection is left out.
"""
import datetime
import html
import json
import re

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "WeekendDispatch/1.0 (personal newsletter)"}
SOURCE_NAME = "BBC Good Food"
COLLECTION_BASE = "https://www.bbcgoodfood.com/recipes/collection/"
RECIPE_BASE = "https://www.bbcgoodfood.com/recipes/"
MAX_RECIPE_TRIES = 4

# Produce keyword (as it appears in the Greenmarket calendar, lower case) -> BBC Good Food collection slug.
# Each slug was checked to return a live collection page. Keys with no collection are not used.
PRODUCE_COLLECTIONS = {
    "tomato": "tomato-recipes",
    "kale": "kale-recipes",
    "beet": "beetroot-recipes",
    "winter squash": "butternut-squash-recipes",
    "apple": "apple-recipes",
    "leek": "leek-recipes",
    "pear": "pear-recipes",
    "asparagus": "asparagus-recipes",
    "rhubarb": "rhubarb-recipes",
    "zucchini": "courgette-recipes",
    "eggplant": "aubergine-recipes",
    "aubergine": "aubergine-recipes",
    "corn": "sweetcorn-recipes",
    "blackberr": "blackberry-recipes",
    "plum": "plum-recipes",
    "peach": "peach-recipes",
    "cucumber": "cucumber-recipes",
    "carrot": "carrot-recipes",
    "parsnip": "parsnip-recipes",
    "celeriac": "celeriac-recipes",
    "fennel": "fennel-recipes",
    "cauliflower": "cauliflower-recipes",
    "broccoli": "broccoli-recipes",
    "spinach": "spinach-recipes",
    "radish": "radish-recipes",
    "snap pea": "pea-recipes",
    "spring onion": "spring-onion-recipes",
    "garlic": "garlic-recipes",
    "potato": "potato-recipes",
    "turnip": "turnip-recipes",
    "lettuce": "lettuce-recipes",
    "blueberr": "blueberry-recipes",
    "sour cherr": "cherry-recipes",
    "kohlrabi": "kohlrabi-recipes",
}
# Collections are checked against the calendar with substring matching, so "beet" matches "beets" and
# "corn" matches "corn (early)". "eggplant" and "aubergine" both point at the same collection; the
# calendar only ever uses "eggplant", so the second key is a harmless spare.

_NON_RECIPE_SLUGS = {"category", "collection", "collections"}


def in_season_keywords(seasonal_items):
    """Collection keys whose produce is in season this month, in a stable order."""
    text = " | ".join(seasonal_items).lower()
    return sorted(kw for kw in PRODUCE_COLLECTIONS if kw in text)


def collection_recipe_urls(slug):
    """Recipe page URLs linked from a BBC Good Food collection page, in page order, without duplicates."""
    r = requests.get(COLLECTION_BASE + slug, headers=UA, timeout=20)
    r.raise_for_status()
    seen, urls = set(), []
    for m in re.finditer(r"https://www\.bbcgoodfood\.com/recipes/([a-z0-9-]+)", r.text):
        part = m.group(1)
        if part in _NON_RECIPE_SLUGS or part in seen or part in PRODUCE_COLLECTIONS.values():
            continue
        seen.add(part)
        urls.append(RECIPE_BASE + part)
    return urls


def _duration(iso):
    """'PT1H30M' -> '1 hr 30 min'. Returns '' when it cannot be read."""
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", iso or "")
    if not m or not (m.group(1) or m.group(2)):
        return ""
    hrs, mins = int(m.group(1) or 0), int(m.group(2) or 0)
    parts = []
    if hrs:
        parts.append(f"{hrs} hr")
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts)


def _find_recipe_data(soup):
    """The first schema.org Recipe object on the page, or None."""
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                t = o.get("@type")
                if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                    return o
                stack.extend(o.values())
    return None


def fetch_recipe(url):
    """Name, ingredient names, timings and yield from a recipe page's schema.org data. None when unusable."""
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    data = _find_recipe_data(BeautifulSoup(r.text, "html.parser"))
    if not data:
        return None
    name = " ".join(str(data.get("name", "")).split())
    ingredients = [" ".join(str(i).split()) for i in data.get("recipeIngredient", []) if str(i).strip()]
    if not name or len(ingredients) < 3:
        return None
    total = _duration(data.get("totalTime") or "") or _duration(data.get("cookTime") or "")
    yld = data.get("recipeYield")
    if isinstance(yld, list):
        yld = yld[0] if yld else ""
    return {"name": name, "url": url, "ingredients": ingredients,
            "time": total, "serves": " ".join(str(yld or "").split())}


def build_recipe_html(date, seasonal_items):
    """The 'A recipe from what's in season' subsection for the Greenmarket section, or '' when no real
    recipe can be read this week. The main ingredient rotates weekly across the in-season list."""
    keys = in_season_keywords(seasonal_items)
    if not keys:
        return ""
    week = date.isocalendar()[1]
    tries = 0
    for i in range(len(keys)):
        kw = keys[(week + i) % len(keys)]
        slug = PRODUCE_COLLECTIONS[kw]
        try:
            urls = collection_recipe_urls(slug)
        except Exception as e:
            print(f"  [recipe] {slug} unavailable: {e}")
            continue
        if not urls:
            continue
        start = week % len(urls)
        for url in urls[start:] + urls[:start]:
            if tries >= MAX_RECIPE_TRIES:
                return ""
            tries += 1
            try:
                rec = fetch_recipe(url)
            except Exception as e:
                print(f"  [recipe] {url} unavailable: {e}")
                continue
            if rec:
                return _render(rec, kw, date)
    return ""


def _render(rec, kw, date):
    e = html.escape
    facts = " &middot; ".join(x for x in (rec["time"], rec["serves"]) if x)
    facts = (facts + " &middot; ") if facts else ""
    ing = " &middot; ".join(e(i) for i in rec["ingredients"])
    return f"""
          <div class="news-subhead">A recipe from what&rsquo;s in season</div>
          <div class="news-item">
            <div class="news-source">{facts}{e(kw)} in season at the market in {date.strftime('%B')}</div>
            <div class="news-hed"><a href="{e(rec['url'])}">{e(rec['name'])}</a></div>
            <div class="news-dek" style="margin-top:6px;"><b>Ingredients:</b> {ing}</div>
            <div class="news-dek" style="margin-top:6px;">From {SOURCE_NAME}. <a href="{e(rec['url'])}">Read the full recipe &rarr;</a></div>
          </div>"""
