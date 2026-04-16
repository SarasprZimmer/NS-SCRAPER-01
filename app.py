"""
NorthStar Scraper — Web Interface
Run: python app.py
Then open: http://localhost:5000
"""

import asyncio
import json
import os
import sys

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(__file__))
from scraper2 import apply_metadata, run_scrape

app = Flask(__name__, static_folder=os.path.dirname(__file__))


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    metadata = {
        "category":    (data.get("category") or "").strip(),
        "madeby_name": (data.get("madeby")   or "").strip(),
        "soldby_name": (data.get("soldby")   or "").strip(),
    }

    try:
        products, error = asyncio.run(run_scrape(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if error:
        return jsonify({"error": error}), 422

    products = apply_metadata(products, metadata)
    return jsonify({"products": products, "count": len(products)})


if __name__ == "__main__":
    print("\nNorthStar Scraper UI")
    print("─" * 30)
    print("Open in browser: http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=False, port=5000)
