"""
DOM Inspector — run this first on any site that returns 0 products.
It dumps the actual class names and structure so we can tune the scraper.

Usage: python inspect.py <url>
"""

import asyncio
import sys
import re
from collections import Counter
from playwright.async_api import async_playwright


async def inspect(url: str):
    print(f"\nInspecting: {url}\n{'─' * 50}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        print("Loading page (waiting up to 40s)...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print(f"  Warning: {e}")

        # Extra wait for JS rendering
        await page.wait_for_timeout(4000)

        html = await page.content()
        print(f"Page HTML size: {len(html):,} bytes\n")

        if len(html) < 500:
            print("ERROR: Page returned almost no content.")
            print("The site may be blocking headless browsers.")
            await browser.close()
            return

        # ── 1. Most common class names ───────────────────────────────────────
        all_classes = []
        for match in re.findall(r'class="([^"]+)"', html):
            all_classes.extend(match.split())
        top_classes = Counter(all_classes).most_common(40)

        print("TOP CLASS NAMES (most frequent first):")
        for cls, count in top_classes:
            print(f"  {count:3}x  .{cls}")

        # ── 2. Check specific selectors ──────────────────────────────────────
        print("\nSELECTOR PROBE:")
        selectors_to_try = [
            "li", "article", "div[class]",
            "[class*='product']", "[class*='item']", "[class*='card']",
            "[class*='grid']", "[class*='collection']", "[class*='catalog']",
            "[class*='tile']", "[class*='thumb']", "[class*='entry']",
            "ul li", ".grid li", ".collection li",
        ]
        for sel in selectors_to_try:
            try:
                els = await page.query_selector_all(sel)
                if len(els) >= 2:
                    first = els[0]
                    cls = await first.get_attribute("class") or ""
                    tag = await first.evaluate("el => el.tagName.toLowerCase()")
                    text = (await first.inner_text())[:60].replace("\n", " ").strip()
                    print(f"  {sel:<35} → {len(els):3} els | <{tag} class='{cls[:50]}'> | '{text}'")
            except Exception:
                pass

        # ── 3. H tags (product names often live here) ────────────────────────
        print("\nHEADINGS ON PAGE:")
        for tag in ["h1", "h2", "h3", "h4"]:
            els = await page.query_selector_all(tag)
            if els:
                texts = []
                for el in els[:6]:
                    t = (await el.inner_text()).strip().replace("\n", " ")
                    if t:
                        texts.append(t[:50])
                print(f"  {tag}: {len(els)} total | first few: {texts}")

        # ── 4. Images ────────────────────────────────────────────────────────
        imgs = await page.query_selector_all("img[src]")
        print(f"\nIMAGES: {len(imgs)} total")
        for img in imgs[:4]:
            src = await img.get_attribute("src") or ""
            alt = await img.get_attribute("alt") or ""
            cls = await img.get_attribute("class") or ""
            print(f"  src='{src[:70]}' alt='{alt[:30]}' class='{cls[:30]}'")

        # ── 5. JSON-LD ───────────────────────────────────────────────────────
        scripts = await page.query_selector_all('script[type="application/ld+json"]')
        print(f"\nJSON-LD BLOCKS: {len(scripts)}")
        for s in scripts[:3]:
            txt = (await s.inner_text())[:200]
            print(f"  {txt}")

        await browser.close()
        print(f"\n{'─' * 50}")
        print("Copy the class names above and share them — we'll update the scraper selectors.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python inspect.py <url>")
        sys.exit(1)
    asyncio.run(inspect(sys.argv[1]))
