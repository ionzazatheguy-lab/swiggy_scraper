# Swiggy Menu Scraper

Scrapes a Swiggy restaurant menu page — **dish name, price, description, and image** —
into a per-restaurant folder with a `menu.csv` and downloaded images.

## Why it attaches to your own Chrome

Swiggy sits behind **Akamai Bot Manager**. A normal automated browser (even Playwright
with a spoofed user-agent) gets a `403` or a blank page. So this scraper does **not**
launch its own browser. Instead it attaches over the Chrome DevTools Protocol (CDP) to a
**real Chrome that you start and drive** — a genuine human session Akamai trusts. The
script only reads the DOM of the tab you already have open; it never navigates for you
(unless no Swiggy tab is found) and never closes your browser.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install playwright httpx
./venv/bin/playwright install chromium   # only needed for the bundled fallback
```

> All commands use the project venv. Run everything via `./venv/bin/python`.

## Usage

**1. Start Chrome with remote debugging** (quit any running Chrome first, or use the
separate profile dir below so it launches a fresh instance):

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.swiggy-chrome"
```

**2. In that Chrome window, open the restaurant menu page** and let it fully load
(scroll a little if you like — the script will scroll too).

**3. Run the scraper** with the same URL:

```bash
./venv/bin/python scraper.py "https://www.swiggy.com/city/delhi/restaurant-name-rest12345"
```

Optional second argument overrides the CDP port (default `9222`):

```bash
./venv/bin/python scraper.py "<url>" 9333
```

## Output

A folder named after the restaurant slug (e.g. `pikwik-since-1991-rohini/`):

```
pikwik-since-1991-rohini/
├── menu.csv          # columns: name, price, description, image_filename
└── images/
    ├── butter_chicken.jpg
    ├── paneer_tikka.jpg
    └── ...
```

- `image_filename` is the dish name slugified (`butter_chicken.jpg`); empty when the
  dish has no image (those rows are written with `""` and no download is attempted).
- Folder name keeps hyphens (`pikwik-since-1991-rohini`); image filenames use
  underscores.

The script prints each dish as it's saved, then a summary: total dishes, how many had
images, and the output folder path.

## How extraction works

Swiggy hashes/rotates its CSS class names, so the scraper:

1. Tries selectors in order — `[data-testid*="item"]` → `[class*="MenuItem"]` →
   `[class*="item-"]` — and logs which one matched (`ITEM_SELECTORS` in `scraper.py`).
2. Reads each dish from the hidden **accessibility string** Swiggy renders per item
   (`"Veg Item. <Name>. Costs: <N> rupees, Description: <desc> ..."`) plus the dish
   `<img>` (its `alt` is the clean name, its `src` is the photo). This survives hashed
   classes. The parsing lives in `_EXTRACT_JS`; edit that JS to change what's pulled.
3. Dedupes dishes that repeat across recommended/category sections.

## Troubleshooting

- **`could not connect to Chrome on port 9222`** — Chrome isn't running with
  `--remote-debugging-port=9222`. Start it as in step 1.
- **Blank page / 403 in Chrome** — reload the page manually and interact with it once;
  Akamai clears after a genuine interaction. Then re-run the scraper.
- **0 dishes found** — Swiggy changed its markup. Add a new pattern to `ITEM_SELECTORS`
  and/or adjust `_EXTRACT_JS`.
