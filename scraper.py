"""
NorthStar Product Scraper
Usage: python scraper.py <url> [--output filename.csv]

Launches a headless browser, finds products on the page,
and exports a NorthStar worksheet-compatible CSV.
"""

import asyncio
import csv
import json
import re
import sys
import argparse
from urllib.parse import urlparse
from playwright.async_api import async_playwright


# ── CSV columns matching NorthStar FORM schema ──────────────────────────────
CSV_HEADERS = [
    "product_name",
    "product_image",
    "category",
    "madeby_name",
    "madeby_image",
    "soldby_name",
    "soldby_source_link",
    "substitute_name",
    "substitute_image",
    "themes",
    "ingredients",
    "barcode",
    "nutriscore",
    "source_url",
]


# ── Heuristics: CSS selectors to try for product containers ─────────────────
# Ordered from most specific to most general. First one that yields >= 2
# matching elements with visible text wins.
PRODUCT_CONTAINER_SELECTORS = [
    # Shopify / common e-commerce
    "[data-product-id]",
    "[data-product]",
    ".product-item",
    ".product-card",
    ".product-tile",
    ".product-grid-item",
    ".product",
    # WooCommerce
    "li.product",
    ".woocommerce-loop-product",
    # Generic
    "[class*='product']",
    "[class*='item-card']",
    "[class*='menu-item']",      # restaurants / coffee shops
    "[class*='collection-item']",
    "article",
]

# ── Wix detection ─────────────────────────────────────────────────────────────
# Wix sites use obfuscated class names and don't use container-based layouts.
# Products are identified by h3 headings, with images matched by alt text.
def is_wix_site(html: str) -> bool:
    return "wixui-rich-text__text" in html or "wixstatic.com" in html or "wix.com" in html

# Selectors to find the name within a container
NAME_SELECTORS = [
    "h1", "h2", "h3", "h4",
    ".product-title", ".product-name", ".item-title",
    "[class*='title']", "[class*='name']",
]

# Selectors to find the image within a container
IMAGE_SELECTORS = [
    "img[src]",
    "img[data-src]",
    "img[data-lazy-src]",
]

