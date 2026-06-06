#!/usr/bin/env python3
"""Swiggy restaurant menu scraper.

Swiggy is behind Akamai Bot Manager, which blocks automated browsers (403 / blank
page). To get around it, this script does NOT launch its own browser. Instead it
attaches over the Chrome DevTools Protocol to a real Chrome that YOU started and
in which YOU opened the restaurant page — a genuine human session Akamai trusts.

Workflow:
    1. Start Chrome with remote debugging (quit any running Chrome first):
         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
             --remote-debugging-port=9222 --user-data-dir="$HOME/.swiggy-chrome"
    2. In that Chrome, open the restaurant menu page and let it fully load.
    3. Run:
         python scraper.py "https://www.swiggy.com/city/delhi/restaurant-name-rest12345"

The script finds the already-open Swiggy tab, scrolls it to trigger lazy images,
extracts each dish (name, price, description, image), writes menu.csv and
downloads images into a folder named after the restaurant slug. It does not close
your browser.
"""

import asyncio
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

# Used only as a header for httpx image downloads, to mirror a real Chrome.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

# Port the user started Chrome with: --remote-debugging-port=<CDP_PORT>
CDP_PORT = 9222

# Selectors tried in order. Swiggy hashes class names, so we fall back through
# several patterns and log which one yielded items.
ITEM_SELECTORS = [
    '[data-testid*="item"]',
    '[class*="MenuItem"]',
    '[class*="item-"]',
]


def restaurant_slug(url: str) -> str:
    """Extract restaurant slug from a Swiggy URL.

    e.g. .../restaurant-name-rest12345 -> 'restaurant-name'
    Falls back to the last path segment if no -rest<id> suffix is present.
    """
    path = urlparse(url).path.rstrip("/")
    last = path.split("/")[-1] if path else "restaurant"
    # strip trailing -rest<digits> (the Swiggy restaurant id)
    slug = re.sub(r"-rest\d+$", "", last)
    # keep hyphens for the folder name (e.g. kukki-da-dhaba)
    slug = re.sub(r"[^a-z0-9-]+", "-", slug.strip().lower()).strip("-")
    return slug or "restaurant"


