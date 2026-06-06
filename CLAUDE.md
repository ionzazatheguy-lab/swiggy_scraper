# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-file async Playwright scraper (`scraper.py`) for Swiggy restaurant menu pages. Extracts dish name/price/description/image, writes `<restaurant-slug>/menu.csv` and downloads images into `<restaurant-slug>/images/`. See `README.md` for the full user-facing workflow.

## Environment

Dependencies live in a project virtualenv — **always run via `./venv/bin/python`, never global Python**. To recreate:

```bash
python3 -m venv venv
./venv/bin/pip install playwright httpx
./venv/bin/playwright install chromium
```

## Run

The scraper does NOT launch its own browser — Swiggy's Akamai Bot Manager blocks automated browsers (403 / blank page). It attaches over CDP to a real Chrome the user started:

```bash
# user starts Chrome with remote debugging and opens the restaurant page
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 --user-data-dir="$HOME/.swiggy-chrome"
# then:
./venv/bin/python scraper.py "https://www.swiggy.com/city/delhi/restaurant-name-rest12345" [port]
```

## Scraper gotchas

- Attaches via `connect_over_cdp` to port `CDP_PORT` (9222). Never call `browser.close()` on that connection — it could close the user's tabs; just let the `async_playwright` context exit to disconnect.
- `find_target_page` reuses the already-open Swiggy tab and does not navigate (navigating can re-trigger the bot challenge); it only `goto`s if no Swiggy tab exists.
- Swiggy hashes/rotates CSS class names. Dish selection falls through `ITEM_SELECTORS` (`[data-testid*="item"]` → `[class*="MenuItem"]` → `[class*="item-"]`); first selector with results wins and is logged. On Swiggy the real per-dish testid is `normal-dish-item`.
- Field extraction runs in-browser via `_EXTRACT_JS`. The reliable signal is Swiggy's hidden per-dish accessibility string (`"Veg Item. <Name>. Costs: <N> rupees, Description: <desc> ..."`) plus the dish `<img>` (alt = clean name, src = photo). Edit that JS, not Python, to change what's pulled. Dishes repeat across sections, so it dedupes on name+price.
- Output dirs are named from the URL's `-rest<id>` slug. Folder slug keeps hyphens (`kukki-da-dhaba`); image filenames use underscores (`butter_chicken.jpg`).
