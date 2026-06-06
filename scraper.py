#!/usr/bin/env python3
"""Swiggy restaurant menu scraper.

Usage:
    python scraper.py "https://www.swiggy.com/city/delhi/restaurant-name-rest12345"

Scrapes each dish (name, price, description, image) from a Swiggy menu page,
writes menu.csv and downloads images into a folder named after the restaurant slug.
"""

import asyncio
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

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


# Runs in the browser. For each matched node, pull name/price/description/image
# using loose heuristics that survive hashed class names.
_EXTRACT_JS = r"""
(selector) => {
  const nodes = Array.from(document.querySelectorAll(selector));
  const priceRe = /(?:₹|Rs\.?\s?)\s?\d[\d,]*/i;
  const seen = new Set();
  const out = [];

  for (const node of nodes) {
    const text = (node.innerText || "").trim();
    if (!text) continue;

    // name: prefer an h-tag, else the first non-empty line
    let name = "";
    const h = node.querySelector("h1,h2,h3,h4,h5,h6");
    if (h && h.innerText.trim()) {
      name = h.innerText.trim();
    } else {
      const lines = text.split("\n").map(s => s.trim()).filter(Boolean);
      name = lines[0] || "";
    }
    if (!name) continue;

    // price
    let price = "";
    const pm = text.match(priceRe);
    if (pm) price = pm[0].replace(/\s+/g, "");

    // description: longest line that isn't the name and isn't a price/rating
    let description = "";
    const lines = text.split("\n").map(s => s.trim()).filter(Boolean);
    for (const line of lines) {
      if (line === name) continue;
      if (priceRe.test(line)) continue;
      if (/^[\d.]+\s*$/.test(line)) continue;       // bare numbers/ratings
      if (/ADD|customis/i.test(line)) continue;     // add buttons
      if (line.length > description.length) description = line;
    }
    if (description.length < 8) description = "";    // likely not a real desc

    // image: first <img> with a real src (skip data: placeholders)
    let image = "";
    const imgs = Array.from(node.querySelectorAll("img"));
    for (const img of imgs) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      if (src && src.startsWith("http")) { image = src; break; }
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


async def scrape(url: str) -> None:
    slug = restaurant_slug(url)
    out_dir = Path(slug)
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=CHROME_UA,
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        print(f"[load] {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # slow scroll to trigger lazy images
        print("[scroll] triggering lazy-loaded images...")
        await scroll_page(page)

        # wait for network idle before extracting
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:  # noqa: BLE001
            print("[warn] networkidle timeout; extracting anyway")

        dishes, used = await extract_dishes(page)
        await browser.close()

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
    if len(sys.argv) != 2:
        print('usage: python scraper.py "<swiggy restaurant url>"')
        sys.exit(1)
    asyncio.run(scrape(sys.argv[1]))


if __name__ == "__main__":
    main()
