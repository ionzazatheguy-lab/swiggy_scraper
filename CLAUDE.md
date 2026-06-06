# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-file async Playwright scraper (`scraper.py`) for Swiggy restaurant menu pages. Extracts dish name/price/description/image, writes `<restaurant-slug>/menu.csv` and downloads images into `<restaurant-slug>/images/`.

## Environment

Dependencies live in a project virtualenv — **always run via `./venv/bin/python`, never global Python**. To recreate:

```bash
python3 -m venv venv
./venv/bin/pip install playwright httpx
./venv/bin/playwright install chromium
```

## Run

```bash
./venv/bin/python scraper.py "https://www.swiggy.com/city/delhi/restaurant-name-rest12345"
```

## Scraper gotchas

- `headless=False` is intentional — headless trips Swiggy bot detection. Keep it.
- Swiggy hashes/rotates CSS class names. Dish selection falls through `ITEM_SELECTORS` (`[data-testid*="item"]` → `[class*="MenuItem"]` → `[class*="item-"]`); the first selector with results wins and is logged. Add new fallbacks there if extraction breaks.
- Field extraction runs in-browser via `_EXTRACT_JS` using loose heuristics (regex price match, longest non-price line as description) so it survives hashed classes — edit that JS, not Python, to change what's pulled.
- Output dirs are named from the URL's `-rest<id>` slug. Folder slug keeps hyphens (`kukki-da-dhaba`); image filenames use underscores (`butter_chicken.jpg`).
