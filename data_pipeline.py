"""
CITYDEV — Data Pipeline (data_pipeline.py)
Loads and preprocesses all six data files, computes demand-gap metrics,
building aggregations, ecostat cost references, and candidate vacant parcels.
"""

import json
import csv
import math
import random
import time
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Any
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

try:
    from google import genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# Embedded Gemini API key (remove before pushing)
GEMINI_API_KEY = ""

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"

WARD_BOUNDARY_FILE  = DATA_DIR / "kochi_ward_boundary.json"
WARD_SMALL_FILE     = DATA_DIR / "kochi_ward_boundaries.geojson"
BUILDINGS_FILE      = DATA_DIR / "kochi_building_kaloor_vytilla_normal.json"
REVIEWS_FILE        = DATA_DIR / "kochi_building_review_dataset_17670.csv"
ECOSTAT_FILE        = DATA_DIR / "ecostat_converted.json"
CENSUS_FILE         = DATA_DIR / "DDW_PCA3208_2011_MDDS_with_UI.json"

# ─── Construction Cost Constants (cited market composite + Ecostat) ────────────
COST_NEW_SQM        = 22_600    # ₹/sqm civil-turnkey new construction (2026 Kerala market)
COST_RENOV_SQM      = 7_900     # ₹/sqm renovation (35% of new, industry assumption)
COST_NOTE           = (
    "Cost basis: ₹22,600/sqm new construction (2026 Kerala market composite); "
    "₹7,900/sqm renovation (industry retrofit-ratio assumption: 35% of new rate). "
    "Cross-checked against Q4 2025-26 Kerala Ecostat EKM material prices. "
    "Not a bottom-up quantity-surveyed estimate."
)

# Building type → category mapping
BUILDING_CATEGORY_MAP = {
    "school": "school", "college": "school", "university": "school",
    "kindergarten": "school", "yes": "generic", "house": "residential",
    "apartments": "residential", "residential": "residential",
    "commercial": "commercial", "retail": "commercial", "office": "commercial",
    "hospital": "clinic", "clinic": "clinic", "healthcare": "clinic",
    "pharmacy": "clinic", "doctors": "clinic",
    "temple": "religious", "church": "religious", "mosque": "religious",
    "stadium": "recreation", "sports_centre": "recreation", "gym": "recreation",
    "park": "recreation", "library": "library", "public": "public",
    "government": "public", "community_centre": "public",
    "palace": "heritage", "hotel": "commercial", "restaurant": "commercial",
    "supermarket": "commercial",
}

FACILITY_CATEGORIES = ["school", "clinic", "recreation", "library", "commercial", "public"]

# Targets per 1000 residents (aspirational benchmarks for Indian urban areas)
FACILITY_TARGETS_PER_1000 = {
    "school":      1.5,
    "clinic":      0.8,
    "recreation":  0.5,
    "library":     0.2,
    "commercial":  3.0,
    "public":      0.4,
}

FACILITY_LABELS = {
    "school":      "Schools / Colleges",
    "clinic":      "Health Clinics / Hospitals",
    "recreation":  "Recreation / Parks",
    "library":     "Libraries",
    "commercial":  "Commercial / Markets",
    "public":      "Community / Public",
}

FACILITY_ICONS = {
    "school":      "🏫",
    "clinic":      "🏥",
    "recreation":  "🌳",
    "library":     "📚",
    "commercial":  "🏪",
    "public":      "🏛️",
}

# ─── Geometry helpers (pure Python, no geopandas dependency) ──────────────────

def _polygon_centroid(coords) -> Tuple[float, float]:
    """Compute centroid of the first ring of a GeoJSON polygon (lon, lat)."""
    ring = coords[0]
    n = len(ring)
    if n == 0:
        return 0.0, 0.0
    cx = sum(pt[0] for pt in ring) / n
    cy = sum(pt[1] for pt in ring) / n
    return cx, cy


def _polygon_area_sqm(coords) -> float:
    """
    Approximate area of a GeoJSON polygon in square meters using the
    spherical excess formula (works well for small polygons in Kerala).
    """
    ring = coords[0]
    if len(ring) < 3:
        return 0.0
    # Convert to meters using 1° lat ≈ 111,320 m, 1° lon ≈ 111,320 * cos(lat) m
    lat0 = ring[0][1]
    lat_m = 111_320.0
    lon_m = 111_320.0 * math.cos(math.radians(lat0))
    pts = [(p[0] * lon_m, p[1] * lat_m) for p in ring]
    n = len(pts)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1]
        area -= pts[j][0] * pts[i][1]
    return abs(area) / 2.0


def _point_in_polygon(px: float, py: float, poly_coords) -> bool:
    """Ray-casting point-in-polygon test for the first ring only."""
    ring = poly_coords[0]
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _get_coords(geometry):
    """Return coordinates list from a GeoJSON geometry dict."""
    gtype = geometry.get("type", "")
    if gtype == "Polygon":
        return geometry.get("coordinates", [[]])
    elif gtype == "MultiPolygon":
        # Return largest polygon
        polys = geometry.get("coordinates", [[[]]])
        if not polys:
            return [[]]
        return max(polys, key=lambda p: len(p[0]) if p else 0)
    return [[]]


# ─── Ward Boundary Loader ─────────────────────────────────────────────────────

