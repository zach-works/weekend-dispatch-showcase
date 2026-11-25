# Weekend Dispatch (showcase)

A Saturday-morning newsletter for one reader. The email is a small envelope that opens into a full newsletter hosted on GitHub Pages. The newsletter covers subway changes on the reader's lines, weather and dog-walk windows with sunrise, sunset, golden hour and tides, a farmers-market go/no-go with a recipe built on what's in season, shows and culture from local venues, a rotating set of nature sections, and a few practical notes for the week. I built it with Claude Code and it runs from a GitHub Actions workflow.

**This is a showcase copy.** Personal details were swapped for a fictional reader ("Alex Sample," who lives near Fort Greene Park with a dog named Biscuit). The venues and artists are placeholders, the schedule is turned off, and no keys are included. The code is complete, but this repo isn't wired to send anything.

| The email | The newsletter it opens |
|---|---|
| ![Sample envelope email](sample/envelope_sample.png) | ![Sample Weekend Dispatch newsletter](sample/weekend_sample.png) |

## What's in it

1. **MTA this weekend.** Every current alert on the reader's lines, from the MTA's GTFS-realtime feed, filtered to the Saturday–Sunday window. A banner covers the two home stations and suggests the nearest station that's still running. Buses, the AirTrain and the LIRR show up only when they're actually disrupted.
2. **Weather, dog walks, and the sun.** Hourly forecast from Open-Meteo, with walk windows that account for pavement heat. Under the forecast: sunrise, sunset and golden hour for each day, and the day's high and low tides.
3. **Greenmarket go/no-go and a seasonal recipe.** A rule-based verdict from rain chance and sustained heat across the outing window. Below it, one real recipe each week, built on an ingredient that's in season that month, read from a public recipe collection with a link to the full recipe.
4. **Shows and culture.** Shows at tracked venues and by tracked artists, read from DICE profile pages, with a companion "long list" page for every going-out night in the next two weeks. The "Around Brooklyn" section adds culture from BAM's film and music schedules, weekend events from parks and museums (with pictures where the source allows), and a link card for a magazine's latest issue.
5. **Nature and history.** A bird, a tree and a "then and now" story from a hand-checked bank rotated by week, with sources. A Map of the Week of one neighborhood's street trees on a USGS aerial photo, and shore conditions from NOAA tides and buoys.
6. **Your block.** The trash, recycling, compost and bulk pickup days for one address, read from the city's public collection lookup. The address is set in an environment variable and is never stored in the repo. Without it, the card is left out.

## How it works

```
GitHub Actions (Saturday morning)
        |
weekend_dispatch.py
  |-- MTA GTFS-realtime alerts, Open-Meteo, NOAA tides and buoys, GrowNYC,
  |   DICE, RSS feeds, BBC Good Food, BAM, NYC Open Data, DSNY, USGS imagery, Wikimedia
  |-- Claude API: writes the weather, market and news sections
  |-- builds the newsletter and the long list -> commits them to GitHub Pages
  |-- builds the envelope: Pillow stamps the date into the postmark of an envelope photo
        |
Gmail SMTP -> inbox (envelope links to the hosted newsletter)
```

A few details worth a look:

- **Reading DICE.** DICE's browse pages only reach a few days ahead, but venue and artist profile pages embed the same JSON for a whole run of shows, months out. The watchlist reads those profile pages, so the email can name a show on the following Friday. See `fetch_dice_watchlist()`.
- **Alerts that matter.** `fetch_surface_transit_alerts()` keeps a watched service only when its alert text says it's cut, rerouted or suspended. Otherwise the service isn't mentioned at all.
- **Real recipes, not written ones.** `recipes.py` reads a recipe's own schema.org data and shows its title, timings, ingredient names and a link. It doesn't reproduce the method, which belongs to the publisher.
- **Culture that fails closed.** Each culture source is read on its own. A source that is down, or whose markup changes, is left out, and the rest of the section still runs. See `culture_listings.py`.
- **Hand-checked content.** The nature and history sections draw from a fixed bank with sources, so no model writes facts the reader might act on.
- **Preview without sending.** `python3 weekend_dispatch.py --preview` builds the newsletter locally from live data with no email and no git.

## Files

| File | What it is |
|---|---|
| `weekend_dispatch.py` | The whole dispatch: fetchers, rules, Claude calls, newsletter, envelope, sender |
| `sample_profile.py` | The fictional reader's settings (location, stations, interests, watchlist, dog's name) |
| `dispatch_pages.py` | Feeds and event readers shared by the news page and the sections |
| `dispatch_media.py` | Nature, history, shore and audio pieces |
| `mta_weekender.py` | Subway changes, the car-free trips and the subway changes page |
| `culture_listings.py` | BAM film and music, weekend events from parks and museums |
| `recipes.py` | The weekly recipe, read from a public recipe collection |
| `ohreader.py` | The magazine link card (reads only the public issue number and cover) |
| `your_block.py` | The pickup-days card, from the address in `DISPATCH_BLOCK_ADDRESS` |
| `envelope_letter.py` | The animated letter envelope |
| `build_demo.py` | A mock inbox with the envelope email and the dispatch in a browser frame |
| `excursions.json` | The library of car-free day trips |
| `render_sample.py` | Renders the envelope, newsletter and long list from fictional data with the network blocked |
| `.github/workflows/weekend_dispatch.yml` | The Saturday workflow (schedule disabled here) |
| `assets/` | Pigeon artwork, a placeholder crest and envelope, and the Jost font (SIL OFL) |
| `sample/` | The rendered samples and their screenshots |

To render the samples yourself: `python3 render_sample.py`. It needs `requests`, `beautifulsoup4`, `anthropic`, `pillow`, `numpy`, `feedparser`, `lxml` and `pypdf` installed.

## What changed for this public copy

- Home location, stations, cross streets, neighbourhood, interests, the dog's name, and the tracked venues and artists are fictional, and they live in `sample_profile.py`.
- The Crown Heights Community News feature is switched off here (`CHCO_ENABLED = False`). It's a real local publication, and its link would show the reader's neighbourhood.
- Email credentials, API keys and the reader's address are read from environment variables and were never committed.
- The personal envelope photo, wax seal and crest were replaced with neutral placeholders.
- The Map of the Week rotation no longer centres on a real home.

## About

Built by Zachary Tharpe ([LinkedIn](https://www.linkedin.com/in/zachary-tharpe)), with Claude Code.

All rights reserved. The code is shared so people can read it; no license is granted to reuse it.