def slugify(text: str) -> str:
    """Lowercase, replace non-alphanumerics with underscores."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


async def scroll_page(page, steps: int = 25, pause: float = 0.4) -> None:
    """Scroll down slowly in a loop to trigger lazy-loaded images."""
    prev_height = 0
    for _ in range(steps):
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(pause)
        if height == prev_height:
            # also nudge to bottom to catch any final lazy loads
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(pause)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == height:
                break
        prev_height = height


async def extract_dishes(page) -> tuple[list[dict], str | None]:
    """Try each selector; return dishes from the first that yields results."""
    for selector in ITEM_SELECTORS:
        count = await page.locator(selector).count()
        if count == 0:
            continue
        print(f"[selector] matched {count} nodes with: {selector}")
        dishes = await page.evaluate(_EXTRACT_JS, selector)
        # keep only nodes that actually look like a dish (have a name)
        dishes = [d for d in dishes if d.get("name")]
        if dishes:
            return dishes, selector
    return [], None


# Runs in the browser. For each matched node, pull name/price/description/image.
# Swiggy renders a hidden accessibility <p>/aria-label per dish of the form:
#   "Veg Item. <Name>. [This item is a Bestseller,] Costs: <N> rupees,
#    [Description: <desc>] [This item is customizable.] Swipe right to add..."
# We parse that string (the cleanest signal) and read the dish name/photo from
# the <img>. If the accessibility string is absent (selector matched a different
# layout), we fall back to loose innerText heuristics.
_EXTRACT_JS = r"""
(selector) => {
  const nodes = Array.from(document.querySelectorAll(selector));
  const priceRe = /(?:₹|Rs\.?\s?)\s?\d[\d,]*/i;
  const seen = new Set();
  const out = [];

  for (const node of nodes) {
    const text = (node.innerText || "").trim();
    if (!text) continue;

    // accessibility string: a <p> or aria-label containing "Costs:"
    let acc = "";
    for (const p of node.querySelectorAll("p")) {
      if (/Costs:/.test(p.textContent)) { acc = p.textContent.trim(); break; }
    }
    if (!acc) {
      const a = node.querySelector('[aria-label*="Costs:"]');
      if (a) acc = a.getAttribute("aria-label").trim();
    }

    // image: first <img> with a real http src; its alt is the clean dish name
    let image = "", imgAlt = "";
    for (const img of node.querySelectorAll("img")) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      if (src && src.startsWith("http")) { image = src; imgAlt = (img.alt || "").trim(); break; }
    }

    // name: img alt > parsed from acc string > first sensible innerText line
    let name = imgAlt;
    if (!name && acc) {
      const m = acc.match(/^(?:Veg Item|Non-veg item|Egg item)\.\s*(.*?)\.\s*(?:This item is a Bestseller,\s*)?Costs:/i);
      if (m) name = m[1].trim();
    }
    if (!name) {
      const lines = text.split("\n").map(s => s.trim()).filter(Boolean);
      name = lines.find(l => !/Costs:|Swipe right|^ADD$/i.test(l)) || lines[0] || "";
    }
    if (!name) continue;

    // price: prefer "Costs: <N> rupees" from acc, else a ₹/Rs match in text
    let price = "";
    const cm = (acc || text).match(/Costs:\s*([\d,]+)\s*rupees/i);
    if (cm) {
      price = "₹" + cm[1];
    } else {
      const pm = text.match(priceRe);
      if (pm) price = pm[0].replace(/\s+/g, "");
    }

    // description: the "Description: ..." span of the acc string, if present
    let description = "";
    if (acc) {
      const dm = acc.match(/Description:\s*(.*?)(?:\s*This item is customizable\.|\s*Swipe right to add)/i);
      if (dm) description = dm[1].trim();
    }

    const key = name + "|" + price;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name, price, description, image });
  }
  return out;
}
"""


def guess_ext(url: str, content_type: str) -> str:
    """Pick a file extension from the URL or content-type, default .jpg."""
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    return ".jpg"


async def download_image(
    client: httpx.AsyncClient, url: str, dest_dir: Path, base_name: str
) -> str | None:
    """Download one image. Returns the saved filename, or None on failure."""
    try:
        resp = await client.get(url, timeout=30.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! image download failed for {base_name}: {exc}")
        return None
    ext = guess_ext(url, resp.headers.get("content-type", ""))
    filename = f"{base_name}{ext}"
    (dest_dir / filename).write_bytes(resp.content)
    return filename


def find_target_page(pages, url: str):
    """Pick the already-open Swiggy tab that matches the given URL.

    Prefers an exact restaurant-id match (rest<digits>), then any open
    swiggy.com restaurant tab. Returns the matching page or None.
    """
    rest_id = None
    m = re.search(r"rest\d+", url)
    if m:
        rest_id = m.group(0)

    swiggy_pages = [pg for pg in pages if "swiggy.com" in pg.url]
    if rest_id:
        for pg in swiggy_pages:
            if rest_id in pg.url:
                return pg
    for pg in swiggy_pages:
        if "/restaurant" in pg.url or "/city/" in pg.url:
            return pg
    return swiggy_pages[0] if swiggy_pages else None


async def scrape(url: str, port: int = CDP_PORT) -> None:
    slug = restaurant_slug(url)
    out_dir = Path(slug)
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(
                f"http://localhost:{port}", timeout=10000
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[error] could not connect to Chrome on port {port}: {exc}")
            print("\nStart Chrome with remote debugging first, e.g.:")
            print('  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\')
            print(f'      --remote-debugging-port={port} '
                  '--user-data-dir="$HOME/.swiggy-chrome"')
            print("then open the restaurant page in it and re-run this script.")
            return

        # Gather every open tab across the real browser's contexts.
        pages = [pg for ctx in browser.contexts for pg in ctx.pages]
        page = find_target_page(pages, url)

        if page is None:
            # No Swiggy tab open — open one in the existing (human) context.
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            print(f"[open] no Swiggy tab found; navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
        else:
            print(f"[attach] using open tab: {page.url}")
        await page.bring_to_front()

        # slow scroll to trigger lazy images
        print("[scroll] triggering lazy-loaded images...")
        await scroll_page(page)

        # wait for network idle before extracting
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:  # noqa: BLE001
            print("[warn] networkidle timeout; extracting anyway")

        dishes, used = await extract_dishes(page)
        # Do NOT call browser.close() — that could close the user's tabs.
        # Exiting the async_playwright context just disconnects from Chrome.

    if not dishes:
        print("[done] no dishes found with any selector. "
              "Page layout may have changed or content was blocked.")
        return

    # download images + write csv
    rows = []
    with_images = 0
    used_names: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers={"User-Agent": CHROME_UA}, follow_redirects=True
    ) as client:
        for dish in dishes:
            name = dish["name"]
            base = slugify(name) or "dish"
            # de-dupe image filenames for identical slugs
            n = used_names.get(base, 0)
            used_names[base] = n + 1
            base_name = base if n == 0 else f"{base}_{n}"

            image_filename = ""
            if dish.get("image"):
                saved = await download_image(
                    client, dish["image"], images_dir, base_name
                )
                if saved:
                    image_filename = saved
                    with_images += 1

            rows.append({
                "name": name,
                "price": dish.get("price", ""),
                "description": dish.get("description", ""),
                "image_filename": image_filename,
            })
            print(f"  saved: {name}")

    csv_path = out_dir / "menu.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["name", "price", "description", "image_filename"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print("\n=== summary ===")
    print(f"selector used:   {used}")
    print(f"total dishes:    {len(rows)}")
    print(f"with images:     {with_images}")
    print(f"output folder:   {out_dir.resolve()}")


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python scraper.py "<swiggy restaurant url>" [cdp_port]')
        print(f"(connects to Chrome on port {CDP_PORT} by default)")
        sys.exit(1)
    port = int(sys.argv[2]) if len(sys.argv) > 2 else CDP_PORT
    asyncio.run(scrape(sys.argv[1], port))


if __name__ == "__main__":
    main()