def load_wards() -> List[Dict]:
    """Load 74-ward boundary file. Returns list of ward dicts."""
    with open(WARD_BOUNDARY_FILE, encoding="utf-8") as f:
        data = json.load(f)
    wards = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        geom  = feat.get("geometry", {})
        coords = _get_coords(geom)
        cx, cy = _polygon_centroid(coords)
        # Use st_area(shape) from properties (in sqm, provided in file)
        # Note: key has parentheses so we must try exact string
        area_sqm = None
        for key in props:
            if 'area' in key.lower() and 'shape' in key.lower():
                try:
                    area_sqm = float(props[key])
                except (ValueError, TypeError):
                    pass
                break
        if not area_sqm or area_sqm <= 0:
            area_sqm = _polygon_area_sqm(coords)
        wards.append({
            "ward_lgd_code":  props.get("ward_lgd_code"),
            "ward_lgd_name":  props.get("ward_lgd_name", "Unknown"),
            "sourcewardcode": props.get("sourcewardcode", ""),
            "town_lgd_code":  props.get("town_lgd_code"),
            "area_sqm":       float(area_sqm) if area_sqm else 0.0,
            "centroid_lon":   cx,
            "centroid_lat":   cy,
            "coords":         coords,
            "geometry_type":  geom.get("type", "Polygon"),
        })
    return wards


# ─── Census Loader ────────────────────────────────────────────────────────────

def load_census_ward_population() -> Dict:
    """
    Extracts total population for Kochi Municipal Corporation from 2011 census.
    Returns: {'total_pop': int, 'ward_rows': list}
    IMPORTANT: 73 census wards ≠ 74 current wards. We use density-based estimation.
    """
    with open(CENSUS_FILE, encoding="utf-8") as f:
        rows = json.load(f)
    kochi_town_code = 803288
    ward_rows = [
        r for r in rows
        if str(r.get("Level", "")).strip().upper() == "WARD"
        and (r.get("Town/Village") == kochi_town_code or str(r.get("Town/Village", "")) == str(kochi_town_code))
        # TRU can be "Urban" or "Total" depending on JSON version — accept any non-Rural
        and str(r.get("TRU", "")).strip() not in ("Rural",)
    ]
    # De-duplicate if both "Total" and "Urban" rows present — take first occurrence per Ward number
    seen_wards = set()
    deduped = []
    for r in ward_rows:
        wnum = r.get("Ward", r.get("ward", ""))
        if wnum not in seen_wards:
            seen_wards.add(wnum)
            deduped.append(r)
    ward_rows = deduped
    total_pop = sum(r.get("TOT_P", 0) for r in ward_rows)
    return {"total_pop": total_pop, "ward_rows": ward_rows, "num_census_wards": len(ward_rows)}


def estimate_ward_populations(wards: List[Dict], census: Dict) -> List[Dict]:
    """
    Density-based population estimation per current ward.
    Method: total 2011 corp population / total area → density per sqm
    Each ward's pop = density × ward_area × growth_factor (1.05 for 2011→present)
    """
    total_area = sum(w["area_sqm"] for w in wards)
    if total_area == 0:
        return wards
    density = census["total_pop"] / total_area  # people per sqm
    growth_factor = 1.05
    for w in wards:
        w["population"] = int(w["area_sqm"] * density * growth_factor)
        w["density_per_sqkm"] = round((w["population"] / w["area_sqm"]) * 1_000_000, 1) if w["area_sqm"] > 0 else 0
        w["area_sqkm"] = round(w["area_sqm"] / 1_000_000, 4)
    return wards


# ─── Building Loader ─────────────────────────────────────────────────────────

