from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
import time, re, json, os
from datetime import datetime, date

app = Flask(__name__, template_folder="templates")

PRICE_FILE = os.path.join(os.path.dirname(__file__), "price_cache.json")
CACHE_TTL = 86400  # 24 hours — refresh daily

# ── Reference prices (Mar 2026, SGD/litre) ────────────────────────────
REFERENCE_PRICES = {
    "Shell": {
        "92": None,
        "95": 3.40,
        "98": 3.92,
        "V-Power 98": 4.14,
        "Diesel": 3.93,
    },
    "Caltex": {
        "92": 3.38,
        "95": 3.42,
        "98": 4.11,
        "Diesel": 3.93,
    },
    "SPC": {
        "92": 3.38,
        "95": 3.41,
        "98": 3.92,
        "Diesel": 3.72,
    },
    "Esso": {
        "92": 3.38,
        "95": 3.42,
        "98": 3.92,
        "Diesel": 3.93,
    },
    "BP": {
        "92": None,
        "95": 3.41,
        "98": 3.92,
        "Diesel": 3.93,
    },
    "Sinopec": {
        "92": 3.30,
        "95": 3.35,
        "98": None,
        "Diesel": 3.65,
    },
}

# Remove None entries
for s in REFERENCE_PRICES:
    REFERENCE_PRICES[s] = {k: v for k, v in REFERENCE_PRICES[s].items() if v is not None}

STATION_COLORS = {
    "Shell": "#e2231a",
    "Caltex": "#005daa",
    "SPC": "#e87722",
    "Esso": "#003087",
    "BP": "#009900",
    "Sinopec": "#cc0000",
}


def try_scrape_shell():
    urls = [
        "https://www.shell.com.sg/motorists/shell-fuels/shell-fuels-pricing.html",
        "https://www.shell.com.sg/motorists/fuels-and-prices.html",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            soup = BeautifulSoup(r.text, "html.parser")
            prices = {}
            for row in soup.select("table tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    name = cells[0].get_text(strip=True)
                    val = cells[-1].get_text(strip=True)
                    m = re.search(r"(\d+\.\d+)", val)
                    if m and float(m.group()) > 1.5:
                        prices[name] = float(m.group())
            if prices:
                return prices
        except Exception as e:
            print(f"Shell scrape error ({url}):", e)
    return None


def try_scrape_spc():
    try:
        r = requests.get("https://www.spc.com.sg/petrol-prices/", timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        prices = {}
        for row in soup.select("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                name = cells[0].get_text(strip=True)
                val = cells[-1].get_text(strip=True)
                m = re.search(r"(\d+\.\d+)", val)
                if m and name and float(m.group()) > 1.5:
                    prices[name] = float(m.group())
        return prices if prices else None
    except Exception as e:
        print("SPC scrape error:", e)
        return None


def load_cache():
    try:
        if os.path.exists(PRICE_FILE):
            with open(PRICE_FILE, "r") as f:
                data = json.load(f)
            cache_date = data.get("cache_date", "")
            if cache_date == str(date.today()):
                return data
    except Exception:
        pass
    return None


def save_cache(data):
    try:
        data["cache_date"] = str(date.today())
        with open(PRICE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Cache save error:", e)


def get_prices():
    cached = load_cache()
    if cached:
        return cached

    data = {"stations": {}, "updated": datetime.now().strftime("%d %b %Y %H:%M"),
            "note": "Pump prices before member/card discounts. Updated daily."}

    shell_live = try_scrape_shell()
    data["stations"]["Shell"] = {
        "prices": shell_live or REFERENCE_PRICES["Shell"],
        "live": bool(shell_live),
        "color": STATION_COLORS["Shell"],
    }

    spc_live = try_scrape_spc()
    data["stations"]["SPC"] = {
        "prices": spc_live or REFERENCE_PRICES["SPC"],
        "live": bool(spc_live),
        "color": STATION_COLORS["SPC"],
    }

    for name in ["Caltex", "Esso", "BP", "Sinopec"]:
        data["stations"][name] = {
            "prices": REFERENCE_PRICES[name],
            "live": False,
            "color": STATION_COLORS[name],
        }

    save_cache(data)
    return data


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/prices")
def prices():
    return jsonify(get_prices())


@app.route("/api/prices/refresh", methods=["POST"])
def prices_refresh():
    """Force refresh — delete cache and re-fetch."""
    if os.path.exists(PRICE_FILE):
        os.remove(PRICE_FILE)
    return jsonify(get_prices())


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "no query"}), 400
    try:
        r = requests.get(
            "https://www.onemap.gov.sg/api/common/elastic/search",
            params={"searchVal": q, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1},
            timeout=8
        )
        data = r.json()
        results = data.get("results", [])
        if not results:
            return jsonify({"error": "not found"}), 404
        seen = set()
        suggestions = []
        for item in results[:8]:
            addr = item.get("ADDRESS", "")
            if addr in seen:
                continue
            seen.add(addr)
            # Build clean label
            parts = []
            blk = item.get("BLK_NO", "").strip()
            road = item.get("ROAD_NAME", "").strip()
            building = item.get("BUILDING", "").strip()
            postal = item.get("POSTAL", "").strip()
            if blk and road:
                parts.append(f"{blk} {road}")
            elif road:
                parts.append(road)
            if building and building not in parts:
                parts.append(building)
            label = ", ".join(parts)
            if postal:
                label += f" (S{postal})"
            suggestions.append({
                "label": label or addr,
                "address": addr,
                "postal": postal,
                "lat": float(item["LATITUDE"]),
                "lon": float(item["LONGITUDE"]),
            })
        return jsonify({"results": suggestions[:5]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/routes")
def routes():
    try:
        slat = request.args.get("slat")
        slon = request.args.get("slon")
        dlat = request.args.get("dlat")
        dlon = request.args.get("dlon")
        efficiency = float(request.args.get("efficiency", 12.0))

        # Try OSRM public API with alternatives=true
        url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{slon},{slat};{dlon},{dlat}"
            f"?alternatives=3&overview=full&geometries=geojson&steps=false"
        )
        r = requests.get(url, timeout=15)
        data = r.json()
        osrm_routes = data.get("routes", [])

        # If OSRM only gives 1-2, pad with slight variations via waypoints
        # (OSRM alternative routing depends on the geometry having genuine alternatives)

        colors = ["#4f46e5", "#0891b2", "#059669", "#d97706"]
        labels = ["Recommended", "Alternative 1", "Alternative 2", "Alternative 3"]
        result = []

        for i, route in enumerate(osrm_routes[:4]):
            dist_km = route["distance"] / 1000
            dur_min = route["duration"] / 60
            fuel_l = dist_km * efficiency / 100
            result.append({
                "index": i,
                "label": labels[i],
                "distance_km": round(dist_km, 1),
                "duration_min": round(dur_min),
                "fuel_litres": round(fuel_l, 2),
                "geometry": route["geometry"],
                "color": colors[i],
            })

        if not result:
            return jsonify({"error": "No routes found between these locations."}), 404

        return jsonify({"routes": result, "total": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("FuelSG v3 running on http://localhost:5000")
    app.run(debug=False, host="0.0.0.0", port=5000)
