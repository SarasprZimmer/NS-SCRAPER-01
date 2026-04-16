# NorthStar Product Scraper

A headless-browser scraper that extracts product data from any e-commerce or retail website and exports it as a NorthStar-ready CSV. Includes both a **command-line tool** and a **web UI** you can open in your browser.

---

## What It Does

You give it a URL — a shop page, a menu, a collection, a product category — and it:

1. Launches a hidden Chrome browser and loads the page (JavaScript fully rendered)
2. Automatically detects how the products are laid out on that specific site — no manual configuration needed
3. Extracts the product name, image URL, and infers a category from the product text
4. Prompts you for the Made By and Sold By values to apply across all rows
5. Exports a clean CSV that maps directly into the NorthStar workspace table

It works across platforms — Shopify, WooCommerce, Wix, Squarespace, Webflow, and generic custom sites — without any platform-specific hardcoding.

---

## Files in This Folder

| File | What it is |
|---|---|
| `scraper2.py` | The core scraper — run directly from the terminal |
| `app.py` | Flask web server that powers the browser UI |
| `index.html` | The browser interface (served by app.py) |
| `dom_inspector.py` | Debug tool — run when a site returns 0 products |
| `requirements.txt` | Python dependencies |

---

## Setup (One Time Only)

### Step 1 — Install Python dependencies

Open a terminal in this folder and run:

```bash
python -m pip install -r requirements.txt
```

This installs Playwright (the headless browser library) and Flask (the web server).

### Step 2 — Install the Chromium browser

Playwright needs its own copy of Chrome:

```bash
playwright install chromium
```

This downloads a headless Chromium browser (~150 MB). You only ever need to do this once.

---

## Option A — Web UI (Recommended)

The easiest way to use the scraper. Everything happens in your browser.

### Step 1 — Start the server

```bash
python app.py
```

You should see:

```
NorthStar Scraper UI
──────────────────────────────
Open in browser: http://localhost:5000
Press Ctrl+C to stop
```

### Step 2 — Open the UI

Open your browser and go to: **http://localhost:5000**

### Step 3 — Scrape a site

1. Paste a product listing URL into the **URL** field
2. Optionally fill in **Category**, **Made By**, and **Sold By** — these get applied to every product row
3. Click **Scrape** and wait 15–30 seconds while the page loads and products are detected
4. Review the results in the table — every cell is editable, so you can fix anything on the spot
5. Click **Download CSV** when you're happy with the data

### Step 4 — Stop the server

Press `Ctrl + C` in the terminal when you're done.

---

## Option B — Command Line

If you prefer working in the terminal:

```bash
# Basic — saves to northstar_products.csv
python scraper2.py https://example.com/shop

# Custom output filename
python scraper2.py https://example.com/collections/all --output my_products.csv
```

After the products are extracted, the scraper will ask you three questions in the terminal:

```
Category  (e.g. Coffee, Tea, Pastry):
Made By   (e.g. Overmountain Coffee Roasters):
Sold By   (e.g. Overmountain Coffee Roasters):
```

Press Enter to skip any field you don't want to fill in. The answers are applied to every product row before the CSV is saved.

---

## CSV Columns → NorthStar Workspace Mapping

| CSV column | NorthStar table column |
|---|---|
| `product_name` | Product Name |
| `product_image` | Product Image URL |
| `category` | Category |
| `madeby_name` | Made By |
| `soldby_name` | Sold By |

---

## Tips for Best Results

**Use a collection or listing page, not the homepage.**
The scraper needs a page that shows multiple products at once — a grid or list. Good examples:

| Platform | Good URL patterns |
|---|---|
| Shopify | `/collections/all`, `/collections/coffee` |
| WooCommerce | `/shop`, `/product-category/drinks` |
| Squarespace | `/shop`, `/menu` |
| Wix | `/shop`, `/store` |
| Generic | `/products`, `/menu`, `/our-products` |

**If you get 0 results:**
Run the DOM inspector on the URL and share the output — it shows the page structure so the scraper can be tuned:

```bash
python dom_inspector.py https://example.com/shop
```

**If images are missing:**
Some sites load images lazily (only when they scroll into view). The scraper handles the most common lazy-load patterns (`data-src`, `data-lazy-src`, `srcset`, etc.), but a few unusual implementations may still come back empty. You can paste the correct image URL directly into the web UI table before downloading.

---

## How the Scraper Works (Under the Hood)

The scraper runs a two-stage pipeline:

### Stage 1 — Detection

It tries five strategies in order, stopping as soon as one finds at least 2 products:

1. **JSON-LD structured data** — checks for `<script type="application/ld+json">` blocks with `@type: Product` — the most reliable source when available
2. **Data attribute containers** — looks for elements with `data-product-id`, `data-product`, or `data-item-id` — common in Shopify and custom storefronts
3. **Repeating grid detection** — finds the element whose parent has the most same-type siblings that each contain text and an image; strips dynamic/unique ID class tokens (like Elementor's `elementor-element-62861a7c`) so the pattern matching works even on heavily templated sites
4. **Heading grid** — for flat-layout pages (Wix, some Squarespace) where products are just headings with nearby images; uses a 3-tier image resolution: exact alt-text match → partial word match → DOM proximity walk
5. **Known container selectors** — explicitly checks WooCommerce (`li.product`, `.e-loop-item`, `.type-product`) and generic list/article patterns

### Stage 2 — Extraction

Once a strategy identifies the product pattern, the extractor pulls data from each item:

- **Name** — from the detected heading or title element
- **Image** — checks `srcset` (picks highest resolution), then `data-src` / `data-lazy-src` / `data-original` / `data-lazy`, then plain `src`; also checks `<picture><source>` elements; falls back to a DOM proximity walk up 6 ancestor levels if no image is found in the container
- **Category** — inferred from keywords in the product name (e.g. "dark roast" → Coffee, "merlot" → Wine, "lip balm" → Beauty)

---

## Troubleshooting

**`playwright install chromium` fails**
Make sure you installed the requirements first: `python -m pip install -r requirements.txt`

**`pip` is not recognized**
Use `python -m pip install ...` instead of `pip install ...`

**The site times out**
Some sites are slow or block automated browsers. The scraper waits up to 35 seconds and continues with whatever loaded. Try the URL directly in your browser first to make sure the page actually works.

**Products detected but all wrong (navigation items instead of products)**
Run `python dom_inspector.py <url>` and note the class names shown. Share the output so the selectors can be tuned.

**The web UI shows "Could not connect"**
Make sure `python app.py` is still running in a terminal window. The server needs to be running while you use the UI.
