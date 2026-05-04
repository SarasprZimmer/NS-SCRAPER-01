"""
NorthStar Product Scraper
Usage: python scraper.py <url> [--output filename.csv]

Two-stage pipeline:
  Stage 1 — Detector: analyses the page DOM, finds the repeating
             pattern that represents products, builds a Blueprint.
  Stage 2 — Extractor: uses the Blueprint to pull every product
             and export a NorthStar-compatible CSV.

Works on Wix, Shopify, WooCommerce, Squarespace, and generic
custom sites without any platform-specific hardcoding.
"""

import asyncio
import csv
import json
import re
import sys
import argparse
from dataclasses import dataclass, field
from urllib.parse import urlparse
from playwright.async_api import async_playwright


# ── CSV schema ───────────────────────────────────────────────────────────────
CSV_HEADERS = [
    "product_name",
    "product_image",
    "price",
    "category",
    "madeby_name",
    "madeby_image",
    "soldby_name",
    "soldby_image",
    "soldby_link",
]


# ── Blueprint ─────────────────────────────────────────────────────────────────
@dataclass
class Blueprint:
    """
    Describes how products are structured on a specific page.
    Produced by the Detector, consumed by the Extractor.
    """
    strategy: str
    container_sel: str = ""
    name_sel: str = ""
    image_sel: str = ""
    desc_sel: str = ""
    count: int = 0
    platform: str = ""
    notes: list = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def absolute_url(base: str, path: str) -> str:
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