# Selectors to find description / ingredients within a container
DESCRIPTION_SELECTORS = [
    "p",
    ".description", ".product-description",
    "[class*='description']", "[class*='notes']",
    "[class*='detail']",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    """Strip excess whitespace from a string."""
    return re.sub(r"\s+", " ", (text or "").strip())


def absolute_url(base: str, path: str) -> str:
    """Make a relative URL absolute."""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    parsed = urlparse(base)
    if path.startswith("//"):
        return parsed.scheme + ":" + path
    if path.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return path


def infer_category(name: str, description: str) -> str:
    """
    Very lightweight keyword-based category inference.
    Works well for food/beverage sites. Extend as needed.
    """
    text = (name + " " + description).lower()
    rules = [
        (["espresso", "shot", "ristretto"], "Espresso"),
        (["cold brew", "cold-brew"], "Cold Brew"),
        (["drip", "pour over", "pour-over", "filter"], "Filter Coffee"),
        (["latte", "cappuccino", "flat white", "macchiato"], "Espresso Drink"),
        (["coffee", "roast", "blend", "single origin", "bean"], "Coffee"),
        (["tea", "chai", "matcha", "herbal"], "Tea"),
        (["chocolate", "cocoa", "mocha"], "Chocolate"),
        (["syrup", "sauce", "flavoring"], "Syrup"),
        (["pastry", "muffin", "scone", "croissant", "bagel", "cookie"], "Pastry"),
        (["sandwich", "wrap", "salad", "bowl"], "Food"),
        (["merch", "shirt", "hat", "cup", "mug", "tumbler", "gear"], "Merchandise"),
    ]
    for keywords, category in rules:
        if any(kw in text for kw in keywords):
            return category
    return ""


def infer_themes(name: str, description: str, category: str) -> list[str]:
    """Generate hashtag themes from product name and description."""
    text = (name + " " + description + " " + category).lower()
    theme_map = {
        "#organic": ["organic"],
        "#fairtrade": ["fair trade", "fairtrade"],
        "#singlerorigin": ["single origin"],
        "#darkroast": ["dark roast", "dark-roast"],
        "#mediumroast": ["medium roast", "medium-roast"],
        "#lightroast": ["light roast", "light-roast"],
        "#decaf": ["decaf", "decaffeinated"],
        "#coldbrew": ["cold brew", "cold-brew"],
        "#espresso": ["espresso"],
        "#seasonal": ["seasonal", "limited"],
        "#locallyroasted": ["roasted locally", "local roast"],
        "#vegan": ["vegan", "plant-based"],
        "#glutenfree": ["gluten free", "gluten-free"],
    }
    themes = []
    for tag, keywords in theme_map.items():
        if any(kw in text for kw in keywords):
            themes.append(tag)
    # Add a category-based theme if nothing found
    if not themes and category:
        themes.append("#" + category.lower().replace(" ", ""))
    return themes


# ── Core scraper ─────────────────────────────────────────────────────────────

async def extract_wix_products(page, base_url: str) -> list[dict]:
    """
    Wix-specific extraction. Products are h3 headings on the page.
    Images are matched by alt text == product name.
    Descriptions come from nearby .wixui-rich-text__text spans.
    """
    products = []

    # Grab all h3s — these are the product names on Wix product pages
    headings = await page.query_selector_all("h3")
    # Also build an image map: alt text → src
    all_imgs = await page.query_selector_all("img[src]")
    img_map = {}
    for img in all_imgs:
        alt = clean(await img.get_attribute("alt") or "")
        src = await img.get_attribute("src") or ""
        if alt and src and "logo" not in alt.lower() and not src.endswith(".svg"):
            img_map[alt.lower()] = src

    # Grab all rich text spans for description matching
    all_text_spans = await page.query_selector_all(".wixui-rich-text__text")
    all_descriptions = []
    for span in all_text_spans:
        text = clean(await span.inner_text())
        if text and len(text) > 20:
            all_descriptions.append(text)

    for heading in headings:
        name = clean(await heading.inner_text())
        if not name or len(name) < 3:
            continue

        # Match image by alt text
        image = img_map.get(name.lower(), "")
        if image:
            image = absolute_url(base_url, image)

        # Try to find a description near this heading in the DOM
        # Use evaluate to get the next sibling text content
        description = ""
        try:
            sibling_text = await heading.evaluate("""el => {
                let next = el.parentElement;
                for (let i = 0; i < 5; i++) {
                    if (!next) break;
                    next = next.nextElementSibling;
                    if (next) {
                        const text = next.innerText || next.textContent || '';
                        if (text.trim().length > 20) return text.trim();
                    }
                }
                return '';
            }""")
            description = clean(sibling_text or "")
        except Exception:
            pass

        # Fall back: find a description from the full list that likely
        # belongs to this product (contains origin, roast keywords)
        if not description:
            name_words = set(name.lower().split())
            for desc in all_descriptions:
                desc_words = set(desc.lower().split())
                if name_words & desc_words:
                    description = desc
                    break

        category = infer_category(name, description)
        themes = infer_themes(name, description, category)

        products.append({
            "product_name": name,
            "product_image": image,
            "category": category,
            "madeby_name": "",
            "madeby_image": "",
            "soldby_name": "",
            "soldby_source_link": base_url,
            "substitute_name": "",
            "substitute_image": "",
            "themes": " ".join(themes),
            "ingredients": description,
            "barcode": "",
            "nutriscore": "",
            "source_url": base_url,
        })

    if products:
        print(f"  ✓ Found {len(products)} products using Wix extractor")
    return products


async def find_products(page, base_url: str) -> list[dict]:
    """
    Try each container selector in order. Return the first set of results
    that looks like a real product list (>= 2 items with non-empty names).
    Falls back to Wix extractor, then JSON-LD.
    """
    # Detect Wix before trying generic selectors
    html = await page.content()
    if is_wix_site(html):
        print("  → Wix site detected, using Wix extractor...")
        products = await extract_wix_products(page, base_url)
        if products:
            return products

    for selector in PRODUCT_CONTAINER_SELECTORS:
        try:
            containers = await page.query_selector_all(selector)
        except Exception:
            continue

        if len(containers) < 2:
            continue

        products = []
        for container in containers:
            product = await extract_from_container(container, base_url)
            if product and product["product_name"]:
                products.append(product)

        if len(products) >= 2:
            print(f"  ✓ Found {len(products)} products using selector: {selector}")
            return products

    # Fallback: try to pull structured data from JSON-LD on the page
    print("  ! Container selectors found nothing — trying JSON-LD fallback...")

    return await extract_json_ld(page, base_url)


async def extract_from_container(container, base_url: str) -> dict | None:
    """Extract a single product's fields from its DOM container."""
    name = ""
    for sel in NAME_SELECTORS:
        el = await container.query_selector(sel)
        if el:
            name = clean(await el.inner_text())
            if name:
                break

    if not name:
        return None

    # Image
    image = ""
    for sel in IMAGE_SELECTORS:
        el = await container.query_selector(sel)
        if el:
            src = await el.get_attribute("src") or \
                  await el.get_attribute("data-src") or \
                  await el.get_attribute("data-lazy-src") or ""
            if src and not src.endswith(".svg") and "placeholder" not in src:
                image = absolute_url(base_url, src)
                break

    # Description
    description = ""
    for sel in DESCRIPTION_SELECTORS:
        el = await container.query_selector(sel)
        if el:
            text = clean(await el.inner_text())
            if text and text.lower() != name.lower() and len(text) > 10:
                description = text
                break

    category = infer_category(name, description)
    themes = infer_themes(name, description, category)

    # Try to grab a price as a faint signal (not stored, just for debugging)
    # price_el = await container.query_selector("[class*='price']")

    return {
        "product_name": name,
        "product_image": image,
        "category": category,
        "madeby_name": "",
        "madeby_image": "",
        "soldby_name": "",
        "soldby_source_link": base_url,
        "substitute_name": "",
        "substitute_image": "",
        "themes": " ".join(themes),
        "ingredients": description,
        "barcode": "",
        "nutriscore": "",
        "source_url": base_url,
    }


async def extract_json_ld(page, base_url: str) -> list[dict]:
    """
    Parse JSON-LD <script type="application/ld+json"> blocks.
    Many modern sites embed structured product data here.
    """
    scripts = await page.query_selector_all('script[type="application/ld+json"]')
    products = []

    for script in scripts:
        try:
            content = await script.inner_text()
            data = json.loads(content)

            # Can be a single object or a list
            items = data if isinstance(data, list) else [data]

            for item in items:
                item_type = item.get("@type", "")
                if item_type == "Product":
                    name = clean(item.get("name", ""))
                    description = clean(item.get("description", ""))
                    image = item.get("image", "")
                    if isinstance(image, list):
                        image = image[0] if image else ""
                    image = absolute_url(base_url, image)
                    brand = item.get("brand", {})
                    if isinstance(brand, dict):
                        brand = brand.get("name", "")

                    category = infer_category(name, description)
                    themes = infer_themes(name, description, category)

                    if name:
                        products.append({
                            "product_name": name,
                            "product_image": image,
                            "category": category,
                            "madeby_name": brand,
                            "madeby_image": "",
                            "soldby_name": "",
                            "soldby_source_link": base_url,
                            "substitute_name": "",
                            "substitute_image": "",
                            "themes": " ".join(themes),
                            "ingredients": description,
                            "barcode": item.get("gtin", ""),
                            "nutriscore": "",
                            "source_url": base_url,
                        })

                elif item_type in ("ItemList", "CollectionPage"):
                    for list_item in item.get("itemListElement", []):
                        sub = list_item.get("item", list_item)
                        name = clean(sub.get("name", ""))
                        if name:
                            description = clean(sub.get("description", ""))
                            category = infer_category(name, description)
                            themes = infer_themes(name, description, category)
                            products.append({
                                "product_name": name,
                                "product_image": absolute_url(base_url, sub.get("image", "")),
                                "category": category,
                                "madeby_name": "",
                                "madeby_image": "",
                                "soldby_name": "",
                                "soldby_source_link": base_url,
                                "substitute_name": "",
                                "substitute_image": "",
                                "themes": " ".join(themes),
                                "ingredients": description,
                                "barcode": "",
                                "nutriscore": "",
                                "source_url": base_url,
                            })
        except (json.JSONDecodeError, Exception):
            continue

    if products:
        print(f"  ✓ Found {len(products)} products via JSON-LD")
    else:
        print("  ! JSON-LD fallback also found nothing.")

    return products


def deduplicate(products: list[dict]) -> list[dict]:
    """Remove duplicate product names (case-insensitive)."""
    seen = set()
    unique = []
    for p in products:
        key = p["product_name"].lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def prompt_metadata(products: list[dict]) -> dict:
    """
    Ask the author for shared column values that apply to every product
    on this page — category, made by, and sold by.
    All three can be left blank by just pressing Enter.
    """
    print(f"\n{'─' * 40}")
    print("COLUMN DEFAULTS")
    print("These values will be applied to all products.")
    print("Press Enter to leave a field empty.")
    print(f"{'─' * 40}")

    # Show a sample of product names so the author knows what they're labelling
    print(f"\nProducts found ({len(products)} total):")
    for p in products[:5]:
        print(f"  • {p['product_name']}")
    if len(products) > 5:
        print(f"  • ... and {len(products) - 5} more")
    print()

    category = input("Category (e.g. Coffee, Tea, Pastry):       ").strip()
    madeby   = input("Made By  (e.g. Overmountain Coffee Roasters): ").strip()
    soldby   = input("Sold By  (e.g. Overmountain Coffee Roasters): ").strip()

    print()
    if category:
        print(f"  ✓ Category → '{category}'")
    if madeby:
        print(f"  ✓ Made By  → '{madeby}'")
    if soldby:
        print(f"  ✓ Sold By  → '{soldby}'")
    if not any([category, madeby, soldby]):
        print("  (No defaults set — columns will be empty)")

    return {
        "category": category,
        "madeby_name": madeby,
        "soldby_name": soldby,
    }


def apply_metadata(products: list[dict], metadata: dict) -> list[dict]:
    """
    Apply shared metadata to every product row.
    Only overwrites a field if the author provided a value —
    existing scraped values (e.g. inferred category) are preserved
    if the author left the prompt blank.
    """
    for p in products:
        if metadata["category"]:
            p["category"] = metadata["category"]
        if metadata["madeby_name"]:
            p["madeby_name"] = metadata["madeby_name"]
        if metadata["soldby_name"]:
            p["soldby_name"] = metadata["soldby_name"]
    return products


def write_csv(products: list[dict], output_path: str):
    """Write products to a NorthStar-compatible CSV file."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(products)
    print(f"\n✓ Saved {len(products)} products to: {output_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

async def scrape(url: str, output: str):
    print(f"\nNorthStar Product Scraper")
    print(f"{'─' * 40}")
    print(f"URL:    {url}")
    print(f"Output: {output}")
    print(f"{'─' * 40}\n")

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

        print(f"→ Loading page...")
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"  ! networkidle timed out ({e}), continuing with what loaded...")

        # Give JS-heavy pages a moment to finish rendering
        await page.wait_for_timeout(2000)

        print(f"→ Scanning for products...")
        products = await find_products(page, url)
        products = deduplicate(products)

        await browser.close()

    if not products:
        print("\n✗ No products found.")
        print("  Tips:")
        print("  - Try a more specific URL (e.g. /products, /shop, /collections/all, /menu)")
        print("  - Check if the site requires login or blocks bots")
        sys.exit(1)

    print(f"\n→ Extracted {len(products)} unique products")

    # Ask author for shared column values
    metadata = prompt_metadata(products)
    products = apply_metadata(products, metadata)

    # Preview first 5
    print(f"\n{'─' * 40}")
    print(f"{'PRODUCT':<35} {'CATEGORY':<20} {'MADE BY'}")
    print(f"{'─' * 40}")
    for p in products[:5]:
        name = p["product_name"][:33]
        cat  = p["category"][:18]
        made = p["madeby_name"][:18]
        print(f"{name:<35} {cat:<20} {made}")
    if len(products) > 5:
        print(f"  ... and {len(products) - 5} more")
    print(f"{'─' * 40}")

    write_csv(products, output)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape product pages and export NorthStar-compatible CSV"
    )
    parser.add_argument("url", help="URL of the product listing page to scrape")
    parser.add_argument(
        "--output", "-o",
        default="northstar_products.csv",
        help="Output CSV filename (default: northstar_products.csv)"
    )
    args = parser.parse_args()

    asyncio.run(scrape(args.url, args.output))


if __name__ == "__main__":
    main()