def load_buildings() -> List[Dict]:
    """Load OSM building footprints. Returns list of building dicts."""
    with open(BUILDINGS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    buildings = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        geom  = feat.get("geometry", {})
        coords = _get_coords(geom)
        cx, cy = _polygon_centroid(coords)
        area_sqm = _polygon_area_sqm(coords)
        raw_type = str(props.get("building", "yes")).lower().strip()
        category = BUILDING_CATEGORY_MAP.get(raw_type, "generic")
        buildings.append({
            "building_id":  props.get("@id", ""),
            "building_tag": raw_type,
            "category":     category,
            "name":         props.get("name", ""),
            "centroid_lon": cx,
            "centroid_lat": cy,
            "area_sqm":     area_sqm,
            "coords":       coords,
        })
    return buildings


# ─── Reviews Loader ───────────────────────────────────────────────────────────

def load_reviews() -> Dict[str, Dict]:
    """Load review CSV. Returns dict keyed by building_id."""
    reviews = {}
    try:
        with open(REVIEWS_FILE, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bid = row.get("building_id", "").strip()
                if not bid:
                    continue
                if bid not in reviews:
                    reviews[bid] = {
                        "building_name": row.get("building_name", ""),
                        "building_type": row.get("building_type", ""),
                        "latitude":      float(row.get("latitude", 0) or 0),
                        "longitude":     float(row.get("longitude", 0) or 0),
                        "ratings":       [],
                        "sentiments":    [],
                        "review_source": row.get("review_source", ""),
                    }
                try:
                    reviews[bid]["ratings"].append(float(row.get("rating", 3)))
                except (ValueError, TypeError):
                    pass
                reviews[bid]["sentiments"].append(row.get("sentiment", "neutral"))
    except FileNotFoundError:
        pass
    # Compute averages
    for bid, rv in reviews.items():
        rv["avg_rating"] = round(sum(rv["ratings"]) / len(rv["ratings"]), 1) if rv["ratings"] else 3.0
        sv = rv["sentiments"]
        rv["sentiment_summary"] = max(set(sv), key=sv.count) if sv else "neutral"
    return reviews


# ─── Ecostat Loader ───────────────────────────────────────────────────────────

def load_ecostat() -> Dict:
    """Extract EKM (Ernakulam) material prices and labour wages from ecostat."""
    with open(ECOSTAT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    materials = []
    for item in data.get("building_materials", []):
        ekm_val = item.get("EKM")
        if ekm_val and str(ekm_val).strip() not in ("", "NA", "null"):
            try:
                price = float(str(ekm_val).replace(",", ""))
                materials.append({
                    "category": item.get("category", ""),
                    "item":     item.get("item", ""),
                    "ekm_price": price,
                    "state_avg": item.get("State Avg"),
                    "unit":     "as per item",
                })
            except ValueError:
                pass
    labour = []
    for lw in data.get("labour_wage_rates", []):
        ekm_val = lw.get("EKM")
        if ekm_val and str(ekm_val).strip() not in ("", "NA", "null"):
            try:
                wage = float(str(ekm_val).replace(",", ""))
                labour.append({
                    "worker_category": lw.get("worker_category", ""),
                    "ekm_wage": wage,
                })
            except ValueError:
                pass
    return {
        "materials": materials,
        "labour":    labour,
        "key_items": {
            "cement_53grade_50kg": 350,
            "tmt_steel_12mm_per_ton": 57_500,
            "bricks_class_a_per_1000": 11_000,
            "mason_wage_per_day": 1_200,
        }
    }


# ─── Spatial Join ─────────────────────────────────────────────────────────────

def spatial_join_buildings_to_wards(
    buildings: List[Dict],
    wards: List[Dict],
) -> List[Dict]:
    """
    Assigns each building a ward_lgd_code using point-in-polygon.
    Uses building centroid for efficiency.
    """
    # Build a bounding-box index per ward for fast pre-filtering
    ward_boxes = []
    for w in wards:
        ring = w["coords"][0] if w["coords"] else []
        if ring:
            lons = [pt[0] for pt in ring]
            lats = [pt[1] for pt in ring]
            ward_boxes.append((min(lons), min(lats), max(lons), max(lats), w))
        else:
            ward_boxes.append((0, 0, 0, 0, w))

    for b in buildings:
        px, py = b["centroid_lon"], b["centroid_lat"]
        b["ward_lgd_code"] = None
        for minx, miny, maxx, maxy, w in ward_boxes:
            if minx <= px <= maxx and miny <= py <= maxy:
                if _point_in_polygon(px, py, w["coords"]):
                    b["ward_lgd_code"] = w["ward_lgd_code"]
                    break
    return buildings


# ─── Demand-Gap Computation ───────────────────────────────────────────────────

def compute_demand_gaps(
    wards: List[Dict],
    buildings: List[Dict],
) -> Tuple[List[Dict], Dict]:
    """
    Computes per-1000-resident facility ratios per ward and gap vs. corp average.
    Returns: (wards_with_gaps, corp_averages_dict)
    """
    # Index buildings by ward
    ward_buildings: Dict[Any, List] = {w["ward_lgd_code"]: [] for w in wards}
    for b in buildings:
        wid = b.get("ward_lgd_code")
        if wid and wid in ward_buildings:
            ward_buildings[wid].append(b)

    # Aggregate counts per ward per category
    for w in wards:
        wid = w["ward_lgd_code"]
        blist = ward_buildings.get(wid, [])
        counts = {cat: 0 for cat in FACILITY_CATEGORIES}
        for b in blist:
            cat = b.get("category", "generic")
            if cat in counts:
                counts[cat] += 1
        w["facility_counts"]  = counts
        w["total_buildings"]  = len(blist)
        pop = w.get("population", 1) or 1
        w["facility_per1000"] = {
            cat: round(cnt / pop * 1000, 3)
            for cat, cnt in counts.items()
        }

    # Corporation-wide averages (total facilities / total pop * 1000)
    total_pop = sum(w.get("population", 0) for w in wards) or 1
    corp_avg: Dict[str, float] = {}
    for cat in FACILITY_CATEGORIES:
        total_count = sum(w["facility_counts"].get(cat, 0) for w in wards)
        # Avoid inflated averages by capping at sensible max
        raw_avg = total_count / total_pop * 1000
        corp_avg[cat] = round(min(raw_avg, FACILITY_TARGETS_PER_1000.get(cat, 5.0) * 2), 4)

    # Gap = ward_ratio - corp_avg (negative = deficit, positive = surplus)
    for w in wards:
        w["demand_gap"] = {}
        pop = w.get("population", 1) or 1
        for cat in FACILITY_CATEGORIES:
            ward_ratio = w["facility_per1000"].get(cat, 0)
            gap = ward_ratio - corp_avg.get(cat, 0)
            w["demand_gap"][cat] = round(gap, 4)
        # Gap severity score (0-100, higher = bigger deficit)
        deficits = [abs(v) for v in w["demand_gap"].values() if v < 0]
        w["gap_severity"] = min(100, int(sum(deficits) / len(deficits) * 50)) if deficits else 0

    return wards, corp_avg


# ─── Vacant Parcel Detection (ML-based) ───────────────────────────────────────

def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Haversine distance in meters between two lat/lon points."""
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _compute_building_features(
    grid_lat: float, grid_lon: float,
    ward_buildings: List[Dict], ward_centroid_lat: float,
    ward_centroid_lon: float, gap_severity: float,
    radius_m: float = 400.0,
) -> Dict[str, float]:
    """
    Compute spatial features for a grid cell relative to nearby buildings.
    These features characterise how "empty" the cell's neighbourhood is.
    """
    distances = []
    categories = set()
    total_building_area = 0.0

    for b in ward_buildings:
        d = _haversine_m(grid_lat, grid_lon, b["centroid_lat"], b["centroid_lon"])
        if d <= radius_m:
            distances.append(d)
            categories.add(b.get("category", "generic"))
            total_building_area += b.get("area_sqm", 0)

    n_nearby = len(distances)
    avg_dist = float(np.mean(distances)) if distances else radius_m * 2
    min_dist = float(np.min(distances)) if distances else radius_m * 2
    diversity = len(categories)

    area_circle = math.pi * radius_m ** 2
    building_density = n_nearby / (area_circle / 1_000_000) if area_circle > 0 else 0
    coverage_ratio = min(1.0, total_building_area / area_circle) if area_circle > 0 else 0

    dist_to_center = _haversine_m(grid_lat, grid_lon, ward_centroid_lat, ward_centroid_lon)

    return {
        "building_density":   round(building_density, 4),
        "avg_nearest_dist":   round(avg_dist, 1),
        "min_nearest_dist":   round(min_dist, 1),
        "building_coverage":  round(coverage_ratio, 4),
        "facility_diversity": diversity,
        "dist_to_center":     round(dist_to_center, 1),
        "gap_severity":       gap_severity,
    }


def detect_vacant_parcels(
    wards: List[Dict],
    buildings: List[Dict],
    grid_step_m: float = 150.0,
    radius_m: float = 400.0,
    n_candidates: int = 20,
    contamination: float = 0.12,
) -> List[Dict]:
    """
    ML-based vacant parcel detection using Isolation Forest.

    Approach:
    1. Create a grid of candidate cells across each ward
    2. For each cell inside the ward boundary, compute spatial features:
       - building_density: nearby buildings per sq km
       - avg_nearest_dist: mean distance to nearby buildings
       - min_nearest_dist: closest building distance
       - building_coverage: fraction of area covered by buildings
       - facility_diversity: number of distinct building categories
       - dist_to_center: distance to ward centroid
       - gap_severity: ward's demand-gap severity score
    3. Run Isolation Forest (unsupervised anomaly detection) across all cells
       to identify cells that are anomalously empty — potential vacant parcels
    4. Convert anomaly scores to 0–1 confidence
    5. Select top candidates per ward, avoiding overlap

    Returns list of candidate dicts with ML scores.
    """
    # Index buildings by ward
    ward_buildings_map: Dict[Any, List] = {w["ward_lgd_code"]: [] for w in wards}
    for b in buildings:
        wid = b.get("ward_lgd_code")
        if wid and wid in ward_buildings_map:
            ward_buildings_map[wid].append(b)

    all_cells = []
    cell_ward_map = []

    for w in wards:
        wid = w["ward_lgd_code"]
        wbuildings = ward_buildings_map.get(wid, [])
        ring = w["coords"][0] if w["coords"] else []
        if len(ring) < 3:
            continue

        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        min_lon, max_lon = min(lons), max(lons)
        min_lat, max_lat = min(lats), max(lats)

        # Convert grid_step from meters to approximate degrees
        lat_step = grid_step_m / 111_320.0
        lon_step = grid_step_m / (111_320.0 * math.cos(math.radians(w["centroid_lat"])))

        glat = min_lat
        while glat <= max_lat:
            glon = min_lon
            while glon <= max_lon:
                if _point_in_polygon(glon, glat, w["coords"]):
                    features = _compute_building_features(
                        glat, glon, wbuildings,
                        w["centroid_lat"], w["centroid_lon"],
                        w.get("gap_severity", 0), radius_m,
                    )
                    all_cells.append(features)
                    cell_ward_map.append({
                        "lat": glat, "lon": glon,
                        "ward": w,
                    })
                glon += lon_step
            glat += lat_step

    if not all_cells:
        return _fallback_simulation(wards, n_candidates)

    # Stack features into numpy array
    feature_keys = [
        "building_density", "avg_nearest_dist", "min_nearest_dist",
        "building_coverage", "facility_diversity", "dist_to_center", "gap_severity",
    ]
    X = np.array([[cell[k] for k in feature_keys] for cell in all_cells])

    # Standardize features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Isolation Forest: anomaly = potential vacant parcel (low building density)
    iso = IsolationForest(
        n_estimators=150,
        contamination=contamination,
        max_features=1.0,
        random_state=42,
        n_jobs=-1,
    )
    anomaly_labels = iso.fit_predict(X_scaled)   # -1 = anomaly (vacant), 1 = normal
    anomaly_scores = iso.decision_function(X_scaled)

    # Convert decision_function scores to 0–1 confidence
    # More negative = more anomalous = higher vacancy confidence
    score_min = anomaly_scores.min()
    score_max = anomaly_scores.max()
    score_range = score_max - score_min if score_max != score_min else 1.0
    vacancy_confidences = 1.0 - (anomaly_scores - score_min) / score_range

    # Build candidates from anomalous cells
    candidates = []
    for i, (cell_info, is_anomaly, confidence) in enumerate(
        zip(cell_ward_map, anomaly_labels, vacancy_confidences)
    ):
        if is_anomaly != -1:
            continue
        w = cell_info["ward"]
        # Approximate parcel area: grid cell area
        area_sqm = int(grid_step_m * grid_step_m)
        candidates.append({
            "centroid_lat":   round(cell_info["lat"], 6),
            "centroid_lon":   round(cell_info["lon"], 6),
            "area_sqm":       area_sqm,
            "confidence":     round(float(confidence), 2),
            "ward_lgd_code":  w["ward_lgd_code"],
            "ward_name":      w["ward_lgd_name"],
            "features":       all_cells[i],
        })

    # Sort by confidence (highest vacancy first)
    candidates.sort(key=lambda c: c["confidence"], reverse=True)

    # Select top candidates per ward, avoid overlapping (min 200m apart)
    selected = []
    ward_counts = {}
    for c in candidates:
        wid = c["ward_lgd_code"]
        ward_counts.setdefault(wid, 0)
        if ward_counts[wid] >= max(1, n_candidates // len(wards)):
            continue
        too_close = False
        for s in selected:
            if _haversine_m(c["centroid_lat"], c["centroid_lon"],
                           s["centroid_lat"], s["centroid_lon"]) < 200:
                too_close = True
                break
        if not too_close:
            ward_counts[wid] += 1
            selected.append(c)
        if len(selected) >= n_candidates:
            break

    # Finalise
    for i, c in enumerate(selected):
        c["candidate_id"] = f"ML-{i+1:03d}"
        c["method"] = "isolation_forest"
        c["note"] = "Detected via Isolation Forest anomaly detection on spatial features"
        c.pop("features", None)

    return selected if selected else _fallback_simulation(wards, n_candidates)


def _fallback_simulation(wards: List[Dict], n: int = 15) -> List[Dict]:
    """Fallback if ML detection produces no candidates."""
    rng = random.Random(42)
    candidates = []
    sorted_wards = sorted(wards, key=lambda w: w.get("gap_severity", 0), reverse=True)
    for i, w in enumerate(sorted_wards[:n]):
        lat = w["centroid_lat"] + rng.uniform(-0.003, 0.003)
        lon = w["centroid_lon"] + rng.uniform(-0.003, 0.003)
        area = rng.randint(600, 5000)
        confidence = round(rng.uniform(0.55, 0.92), 2)
        candidates.append({
            "candidate_id":   f"FALLBACK-{i+1:03d}",
            "centroid_lat":   round(lat, 6),
            "centroid_lon":   round(lon, 6),
            "area_sqm":       area,
            "confidence":     confidence,
            "ward_lgd_code":  w["ward_lgd_code"],
            "ward_name":      w["ward_lgd_name"],
            "method":         "simulation_fallback",
            "note":           "Fallback — ML detection found no candidates",
        })
    return candidates


# ─── Recommendation Engine (Layer 3) ─────────────────────────────────────────

OPTION_TEMPLATES = {
    "school": [
        {
            "icon": "🏫",
            "title": "Community Learning Hub",
            "description": "Multi-grade primary school + adult literacy centre + digital skills lab. Serves 600–800 students.",
            "area_sqm": 1800,
            "type": "school",
            "people_served": 750,
            "social_score": 88,
            "economic_score": 55,
            "sustainability_score": 72,
            "feasibility_score": 78,
        },
        {
            "icon": "📚",
            "title": "Ward Public Library + Study Hall",
            "description": "Public lending library, 3 reading rooms, broadband access, co-working desks for students.",
            "area_sqm": 600,
            "type": "library",
            "people_served": 400,
            "social_score": 75,
            "economic_score": 42,
            "sustainability_score": 80,
            "feasibility_score": 88,
        },
        {
            "icon": "🖥️",
            "title": "Vocational Training Centre",
            "description": "ITI-affiliated skill development centre: electronics, tailoring, culinary arts. 200 seats/batch.",
            "area_sqm": 1200,
            "type": "school",
            "people_served": 500,
            "social_score": 82,
            "economic_score": 78,
            "sustainability_score": 68,
            "feasibility_score": 72,
        },
    ],
    "clinic": [
        {
            "icon": "🏥",
            "title": "Primary Health Centre",
            "description": "24×7 PHC with 10 beds, OPD, diagnostic lab, and maternal health unit. Serves 3 wards.",
            "area_sqm": 800,
            "type": "clinic",
            "people_served": 900,
            "social_score": 95,
            "economic_score": 50,
            "sustainability_score": 75,
            "feasibility_score": 70,
        },
        {
            "icon": "💊",
            "title": "Urban Health & Wellness Clinic",
            "description": "Outpatient clinic + pharmacy + telemedicine pod. Low footprint, fast to operationalise.",
            "area_sqm": 400,
            "type": "clinic",
            "people_served": 450,
            "social_score": 80,
            "economic_score": 62,
            "sustainability_score": 78,
            "feasibility_score": 90,
        },
        {
            "icon": "🧪",
            "title": "Diagnostic & Screening Hub",
            "description": "Mobile-integrated diagnostics, blood bank satellite unit, cancer screening camp facility.",
            "area_sqm": 600,
            "type": "clinic",
            "people_served": 600,
            "social_score": 85,
            "economic_score": 58,
            "sustainability_score": 70,
            "feasibility_score": 76,
        },
    ],
    "recreation": [
        {
            "icon": "🌳",
            "title": "Pocket Park + Fitness Zone",
            "description": "Urban pocket park with open-air gym, walking track, play equipment, and green buffer.",
            "area_sqm": 2500,
            "type": "recreation",
            "people_served": 1200,
            "social_score": 78,
            "economic_score": 40,
            "sustainability_score": 92,
            "feasibility_score": 82,
        },
        {
            "icon": "⚽",
            "title": "Multi-Sport Turf & Indoor Hall",
            "description": "Synthetic turf football pitch + badminton/basketball hall + changing rooms.",
            "area_sqm": 3500,
            "type": "recreation",
            "people_served": 800,
            "social_score": 72,
            "economic_score": 65,
            "sustainability_score": 60,
            "feasibility_score": 68,
        },
        {
            "icon": "🎭",
            "title": "Cultural & Community Centre",
            "description": "Multipurpose hall, 300-seat auditorium, art gallery, and senior activity rooms.",
            "area_sqm": 2200,
            "type": "recreation",
            "people_served": 1000,
            "social_score": 84,
            "economic_score": 55,
            "sustainability_score": 72,
            "feasibility_score": 74,
        },
    ],
    "generic": [
        {
            "icon": "🏗️",
            "title": "Mixed-Use Development",
            "description": "Ground floor commercial units + upper floor affordable housing + rooftop solar + rain water harvesting.",
            "area_sqm": 2000,
            "type": "mixed",
            "people_served": 600,
            "social_score": 70,
            "economic_score": 80,
            "sustainability_score": 82,
            "feasibility_score": 72,
        },
        {
            "icon": "🌱",
            "title": "Urban Farming Hub",
            "description": "Rooftop + terrace hydroponic farming collective, weekly produce market, composting facility.",
            "area_sqm": 1200,
            "type": "recreation",
            "people_served": 300,
            "social_score": 72,
            "economic_score": 60,
            "sustainability_score": 96,
            "feasibility_score": 78,
        },
        {
            "icon": "🏢",
            "title": "Ward Office & Services Hub",
            "description": "Modern ward-level service centre, digital kiosks, document services, women's helpdesk.",
            "area_sqm": 900,
            "type": "public",
            "people_served": 2000,
            "social_score": 80,
            "economic_score": 48,
            "sustainability_score": 65,
            "feasibility_score": 85,
        },
    ],
}


def get_top_deficit_category(ward: Dict) -> str:
    """Returns the facility category with the largest deficit for a ward."""
    gaps = ward.get("demand_gap", {})
    deficits = {k: v for k, v in gaps.items() if v < 0}
    if not deficits:
        return "generic"
    top = min(deficits, key=lambda k: deficits[k])
    return top if top in OPTION_TEMPLATES else "generic"


def generate_options(ward: Dict) -> List[Dict]:
    """Generate three development options for a ward, targeting the biggest deficit."""
    top_cat = get_top_deficit_category(ward)
    templates = OPTION_TEMPLATES.get(top_cat, OPTION_TEMPLATES["generic"])
    if len(templates) < 3:
        templates = templates + OPTION_TEMPLATES["generic"][:3-len(templates)]
    return [dict(t) for t in templates[:3]]


def score_options(
    options: List[Dict],
    weights: Dict[str, float],
    ward: Dict,
    budget: float = 25e7,
) -> List[Dict]:
    """
    Weighted scoring of options.
    weights keys: social, economic, sustainability, feasibility (sum to 1.0)
    Also incorporates gap-addressed bonus and budget-fit penalty.
    """
    w_s  = weights.get("social",         0.3)
    w_e  = weights.get("economic",        0.25)
    w_su = weights.get("sustainability",  0.25)
    w_f  = weights.get("feasibility",     0.2)
    # Gap addressed bonus: how well does the option type address top deficit?
    top_cat = get_top_deficit_category(ward)
    for opt in options:
        gap_bonus = 15 if opt.get("type") == top_cat else 5
        # Cost estimate
        area = opt.get("area_sqm", 1000)
        opt["cost_estimate"] = area * COST_NEW_SQM
        opt["cost_crore"] = round(opt["cost_estimate"] / 1e7, 2)
        opt["cost_label"] = f"₹{opt['cost_crore']:.1f} Cr"
        # Budget fit: 1.0 if within budget, decays if over
        # At 2x budget → 0.5, at 5x+ → 0.1
        if budget > 0:
            ratio = budget / max(opt["cost_estimate"], 1)
            opt["budget_fit"] = round(min(1.0, max(0.1, ratio)), 2)
        else:
            opt["budget_fit"] = 0.1
        # Apply budget fit to weighted score
        raw = (
            w_s  * opt["social_score"] +
            w_e  * opt["economic_score"] +
            w_su * opt["sustainability_score"] +
            w_f  * opt["feasibility_score"] +
            gap_bonus
        )
        opt["total_score"] = min(100, round(raw * opt["budget_fit"]))
    options.sort(key=lambda o: o["total_score"], reverse=True)
    for i, opt in enumerate(options):
        opt["rank"] = i + 1
    return options


# ─── Impact Simulation (Layer 3 output) ───────────────────────────────────────

def simulate_impact(ward: Dict, top_option: Dict, budget: float = 25e7) -> List[Dict]:
    """Compute before/after impact metrics for the top-ranked option."""
    pop = ward.get("population", 1000) or 1000
    top_cat = top_option.get("type", "generic")
    served  = top_option.get("people_served", 500)
    cost_cr = top_option.get("cost_crore", 5.0)

    # Fall back to generic if the option type isn't a tracked facility category
    cat_key = top_cat if top_cat in FACILITY_CATEGORIES else "generic"
    before_ratio = ward.get("facility_per1000", {}).get(cat_key, 0)
    after_count  = ward.get("facility_counts", {}).get(cat_key, 0) + 1
    after_ratio  = round(after_count / pop * 1000, 3)
    gap_before   = ward.get("demand_gap", {}).get(cat_key, 0)
    gap_after    = round(gap_before + (after_ratio - before_ratio), 3)

    budget_cr = budget / 1e7
    remaining = budget_cr - cost_cr

    rows = [
        {
            "metric":    f"{FACILITY_ICONS.get(top_cat,'📍')} {FACILITY_LABELS.get(top_cat, top_cat)} / 1,000",
            "current":   f"{before_ratio:.3f}",
            "projected": f"{after_ratio:.3f}",
            "change":    f"+{after_ratio - before_ratio:.3f}",
            "positive":  True,
        },
        {
            "metric":    "📊 Demand Gap vs Corp Avg",
            "current":   f"{gap_before:+.3f}",
            "projected": f"{gap_after:+.3f}",
            "change":    f"{gap_after - gap_before:+.3f}",
            "positive":  gap_after > gap_before,
        },
        {
            "metric":    "👥 Direct Beneficiaries",
            "current":   "0",
            "projected": f"{served:,}",
            "change":    f"+{served:,}",
            "positive":  True,
        },
        {
            "metric":    "💰 Capital Investment",
            "current":   "—",
            "projected": f"₹{cost_cr:.1f} Cr",
            "change":    f"₹{cost_cr:.1f} Cr",
            "positive":  True,
        },
        {
            "metric":    "🏦 Budget Remaining",
            "current":   f"₹{budget_cr:.0f} Cr",
            "projected": f"₹{remaining:.1f} Cr",
            "change":    f"₹{remaining:.1f} Cr",
            "positive":  remaining >= 0,
        },
        {
            "metric":    "🏗️ Construction Area",
            "current":   "—",
            "projected": f"{top_option.get('area_sqm', 0):,} sqm",
            "change":    f"+{top_option.get('area_sqm', 0):,} sqm",
            "positive":  True,
        },
        {
            "metric":    "📅 Est. Build Time",
            "current":   "—",
            "projected": f"{max(12, int(cost_cr * 6))} months",
            "change":    "Greenfield",
            "positive":  True,
        },
    ]
    return rows


# ─── Budget Allocation ─────────────────────────────────────────────────────────

def compute_budget_allocation(option: Dict) -> List[Dict]:
    """Break down construction cost into line items."""
    total = option.get("cost_estimate", 0)
    if total == 0:
        return []
    return [
        {"label": "Civil & Structure",     "pct": 45, "amount": int(total * 0.45)},
        {"label": "MEP (Electrical/Plumbing)", "pct": 18, "amount": int(total * 0.18)},
        {"label": "Finishes & Interiors",  "pct": 15, "amount": int(total * 0.15)},
        {"label": "Landscape & External",  "pct": 8,  "amount": int(total * 0.08)},
        {"label": "Furniture & Equipment", "pct": 7,  "amount": int(total * 0.07)},
        {"label": "Contingency (7%)",      "pct": 7,  "amount": int(total * 0.07)},
    ]


# ─── LLM Narration (Layer 5 — template-based without LLM) ─────────────────────

def generate_narration(ward: Dict, top_option: Dict, corp_avg: Dict, budget: float = 25e7) -> str:
    """
    Generates a plain-language explanation of the recommendation.
    Uses computed numbers only — no hallucination.
    """
    name     = ward.get("ward_lgd_name", "this ward")
    pop      = ward.get("population", 0)
    top_cat  = top_option.get("type", "generic")
    cat_label = FACILITY_LABELS.get(top_cat, top_cat)
    gap      = ward.get("demand_gap", {}).get(top_cat, 0)
    ward_ratio = ward.get("facility_per1000", {}).get(top_cat, 0)
    corp_ratio = corp_avg.get(top_cat, 0)
    cost_cr  = top_option.get("cost_crore", 0)
    served   = top_option.get("people_served", 0)
    score    = top_option.get("total_score", 0)
    title    = top_option.get("title", "")
    bf       = top_option.get("budget_fit", 1.0)
    budget_cr = budget / 1e7

    gap_str = f"{abs(gap):.3f} per 1,000 residents below the corporation average"
    direction = "deficit" if gap < 0 else "surplus"

    if bf >= 1.0:
        budget_note = f"The estimated cost of ₹{cost_cr:.1f} crore falls **within** the available budget of ₹{budget_cr:.0f} crore."
    else:
        over_pct = (1 - bf) * 100
        budget_note = f"The estimated cost of ₹{cost_cr:.1f} crore **exceeds** the available budget of ₹{budget_cr:.0f} crore by approximately {over_pct:.0f}%, which reduced its weighted score."

    narration = (
        f"**{name}** has an estimated population of **{pop:,}** (density-based estimate from 2011 census). "
        f"For {cat_label.lower()}, this ward currently shows **{ward_ratio:.3f} facilities per 1,000 residents**, "
        f"compared to the corporation-wide average of **{corp_ratio:.4f}**. "
        f"That is a **{direction} of {gap_str}**. "
        f"\n\nBased on the current priority weights, the top-ranked intervention is the **{title}**, "
        f"with a budget-adjusted weighted score of **{score}/100**. This option is projected to directly serve "
        f"**{served:,} residents** and requires an estimated capital investment of **₹{cost_cr:.1f} crore** "
        f"(composite market rate; not quantity-surveyed). "
        f"\n\n{budget_note} "
        f"\n\n*This narration reflects computed values only. All figures are estimates based on available open data.*"
    )
    return narration


# ─── LLM Narration (Gemini-powered) ───────────────────────────────────────────

def generate_narration_llm(
    ward: Dict, top_option: Dict, corp_avg: Dict, budget: float = 25e7,
) -> Tuple[str, bool]:
    """
    Generates narration using Google Gemini LLM.
    Returns (narration_text, used_llm: bool).
    Falls back to template-based narration only if API call fails.
    """
    api_key = GEMINI_API_KEY
    if not api_key or not GEMINI_AVAILABLE:
        return generate_narration(ward, top_option, corp_avg, budget), False

    try:
        client = genai.Client(api_key=api_key)
        model = "gemini-flash-lite-latest"

        name      = ward.get("ward_lgd_name", "this ward")
        pop       = ward.get("population", 0)
        area      = ward.get("area_sqkm", 0)
        density   = ward.get("density_per_sqkm", 0)
        top_cat   = top_option.get("type", "generic")
        cat_label = FACILITY_LABELS.get(top_cat, top_cat)
        gap       = ward.get("demand_gap", {}).get(top_cat, 0)
        ward_ratio = ward.get("facility_per1000", {}).get(top_cat, 0)
        corp_ratio = corp_avg.get(top_cat, 0)
        cost_cr   = top_option.get("cost_crore", 0)
        served    = top_option.get("people_served", 0)
        score     = top_option.get("total_score", 0)
        title     = top_option.get("title", "")
        bf        = top_option.get("budget_fit", 1.0)
        budget_cr = budget / 1e7
        counts    = ward.get("facility_counts", {})

        facility_summary = ", ".join(
            f"{FACILITY_LABELS.get(c, c)}: {counts.get(c, 0)}"
            for c in FACILITY_CATEGORIES
        )

        prompt = f"""You are an urban planning analyst for Kochi Municipal Corporation, Kerala, India.
Write a clear, professional narration (3-4 paragraphs) for a municipal planner explaining
the development recommendation for the ward described below.

WARD DATA:
- Name: {name}
- Population: {pop:,} (density-estimated from 2011 census)
- Area: {area:.2f} sq km
- Density: {density:.0f} per sq km
- Facility counts: {facility_summary}

DEMAND ANALYSIS:
- {cat_label} ratio in ward: {ward_ratio:.3f} per 1,000 residents
- Corporation average: {corp_ratio:.4f} per 1,000 residents
- Gap: {gap:+.3f} ({'deficit' if gap < 0 else 'surplus'})

RECOMMENDATION:
- Project: {title}
- Description: {top_option.get('description', '')}
- Budget-adjusted score: {score}/100
- Estimated cost: ₹{cost_cr:.1f} crore
- Projected beneficiaries: {served:,}
- Available budget: ₹{budget_cr:.0f} crore
- Budget fit: {'within budget' if bf >= 1.0 else f'over budget by {(1-bf)*100:.0f}%'}

INSTRUCTIONS:
1. Start with the ward name and its current situation
2. Explain the demand gap in plain language
3. Present the recommended project and why it was ranked #1
4. Mention the budget status and cost-benefit
5. End with projected impact on the community
6. Use markdown formatting (**bold** for key numbers)
7. Keep it professional but accessible — written for elected officials, not engineers
8. Do NOT make up any numbers. Use ONLY the data provided above.
 9. Do NOT exceed 4 paragraphs."""

        narration = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                narration = response.text.strip() if response.text else ""
                if narration:
                    break
            except Exception as retry_err:
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))
                    continue
                raise retry_err

        if not narration:
            return generate_narration(ward, top_option, corp_avg, budget), False
        return narration, True

    except Exception as e:
        print(f"Gemini API error: {e}")
        return generate_narration(ward, top_option, corp_avg, budget), False


# ─── Full Pipeline (cached) ───────────────────────────────────────────────────

_CACHE: Dict = {}


def load_all_data(force: bool = False) -> Dict:
    """
    Loads and joins all datasets. Results are cached in memory.
    Returns a dict with all processed data ready for the app.
    """
    global _CACHE
    if _CACHE and not force:
        return _CACHE

    print("Loading ward boundaries...")
    wards = load_wards()

    print("Loading census data...")
    census = load_census_ward_population()

    print("Estimating ward populations...")
    wards = estimate_ward_populations(wards, census)

    print("Loading buildings...")
    buildings = load_buildings()

    print("Spatial joining buildings to wards...")
    buildings = spatial_join_buildings_to_wards(buildings, wards)

    print("Loading reviews...")
    reviews = load_reviews()

    # Merge reviews onto buildings
    for b in buildings:
        rv = reviews.get(b["building_id"], {})
        b["avg_rating"]         = rv.get("avg_rating", None)
        b["sentiment_summary"]  = rv.get("sentiment_summary", None)
        b["review_source"]      = rv.get("review_source", "")

    print("Computing demand gaps...")
    wards, corp_avg = compute_demand_gaps(wards, buildings)

    print("Loading ecostat...")
    ecostat = load_ecostat()

    print("Detecting vacant parcels (Isolation Forest)...")
    vacant_candidates = detect_vacant_parcels(wards, buildings)

    # Build ward lookup dict
    ward_lookup = {w["ward_lgd_code"]: w for w in wards}

    _CACHE = {
        "wards":             wards,
        "ward_lookup":       ward_lookup,
        "buildings":         buildings,
        "reviews":           reviews,
        "ecostat":           ecostat,
        "corp_avg":          corp_avg,
        "census":            census,
        "vacant_candidates": vacant_candidates,
        "COST_NEW_SQM":      COST_NEW_SQM,
        "COST_RENOV_SQM":    COST_RENOV_SQM,
        "COST_NOTE":         COST_NOTE,
    }
    print("Data pipeline complete.")
    return _CACHE


if __name__ == "__main__":
    # Quick sanity check
    data = load_all_data()
    print(f"\n=== Pipeline Summary ===")
    print(f"Wards loaded:          {len(data['wards'])}")
    print(f"Buildings loaded:      {len(data['buildings'])}")
    print(f"Reviews loaded:        {len(data['reviews'])}")
    print(f"Ecostat materials:     {len(data['ecostat']['materials'])}")
    print(f"Corp avg (school):     {data['corp_avg'].get('school', 0):.4f}/1000")
    print(f"Vacant candidates:     {len(data['vacant_candidates'])}")
    w0 = data["wards"][0]
    print(f"\nSample ward: {w0['ward_lgd_name']}")
    print(f"  Population: {w0['population']:,}")
    print(f"  Area:       {w0['area_sqkm']:.2f} sqkm")
    print(f"  Gap score:  {w0['gap_severity']}")