async def best_image_src(img_el) -> str:
    """
    Extract the best available image URL from an <img> element.
    Handles lazy-loading (data-src, data-lazy-src, data-original …),
    srcset (picks the highest-width candidate), and plain src.
    Returns "" when nothing usable is found.
    """
    BAD = (".svg", "placeholder", "blank.gif", "data:image", "spacer")

    def _ok(url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        return not any(b in url_lower for b in BAD)

    def _best_srcset(raw: str) -> str:
        """Pick the widest URL from a srcset string."""
        best_w, best_url = -1, ""
        for part in raw.split(","):
            tokens = part.strip().split()
            if not tokens:
                continue
            url = tokens[0]
            if not _ok(url):
                continue
            w = 0
            if len(tokens) > 1:
                desc = tokens[1]
                if desc.endswith("w"):
                    try:
                        w = int(desc[:-1])
                    except ValueError:
                        pass
            if w > best_w or best_url == "":
                best_w, best_url = w, url
        return best_url

    # 1. srcset / data-srcset → best resolution
    for attr in ("srcset", "data-srcset"):
        raw = await img_el.get_attribute(attr) or ""
        url = _best_srcset(raw)
        if url:
            return url

    # 2. Lazy-load data-* attributes (most common CMS patterns)
    for attr in ("data-src", "data-lazy-src", "data-original",
                 "data-lazy", "data-img", "data-url"):
        val = (await img_el.get_attribute(attr) or "").strip()
        val = val.split(",")[0].split(" ")[0]   # strip any accidental srcset
        if _ok(val):
            return val

    # 3. Plain src
    val = (await img_el.get_attribute("src") or "").strip()
    if _ok(val):
        return val

    return ""


def infer_category(name: str, description: str) -> str:
    text = (name + " " + description).lower()
    rules = [
        (["espresso", "shot", "ristretto"],                    "Espresso"),
        (["cold brew", "cold-brew"],                            "Cold Brew"),
        (["drip", "pour over", "pour-over", "filter"],         "Filter Coffee"),
        (["latte", "cappuccino", "flat white", "macchiato"],   "Espresso Drink"),
        (["coffee", "roast", "blend", "single origin", "bean"], "Coffee"),
        (["tea", "chai", "matcha", "herbal"],                   "Tea"),
        (["chocolate", "cocoa", "mocha"],                       "Chocolate"),
        (["syrup", "sauce", "flavoring"],                       "Syrup"),
        (["pastry", "muffin", "scone", "croissant", "bagel", "cookie"], "Pastry"),
        (["sandwich", "wrap", "salad", "bowl"],                 "Food"),
        (["beer", "ale", "lager", "stout", "ipa"],              "Beer"),
        (["wine", "merlot", "cabernet", "chardonnay"],          "Wine"),
        (["spirit", "whiskey", "vodka", "gin", "rum"],          "Spirits"),
        (["candle", "soap", "lotion", "skincare"],              "Beauty"),
        (["shirt", "hat", "mug", "tumbler", "gear", "merch"],  "Merchandise"),
    ]
    for keywords, category in rules:
        if any(kw in text for kw in keywords):
            return category
    return ""


def make_product(name, image, base_url, description="", madeby="", soldby="", soldby_link="", price="") -> dict:
    category = infer_category(name, description)
    return {
        "product_name":  name,
        "product_image": absolute_url(base_url, image),
        "price":         price,
        "category":      category,
        "madeby_name":   madeby,
        "madeby_image":  "",
        "soldby_name":   soldby,
        "soldby_image":  "",
        "soldby_link":   soldby_link,
    }


# Button / badge text that sometimes gets prepended to product names on card hover overlays.
_STRIP_PREFIXES = [
    "quick view ", "quick shop ", "view product ", "view item ",
    "add to cart ", "buy now ", "shop now ",
]

# Terms that are definitely NOT product names — nav links, UI buttons, page labels.
_NON_PRODUCT_NAMES = {
    # Navigation
    "home","about","about us","contact","contact us","menu","shop","store",
    "all products","our products","all items","collections","browse",
    "testimonials","reviews","blog","news","faq","press","careers",
    "privacy policy","terms","terms of service","sitemap","accessibility",
    # Account / cart UI
    "my account","log in","login","sign in","sign up","register",
    "cart","checkout","wish list","wishlist",
    # Generic page labels
    "search","newsletter","subscribe","follow us","social media",
    "back to top","load more","show more","see more","read more",
    "learn more","view more","explore","discover","get started",
    # Product card UI buttons (not names)
    "quick view","quick shop","view details","add to cart","buy now",
    "shop now","order now","select options","choose options","view product",
    "view item","see details","more info","out of stock","sold out",
    # Single-word generic
    "new","sale","featured","popular","trending","best seller",
}

# Regex that matches a price appended/prepended to a product name, e.g.:
#   "Ethiopia Natural $18.00"  →  price "$18.00",  name "Ethiopia Natural"
#   "$12.50 Colombia Washed"   →  price "$12.50",  name "Colombia Washed"
#   "Decaf Blend £9.99"        →  price "£9.99",   name "Decaf Blend"
_PRICE_RE = re.compile(
    r'(?:^|\s)([£$€¥₹]\s?\d[\d,]*(?:\.\d{1,2})?'   # leading symbol: $12.00
    r'|\d[\d,]*(?:\.\d{1,2})?\s?[£$€¥₹]'             # trailing symbol: 12.00$
    r'|\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|CAD|AUD))',  # ISO code: 12.00 USD
    re.IGNORECASE
)

def filter_products(products: list) -> list:
    """Remove navigation items / UI buttons; strip button-text prefixes from names."""
    out = []
    for p in products:
        name = p["product_name"].strip()

        # Strip leading button/badge text (e.g. "Quick View Peru Organic" → "Peru Organic")
        lower = name.lower()
        for prefix in _STRIP_PREFIXES:
            if lower.startswith(prefix):
                name  = name[len(prefix):].strip()
                lower = name.lower()
                break  # only strip one prefix

        # If no price was scraped separately, try to extract it from the name
        p = dict(p)
        if not p.get("price"):
            m = _PRICE_RE.search(name)
            if m:
                p["price"] = m.group(0).strip()
                name = (name[:m.start()] + name[m.end():]).strip()

        p["product_name"] = name

        name_lower = name.lower()
        # Exact match against blocked list
        if name_lower in _NON_PRODUCT_NAMES:
            continue
        # Very short names are rarely real products (single letters / symbols)
        if len(name) < 3:
            continue
        # Names that are ALL uppercase short strings tend to be nav labels
        if name.isupper() and len(name) < 6:
            continue
        out.append(p)
    return out

def deduplicate(products: list) -> list:
    seen, unique = set(), []
    for p in products:
        key = p["product_name"].lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


# =============================================================================
# PAGINATION
# =============================================================================

NEXT_LINK_TEXTS = {
    "next", "next page", "›", "»", "→", ">", "load more", "show more",
    "next »", "next ›", "next →", "older posts", "more products",
}

async def find_next_page(page, current_url: str) -> str:
    """
    Look for a Next page link in the DOM and return its absolute URL.
    Returns "" if no next page is found.

    Checks three things in order:
    1. <a> tags whose visible text matches known next-page labels
    2. <a rel="next">  (standard HTML pagination rel attribute)
    3. <link rel="next"> in <head>  (SEO pagination hint)
    """
    # Strategy 1: visible link text
    anchors = await page.query_selector_all("a[href]")
    for anchor in anchors:
        text = clean(await anchor.inner_text()).lower()
        href = await anchor.get_attribute("href") or ""
        aria = (await anchor.get_attribute("aria-label") or "").lower()
        if (text in NEXT_LINK_TEXTS or aria in NEXT_LINK_TEXTS):
            if href and not href.startswith("#"):
                return absolute_url(current_url, href)

    # Strategy 2: rel="next" on an <a> tag
    rel_next = await page.query_selector("a[rel='next']")
    if rel_next:
        href = await rel_next.get_attribute("href") or ""
        if href and not href.startswith("#"):
            return absolute_url(current_url, href)

    # Strategy 3: <link rel="next"> in <head>
    link_next = await page.query_selector("link[rel='next']")
    if link_next:
        href = await link_next.get_attribute("href") or ""
        if href:
            return absolute_url(current_url, href)

    return ""


async def find_product_link(container_el, base_url: str) -> str:
    """
    Extract the product detail page URL from a product card element.
    Tries (in order):
      1. Container itself is an <a>
      2. Title/heading element has an <a> ancestor within the card
      3. Image is wrapped in an <a>
      4. First non-button <a> found anywhere in the card
    Returns an absolute URL, or "" if nothing found.
    """
    raw = await container_el.evaluate("""el => {
        const SKIP_TEXT  = ['add to cart','buy now','shop now','select options',
                            'view cart','checkout','more info','read more'];
        const SKIP_CLASS = ['add_to_cart','cart-button','buy-button','checkout'];

        function ok(a) {
            const href = (a.getAttribute('href') || '').trim();
            if (!href || href === '#' || href.startsWith('javascript:')) return false;
            // Skip obvious social / mailto / tel links
            if (/^(mailto:|tel:|#|https?:\/\/(www\.)?(facebook|twitter|instagram|tiktok|linkedin))/i.test(href)) return false;
            const txt = (a.textContent || '').toLowerCase().trim();
            if (SKIP_TEXT.includes(txt)) return false;
            const cls = (a.getAttribute('class') || '').toLowerCase();
            if (SKIP_CLASS.some(s => cls.includes(s))) return false;
            return true;
        }

        // 1. Container is itself a link
        if (el.tagName === 'A' && ok(el)) return el.getAttribute('href');

        // 2. Title/heading has an <a> ancestor inside the card
        const heading = el.querySelector('h1,h2,h3,h4,.product-title,.product-name,[class*="title"],[class*="name"]');
        if (heading) {
            // heading wrapped in <a>
            let node = heading.parentElement;
            while (node && node !== el) {
                if (node.tagName === 'A' && ok(node)) return node.getAttribute('href');
                node = node.parentElement;
            }
            // <a> inside the heading
            const aIn = heading.querySelector('a');
            if (aIn && ok(aIn)) return aIn.getAttribute('href');
        }

        // 3. Image is wrapped in an <a>
        const img = el.querySelector('img');
        if (img) {
            let node = img.parentElement;
            while (node && node !== el) {
                if (node.tagName === 'A' && ok(node)) return node.getAttribute('href');
                node = node.parentElement;
            }
        }

        // 4. First qualifying <a> anywhere in the card
        for (const a of el.querySelectorAll('a')) {
            if (ok(a)) return a.getAttribute('href');
        }

        return '';
    }""")
    if raw:
        return absolute_url(base_url, raw)
    return ""


async def find_heading_link(heading_el, base_url: str) -> str:
    """
    Find the product link from a standalone heading element (heading strategy).
    Walks up ancestors looking for an <a>, then checks sibling/parent containers.
    """
    raw = await heading_el.evaluate("""el => {
        function ok(a) {
            const href = (a.getAttribute('href') || '').trim();
            if (!href || href === '#' || href.startsWith('javascript:')) return false;
            if (/^(mailto:|tel:|https?:\/\/(www\.)?(facebook|twitter|instagram|tiktok|linkedin))/i.test(href)) return false;
            return true;
        }
        // Walk up from heading to find <a> ancestor (heading inside a link)
        let node = el.parentElement;
        for (let i = 0; i < 8; i++) {
            if (!node || node === document.body) break;
            if (node.tagName === 'A' && ok(node)) return node.getAttribute('href');
            node = node.parentElement;
        }
        // <a> inside the heading text
        const aIn = el.querySelector('a');
        if (aIn && ok(aIn)) return aIn.getAttribute('href');
        // Walk up and look for any <a> sibling in the same product block
        node = el.parentElement;
        if (node) {
            for (const sib of node.children) {
                if (sib.tagName === 'A' && ok(sib)) return sib.getAttribute('href');
                const a = sib.querySelector('a');
                if (a && ok(a)) return a.getAttribute('href');
            }
        }
        return '';
    }""")
    if raw:
        return absolute_url(base_url, raw)
    return ""


# =============================================================================
# STAGE 1 - DETECTOR
# =============================================================================

async def detect(page, url: str) -> Blueprint:
    html     = await page.content()
    platform = detect_platform(html, url)
    print(f"  -> Platform hint: {platform}")

    # ── Wix category-page guard ───────────────────────────────────────────────
    # On Wix stores, /all-products and similar pages sometimes show category
    # tiles (Single Origin, Blends, Decaf …) that share the data-hook but have
    # NO product images.  If we detect this layout, bail out immediately — the
    # weaker fallback strategies (heading-grid, list-items) would just scrape the
    # same tiles and call them products.
    if platform == "wix":
        wix_items = await page.query_selector_all("[data-hook='product-item']")
        if len(wix_items) >= 2:
            imgs_found = 0
            for el in wix_items[:8]:
                if await el.query_selector("img"):
                    imgs_found += 1
            if imgs_found == 0:
                print("  ! Wix category-listing page detected (no product images).")
                print("    Tip: try a sub-category URL (e.g. /single-origin, /blends)")
                return Blueprint(strategy="none", platform=platform,
                                 notes=["Wix category page — no product images on tiles"])

    strategies = [
        detect_jsonld,
        detect_data_attributes,
        detect_repeating_grid,
        detect_heading_grid,
        detect_list_items,
    ]

    for strategy_fn in strategies:
        bp = await strategy_fn(page, url, platform)
        if bp and bp.count >= 2:
            bp.platform = platform
            print(f"  + Strategy '{bp.strategy}' found {bp.count} products")
            for note in bp.notes:
                print(f"    {note}")
            return bp

    return Blueprint(strategy="none", platform=platform)



def detect_platform(html: str, url: str) -> str:
    if "wixstatic.com" in html or "wixui-rich-text" in html:
        return "wix"
    if "cdn.shopify.com" in html or "shopify.com" in html:
        return "shopify"
    if "woocommerce" in html or "wp-content" in html:
        return "woocommerce"
    if "squarespace.com" in html or "static1.squarespace" in html:
        return "squarespace"
    if "webflow.io" in html or "webflow.com" in html:
        return "webflow"
    return "generic"


async def detect_jsonld(page, url: str, platform: str) -> Blueprint:
    scripts = await page.query_selector_all('script[type="application/ld+json"]')
    count = 0
    for script in scripts:
        try:
            data  = json.loads(await script.inner_text())
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type", "")
                if t == "Product":
                    count += 1
                elif t in ("ItemList", "CollectionPage"):
                    count += len(item.get("itemListElement", []))
        except Exception:
            pass
    if count >= 2:
        return Blueprint(
            strategy="jsonld",
            count=count,
            notes=[f"Found {count} items in JSON-LD structured data"]
        )
    return None


async def detect_data_attributes(page, url: str, platform: str) -> Blueprint:
    candidates = [
        # Wix Stores — use the dedicated data-hook name element; do NOT fall back
        # to h3/.title because category nav tiles share the same product-item
        # hook but only have heading text, no product images.
        ("[data-hook='product-item']",  "[data-hook='product-item-name']", "img", "p"),
        # Generic data-attribute patterns
        ("[data-product-id]", "h2, h3, .product-title, .title", "img", "p, .description"),
        ("[data-product]",    "h2, h3, .title",                 "img", "p"),
        ("[data-item-id]",    "h2, h3",                         "img", "p"),
    ]
    for container_sel, name_sel, image_sel, desc_sel in candidates:
        els = await page.query_selector_all(container_sel)
        if len(els) < 2:
            continue
        sample = els[:8]
        names_found = 0
        imgs_found  = 0
        for el in sample:
            n = await el.query_selector(name_sel)
            if n and clean(await n.inner_text()):
                names_found += 1
            if await el.query_selector("img"):
                imgs_found += 1
        # Require ≥2 names AND that most sampled containers have images.
        # This prevents matching Wix category-navigation tiles that share
        # the same data-hook as product cards but have no product images.
        min_imgs = max(2, len(sample) // 2)
        if names_found >= 2 and imgs_found >= min_imgs:
            return Blueprint(
                strategy="container",
                container_sel=container_sel,
                name_sel=name_sel,
                image_sel=image_sel,
                desc_sel=desc_sel,
                count=len(els),
                notes=[f"Data-attribute container: {container_sel}"]
            )
    return None


async def detect_repeating_grid(page, url: str, platform: str) -> Blueprint:
    """
    Core adaptive algorithm. Finds the element whose parent has the most
    same-tag same-class siblings that each contain text + an image.
    """
    result = await page.evaluate("""() => {
        const MIN_ITEMS = 3;
        let best = null;
        let bestScore = 0;
        const seen = new Set();

        // Strip dynamic/unique ID tokens like "elementor-element-62861a7c"
        // so Elementor, Webflow, and similar builders don't break sibling matching.
        // Use getAttribute('class') — always a plain string, even on SVG elements
        // where el.className is an SVGAnimatedString (not splittable).
        function stableClass(el) {
            // Strip any token that ENDS with a hex-segment suffix like:
            //   elementor-element-62861a7c  (multi-word prefix + hex)
            //   post-11060                 (single-word prefix + numeric hex)
            //   elementor-11430
            // Keeps: e-loop-item, type-product, e-flex, status-publish, etc.
            return (el.getAttribute('class') || '').split(/\\s+/)
                .filter(t => t && !/-[a-f0-9]{4,}$/i.test(t))
                .join(' ');
        }

        for (const el of document.querySelectorAll('*')) {
            const parent = el.parentElement;
            if (!parent) continue;
            const key = el.tagName + '|' + stableClass(el);
            if (seen.has(key)) continue;
            seen.add(key);

            const siblings = Array.from(parent.children).filter(
                c => c.tagName === el.tagName && stableClass(c) === stableClass(el)
            );
            if (siblings.length < MIN_ITEMS) continue;

            let productLike = 0;
            for (const sib of siblings) {
                const text   = (sib.innerText || '').trim();
                const hasImg = sib.querySelector('img') !== null;
                const hasText = text.length >= 2 && text.length <= 500;
                if (hasText && hasImg) productLike++;
            }

            if (productLike > bestScore && productLike >= MIN_ITEMS) {
                bestScore = productLike;
                // Pick a stable, meaningful class (skip generic Elementor tokens
                // and anything that looks like a unique hex ID)
                const SKIP = new Set(['elementor-element','elementor-widget',
                                      'elementor-widget-container','e-con','e-flex',
                                      'e-parent','e-child','e-con-full','e-con-inner',
                                      'e-con-boxed']);
                const stableTokens = (el.getAttribute('class') || '').split(/\s+/)
                    .filter(t => t && !/-[a-f0-9]{4,}$/i.test(t) && !SKIP.has(t));
                let sel = el.tagName.toLowerCase();
                if (stableTokens.length) sel += '.' + CSS.escape(stableTokens[0]);
                best = { sel, count: siblings.length, productLike };
            }
        }
        return best;
    }""")

    if not result or result["productLike"] < 2:
        return None

    container_sel = result["sel"]
    name_sel  = await probe_name_selector(page, container_sel)
    image_sel = "img"
    desc_sel  = await probe_desc_selector(page, container_sel, name_sel)

    if not name_sel:
        return None

    return Blueprint(
        strategy="container",
        container_sel=container_sel,
        name_sel=name_sel,
        image_sel=image_sel,
        desc_sel=desc_sel,
        count=result["count"],
        notes=[
            f"Repeating grid: {container_sel} ({result['count']} items, {result['productLike']} product-like)",
            f"Name: {name_sel} | Desc: {desc_sel or 'none'}",
        ]
    )


async def detect_heading_grid(page, url: str, platform: str) -> Blueprint:
    """
    For flat-layout pages (Wix, some Squarespace) where products are
    just headings with matching images by alt text — no container wrapper.
    """
    # Common navigation / footer section labels to exclude.
    # A heading list dominated by these is a nav section, not a product grid.
    NAV_LABELS = {
        "shop", "navigate", "navigation", "menu", "my account", "account",
        "newsletter", "subscribe", "about", "about us", "contact", "home",
        "follow us", "follow", "links", "pages", "sitemap",
    }

    for tag in ["h2", "h3", "h4"]:   # h2 first — products are almost always h2
        headings = await page.query_selector_all(tag)
        if len(headings) < 2:
            continue

        names = []
        for h in headings:
            text = clean(await h.inner_text())
            if text and 2 < len(text) < 80 and text.lower() not in NAV_LABELS:
                names.append(text)

        if len(names) < 2:
            continue

        imgs = await page.query_selector_all("img[alt]")
        alts = set()
        for img in imgs:
            alt = clean(await img.get_attribute("alt") or "")
            if alt:
                alts.add(alt.lower())

        matched = sum(1 for n in names if n.lower() in alts)

        # Always require ≥2 headings whose text actually matches an image alt-text.
        # Removed the old Wix/ecommerce platform shortcut ("any 4+ headings on a
        # known platform") — that shortcut caused category nav tiles (Single Origin,
        # Blends, Decaf…) to be accepted as products because they're headings on a
        # Wix page but have zero associated product images.
        if matched >= 2:
            return Blueprint(
                strategy="heading",
                name_sel=tag,
                image_sel="img",
                count=len(names),
                notes=[
                    f"Heading grid: {len(names)} <{tag}> elements",
                    f"Image alt matches: {matched}/{len(names)}",
                ]
            )
    return None


async def detect_list_items(page, url: str, platform: str) -> Blueprint:
    for sel in [
        # WooCommerce / Elementor loop
        "li.product", ".e-loop-item", ".type-product", "[class*='loop-item']",
        # Generic fallbacks — skip bare 'li' on Wix: Wix surfaces products via
        # data-hook attributes, so bare <li> matches are always nav category tiles.
        "li" if platform != "wix" else None,
        "article", ".menu-item", "[class*='entry']",
    ]:
        if sel is None:
            continue
        els = await page.query_selector_all(sel)
        if len(els) < 2:
            continue
        product_like = 0
        for el in els[:20]:
            text = clean(await el.inner_text())
            img  = await el.query_selector("img")
            if img and 2 < len(text) < 600:
                product_like += 1
        # For the broad 'li' selector, require more matches to avoid nav menus.
        min_count = 6 if sel == "li" else 3
        if product_like >= min_count:
            name_sel = await probe_name_selector(page, sel)
            desc_sel = await probe_desc_selector(page, sel, name_sel)
            if name_sel:
                return Blueprint(
                    strategy="container",
                    container_sel=sel,
                    name_sel=name_sel,
                    image_sel="img",
                    desc_sel=desc_sel,
                    count=product_like,
                    notes=[f"List items: {sel} ({product_like} product-like)"]
                )
    return None


async def probe_name_selector(page, container_sel: str) -> str:
    candidates = [
        "h1", "h2", "h3", "h4",
        ".title", ".name", ".product-title", ".product-name",
        "[class*='title']", "[class*='name']",
        "a", "strong", "span", "p"
    ]
    containers = await page.query_selector_all(container_sel)
    if not containers:
        return ""
    sample = containers[:8]

    best_sel, best_count = "", 0
    for sel in candidates:
        hits = 0
        for c in sample:
            el = await c.query_selector(sel)
            if el:
                text = clean(await el.inner_text())
                if 2 < len(text) < 100:
                    hits += 1
        if hits > best_count:
            best_count = hits
            best_sel   = sel

    return best_sel if best_count >= 2 else ""


async def probe_desc_selector(page, container_sel: str, name_sel: str) -> str:
    candidates = [
        "p", ".description", "[class*='description']",
        "[class*='notes']", "[class*='detail']",
        "[class*='body']", "[class*='text']", "span"
    ]
    containers = await page.query_selector_all(container_sel)
    if not containers:
        return ""
    sample = containers[:6]

    for sel in candidates:
        if sel == name_sel:
            continue
        hits = 0
        for c in sample:
            el = await c.query_selector(sel)
            if el:
                text = clean(await el.inner_text())
                if len(text) > 20:
                    hits += 1
        if hits >= 2:
            return sel
    return ""


async def probe_price(container) -> str:
    """
    Try common price selectors on a single product container element.
    Returns the cleaned price string (e.g. '$14.00') or ''.
    """
    PRICE_SELS = [
        # Wix
        "[data-hook='product-item-price-to-pay']",
        "[data-hook='product-item-price']",
        "[data-hook='price-range-from']",
        # Shopify
        ".price", ".product-price", ".price__current",
        "[class*='price']",
        # WooCommerce
        ".woocommerce-Price-amount", ".amount",
        # Generic
        "[class*='Price']", "[class*='cost']",
        "[itemprop='price']",
    ]
    for sel in PRICE_SELS:
        el = await container.query_selector(sel)
        if not el:
            continue
        # Prefer data attribute for machine-readable value
        raw = (await el.get_attribute("data-price")
               or await el.get_attribute("content")
               or await el.inner_text() or "")
        text = clean(raw)
        if text and len(text) < 30:   # sanity: don't grab a whole paragraph
            return text
    return ""


# =============================================================================
# STAGE 2 - EXTRACTOR
# =============================================================================

async def extract(page, blueprint: Blueprint, base_url: str) -> list:
    if blueprint.strategy == "jsonld":
        return await extract_jsonld(page, base_url)
    elif blueprint.strategy == "container":
        return await extract_containers(page, blueprint, base_url)
    elif blueprint.strategy == "heading":
        return await extract_headings(page, blueprint, base_url)
    return []


async def extract_jsonld(page, base_url: str) -> list:
    scripts  = await page.query_selector_all('script[type="application/ld+json"]')
    products = []
    for script in scripts:
        try:
            data  = json.loads(await script.inner_text())
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type", "")
                if t == "Product":
                    name  = clean(item.get("name", ""))
                    desc  = clean(item.get("description", ""))
                    image = item.get("image", "")
                    if isinstance(image, list):
                        image = image[0] if image else ""
                    brand = item.get("brand", {})
                    if isinstance(brand, dict):
                        brand = brand.get("name", "")
                    if name:
                        prod_url = item.get("url", item.get("@id", ""))
                        if prod_url and not prod_url.startswith("http"):
                            prod_url = absolute_url(base_url, prod_url)
                        # Price from JSON-LD offers
                        price = ""
                        offers = item.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        if isinstance(offers, dict):
                            p_val = offers.get("price", offers.get("lowPrice", ""))
                            cur   = offers.get("priceCurrency", "")
                            if p_val:
                                price = f"{cur} {p_val}".strip() if cur else str(p_val)
                        products.append(make_product(
                            name, image, base_url,
                            description=desc, madeby=brand,
                            soldby_link=prod_url, price=price
                        ))
                elif t in ("ItemList", "CollectionPage"):
                    for li in item.get("itemListElement", []):
                        sub  = li.get("item", li)
                        name = clean(sub.get("name", ""))
                        desc = clean(sub.get("description", ""))
                        if name:
                            prod_url = sub.get("url", sub.get("@id", ""))
                            if prod_url and not prod_url.startswith("http"):
                                prod_url = absolute_url(base_url, prod_url)
                            products.append(make_product(
                                name, sub.get("image", ""), base_url,
                                description=desc, soldby_link=prod_url
                            ))
        except Exception:
            continue
    return products


async def extract_containers(page, bp: Blueprint, base_url: str) -> list:
    containers = await page.query_selector_all(bp.container_sel)
    products   = []
    for container in containers:
        name = ""
        if bp.name_sel:
            el = await container.query_selector(bp.name_sel)
            if el:
                name = clean(await el.inner_text())
        if not name:
            continue

        image = ""
        if bp.image_sel:
            # Try <img> first
            el = await container.query_selector(bp.image_sel)
            if el:
                image = await best_image_src(el)

            # Fallback: check <picture><source srcset="…"> inside container
            if not image:
                source = await container.query_selector("picture source")
                if source:
                    raw = (await source.get_attribute("srcset")
                           or await source.get_attribute("data-srcset") or "")
                    if raw:
                        image = raw.split(",")[0].split(" ")[0].strip()

            # Last resort: any img anywhere in the container
            if not image:
                any_img = await container.query_selector("img")
                if any_img:
                    image = await best_image_src(any_img)

        desc = ""
        if bp.desc_sel:
            el = await container.query_selector(bp.desc_sel)
            if el:
                text = clean(await el.inner_text())
                if text and text.lower() != name.lower() and len(text) > 10:
                    desc = text

        price = await probe_price(container)
        link  = await find_product_link(container, base_url)
        products.append(make_product(name, image, base_url, soldby_link=link, price=price))
    return products


async def extract_headings(page, bp: Blueprint, base_url: str) -> list:
    headings = await page.query_selector_all(bp.name_sel)

    # Build alt-text → src map from every img on the page
    all_imgs = await page.query_selector_all("img")
    img_map  = {}   # alt_lower -> src
    for img in all_imgs:
        alt = clean(await img.get_attribute("alt") or "")
        src = await best_image_src(img)
        if src and "logo" not in alt.lower():
            if alt:
                img_map[alt.lower()] = src

    products = []
    for heading in headings:
        name = clean(await heading.inner_text())
        if not name or len(name) < 2 or len(name) > 100:
            continue

        # ── Image: 3-tier resolution ──────────────────────────────────────────
        # Tier 1: exact alt-text match
        image = img_map.get(name.lower(), "")

        # Tier 2: partial word match (handles "Dark Roast Coffee" vs alt "Dark Roast")
        if not image:
            name_words = set(name.lower().split()) - {"the", "a", "an", "and", "of", "for", "in", "at"}
            for alt_lower, src in img_map.items():
                alt_words = set(alt_lower.split())
                if len(name_words & alt_words) >= max(1, len(name_words) // 2):
                    image = src
                    break

        # Tier 3: DOM proximity — walk up from the heading and grab nearest img
        if not image:
            image = await heading.evaluate("""el => {
                function imgSrc(img) {
                    const attrs = ['data-src','data-lazy-src','data-original','data-lazy','data-img','src'];
                    for (const a of attrs) {
                        const v = img.getAttribute(a);
                        if (v && !v.endsWith('.svg') && !v.includes('placeholder')
                                && !v.startsWith('data:image')) return v;
                    }
                    const ss = img.getAttribute('srcset') || img.getAttribute('data-srcset') || '';
                    if (ss) {
                        const candidates = ss.split(',').map(p => p.trim().split(/\\s+/)[0]).filter(Boolean);
                        if (candidates.length) return candidates[candidates.length - 1]; // widest tends to be last
                    }
                    return '';
                }
                // Walk ancestors (up to 6 levels) looking for any img inside
                let node = el;
                for (let i = 0; i < 6; i++) {
                    if (!node.parentElement) break;
                    node = node.parentElement;
                    const img = node.querySelector('img');
                    if (img) { const s = imgSrc(img); if (s) return s; }
                }
                // Try siblings of heading's parent
                const parent = el.parentElement;
                if (parent) {
                    for (const sib of parent.parentElement ? parent.parentElement.children : []) {
                        const img = sib.querySelector('img') || (sib.tagName === 'IMG' ? sib : null);
                        if (img) { const s = imgSrc(img); if (s) return s; }
                    }
                }
                return '';
            }""") or ""

        link  = await find_heading_link(heading, base_url)
        products.append(make_product(name, image, base_url, soldby_link=link))
        # Note: price is not probed here (no container element); filter_products
        # will extract it from the name if a price string is concatenated into it.
    return products


# =============================================================================
# METADATA PROMPTS
# =============================================================================

def prompt_metadata(products: list) -> dict:
    div40 = "\u2500" * 40
    print(f"\n{div40}")
    print("COLUMN DEFAULTS")
    print("Applied to every product row. Press Enter to skip.")
    print(div40)
    print(f"\nProducts found ({len(products)} total):")
    for p in products[:5]:
        img_status = "✓ image" if p["product_image"] else "✗ no image"
        print(f"  - {p['product_name']}  [{img_status}]")
    if len(products) > 5:
        print(f"  - ... and {len(products) - 5} more")
    print()

    category     = input("Category  (e.g. Coffee, Tea, Pastry):          ").strip()
    madeby       = input("Made By   (e.g. Overmountain Coffee Roasters):  ").strip()
    soldby       = input("Sold By   (e.g. Overmountain Coffee Roasters):  ").strip()
    madeby_image = input("Made By Logo URL:                               ").strip()
    soldby_image = input("Sold By Logo URL:                               ").strip()

    print()
    for label, val in [
        ("Category",         category),
        ("Made By",          madeby),
        ("Sold By",          soldby),
        ("Made By Logo URL", madeby_image),
        ("Sold By Logo URL", soldby_image),
    ]:
        if val:
            print(f"  + {label} -> '{val}'")
    if not any([category, madeby, soldby, madeby_image, soldby_image]):
        print("  (No defaults set — columns left empty)")

    return {
        "category":     category,
        "madeby_name":  madeby,
        "soldby_name":  soldby,
        "madeby_image": madeby_image,
        "soldby_image": soldby_image,
    }


FALLBACK_IMAGE = "https://www.test.northstarproject.org/static/media/appiconcrop3.a1bc55ceda3313f4778a.png"


def apply_metadata(products: list, metadata: dict) -> list:
    category = (metadata.get("category") or "").strip()
    madeby_name = (metadata.get("madeby_name") or "").strip()
    soldby_name = (metadata.get("soldby_name") or "").strip()
    madeby_image = (metadata.get("madeby_image") or "").strip()
    soldby_image = (metadata.get("soldby_image") or "").strip()

    for p in products:
        if category:
            p["category"] = category
        if madeby_name:
            p["madeby_name"] = madeby_name
        if soldby_name:
            p["soldby_name"] = soldby_name
        if madeby_image:
            p["madeby_image"] = madeby_image
        if soldby_image:
            p["soldby_image"] = soldby_image
        # soldby_link is auto-scraped — never overwritten by metadata

        if not p.get("product_image"):
            p["product_image"] = FALLBACK_IMAGE
        if not p.get("madeby_image"):
            p["madeby_image"] = FALLBACK_IMAGE
        if not p.get("soldby_image"):
            p["soldby_image"] = FALLBACK_IMAGE

    return products


# =============================================================================
# OUTPUT
# =============================================================================

def write_csv(products: list, output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(products)
    print(f"\n+ Saved {len(products)} products -> {output_path}")


# =============================================================================
# MAIN
# =============================================================================

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

        async def load_page(target_url):
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=35000)
            except Exception as e:
                print(f"  ! Timeout ({e.__class__.__name__}), continuing with what loaded...")
            await page.wait_for_timeout(2500)

        print("-> Loading page...")
        await load_page(url)

        # Stage 1: Detect structure on first page only.
        # The blueprint is reused for every subsequent page.
        print("-> Detecting page structure...")
        blueprint = await detect(page, url)

        if blueprint.strategy == "none":
            print("\n! Could not detect a product pattern on this page.")
            print("  Tips:")
            print("  - Try a more specific URL (e.g. /shop, /menu, /collections/all)")
            print("  - Run dom_inspector.py on the URL and share the output")
            await browser.close()
            sys.exit(1)

        # Stage 2: Paginated extraction loop.
        # Detect runs once, extract runs on every page.
        all_products = []
        current_url  = url
        page_num     = 1
        MAX_PAGES    = 50  # safety cap

        while True:
            print(f"-> Extracting page {page_num}...")
            page_products = await extract(page, blueprint, current_url)
            all_products.extend(page_products)
            print(f"   {len(page_products)} products on this page "
                  f"({len(all_products)} total so far)")

            if page_num >= MAX_PAGES:
                print(f"  ! Reached {MAX_PAGES}-page safety cap, stopping.")
                break

            next_url = await find_next_page(page, current_url)
            if not next_url or next_url == current_url:
                print(f"  -> No next page found, done.")
                break

            print(f"  -> Next page: {next_url}")
            current_url = next_url
            page_num   += 1
            await load_page(current_url)

        products = filter_products(deduplicate(all_products))
        await browser.close()

    if not products:
        print("\n! Detection found a pattern but extraction returned no products.")
        print("  Try running dom_inspector.py and sharing the output.")
        sys.exit(1)

    print(f"\n-> Extracted {len(products)} unique products")

    metadata = prompt_metadata(products)
    products = apply_metadata(products, metadata)

    with_img = sum(1 for p in products if p["product_image"])
    divider = "\u2500" * 55
    print(f"\n{divider}")
    print(f"{'PRODUCT':<30} {'IMG':>3}  {'CATEGORY':<16} {'MADE BY'}")
    print(divider)
    for p in products[:6]:
        img_mark = "\u2713" if p["product_image"] else "\u2717"
        print(f"{p['product_name'][:28]:<30} {img_mark:>3}  "
              f"{p['category'][:14]:<16} {p['madeby_name'][:20]}")
    if len(products) > 6:
        print(f"  ... and {len(products) - 6} more")
    print(divider)
    print(f"  Images found: {with_img}/{len(products)}")

    write_csv(products, output)


async def run_scrape(url: str) -> tuple:
    """
    Programmatic API for the web interface (app.py).
    No stdin prompts, no CSV writing.
    Returns (products: list, error: str).
    """
    from playwright.async_api import async_playwright
    try:
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

            async def load_page_rs(target_url):
                try:
                    await page.goto(target_url, wait_until="networkidle", timeout=35000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)

            await load_page_rs(url)

            blueprint = await detect(page, url)
            if blueprint.strategy == "none":
                await browser.close()
                msg = (blueprint.notes[0] if blueprint.notes
                       else "Could not detect a product pattern on this page.")
                return [], msg

            all_products = []
            current_url  = url
            page_num     = 1
            MAX_PAGES    = 50

            while True:
                page_products = await extract(page, blueprint, current_url)
                all_products.extend(page_products)

                if page_num >= MAX_PAGES:
                    break
                next_url = await find_next_page(page, current_url)
                if not next_url:
                    break
                current_url = next_url
                page_num   += 1
                await load_page_rs(current_url)

            products = filter_products(deduplicate(all_products))
            await browser.close()

        if not products:
            return [], "Detection found a pattern but extraction returned no products."

        return products, ""

    except Exception as exc:
        return [], str(exc)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape product pages -> NorthStar CSV"
    )
    parser.add_argument("url", help="Product listing URL to scrape")
    parser.add_argument(
        "--output", "-o",
        default="northstar_products.csv",
        help="Output CSV filename (default: northstar_products.csv)"
    )
    args = parser.parse_args()
    asyncio.run(scrape(args.url, args.output))


if __name__ == "__main__":
    main()
