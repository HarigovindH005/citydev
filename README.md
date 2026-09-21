# CITYDEV — Urban Intelligence for Kochi Municipal Corporation

**Data-driven decision support for urban planning in Kochi, Kerala.**

CITYDEV is an interactive, open-source decision-support tool built for the **Kochi Municipal Corporation** to help urban planners, municipal officials, and elected representatives evaluate facility gaps, simulate development impact, and optimize budget allocation across the city's 74 wards.

The system ingests **open geospatial data** (OpenStreetMap, Census of India, Kerala Ecostat, and LGD ward boundaries) and applies machine learning (Isolation Forest) to detect vacant parcels, demand-gap analytics to identify underserved facilities, and a weighted recommendation engine to rank development options per ward.

---

## Features

| Module | Description |
|--------|-------------|
| **Demand-Gap Analytics** | Computes per-1,000-resident facility ratios for schools, clinics, parks, libraries, commercial spaces, and public buildings — benchmarked against corporation averages. |
| **AI-Ranked Recommendations** | Generates three development options per ward, scored by weighted criteria: Social Impact, Economic Viability, Sustainability, and Feasibility. |
| **Impact Simulation** | Projects before/after outcomes for the selected option: facility ratios, population served, budget remaining, and construction timeline. |
| **Vacant-Space Detection** | Uses **Isolation Forest** unsupervised ML to detect spatially anomalous empty parcels across all 74 wards — no manual labeling required. |
| **Adaptive Reuse Mode** | Identifies underused existing buildings suitable for conversion, calculating capital savings vs. new construction. |
| **LLM-Powered Narration** | Generates professional plain-language narratives of recommendations via Google Gemini (optional, falls back to template-based narration). |
| **Interactive Maps** | Folium-based ward and building maps with color-coded gap severity, facility categories, and ML-detected vacant parcels. |
| **Budget Allocation** | Breaks down capital costs into line items (civil, MEP, finishes, landscape, furniture, contingency) using Kerala Ecostat Q4 2025-26 pricing. |

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| **Frontend** | Streamlit, Folium, Streamlit-Folium |
| **Analytics** | Scikit-learn (Isolation Forest, StandardScaler), NumPy |
| **Geospatial** | Pure Python (no geopandas dependency) — custom centroid, area, and point-in-polygon algorithms |
| **LLM (optional)** | google-genai |
| **Data Sources** | OpenStreetMap (ODbL), Census of India 2011, Kerala Ecostat Q4 2025-26, LGD Ward Boundaries |

---

## Project Structure

```
citydevfinal/
├── app.py                    # Main Streamlit application (UI + orchestration)
├── data_pipeline.py          # Data loading, preprocessing, ML, and analytics engine
├── requirements.txt          # Python dependencies
├── styles.css                # Custom CSS styling for the Streamlit UI
├── EVALUATION_DOCS.md        # Evaluation and technical documentation
├── data/
│   ├── kochi_ward_boundary.json          # 74-ward boundary GeoJSON (LGD)
│   ├── kochi_ward_boundaries.geojson     # Simplified ward boundaries
│   ├── kochi_building_kaloor_vytilla_normal.json  # OSM building footprints
│   ├── kochi_building_review_dataset_17670.csv    # Building reviews (synthetic)
│   ├── ecostat_converted.json            # Kerala Ecostat Q4 2025-26 material prices
│   └── DDW_PCA3208_2011_MDDS_with_UI.json  # Census 2011 ward-level population data
└── .gitignore
```

---

## Installation & Setup

### Prerequisites
- Python 3.9+
- pip package manager

### Steps

```bash
# Clone the repository
git clone https://github.com/HarigovindH005/citydev.git
cd citydev

# Install dependencies
pip install -r requirements.txt

# (Optional) Set your Gemini API key as an environment variable
# export GEMINI_API_KEY="your-api-key-here"

# Run the application
streamlit run app.py
```

### Using with Gemini LLM (recommended for AI narration)

```bash
export GEMINI_API_KEY="your-gemini-api-key"
streamlit run app.py
```

If no API key is set, the app falls back to template-based narration (no functionality loss).

---

## Usage Guide

### 1. Select a Ward
Choose any of Kochi Municipal Corporation's 74 wards from the sidebar dropdown.

### 2. Choose a Planning Mode
- **🏗️ BUILD**: Focus on new construction options
- **♻️ REUSE**: Identify existing buildings for adaptive reuse

### 3. Adjust Priority Weights
Drag the sliders in the sidebar to re-rank recommendations:
- Social Impact (default: 30%)
- Economic Viability (default: 25%)
- Sustainability (default: 25%)
- Feasibility (default: 20%)

### 4. Set Budget
Define the available budget (in ₹ crores) to see how budget constraints affect option rankings.

### 5. Review Results
- **Demand-Gap Profile**: See which facility types are over/under-supplied
- **Ranked Options**: Three development options with weighted scores
- **AI Narration**: Plain-language explanation of the top recommendation
- **Impact Simulation**: Before/after projections
- **Vacant Parcels**: ML-detected candidate sites in the selected ward

---

## Data Transparency

The data used in CITYDEV has the following limitations, clearly documented in the UI:

- **Population**: Estimated via density-based redistribution across redrawn ward boundaries (Census 2011 ÷ current 74 wards, with a 5% growth factor)
- **Building Reviews**: Synthetic data generated from OSM attributes — not real public sentiment
- **Amenity Counts**: Derived from OSM building tags, which undercounts actual amenity presence
- **Construction Costs**: Based on a cited market composite rate (₹22,600/sqm new, ₹7,900/sqm renovation), cross-checked against Kerala Ecostat Q4 2025-26 — not a quantity-surveyed estimate
- **Vacant Parcel Detection**: Uses Isolation Forest on spatial features; production version uses a deep learning model with weak supervision from OSM + Bhuvan labels

---

## API Reference

### `data_pipeline.py` — Key Functions

| Function | Description |
|----------|-------------|
| `load_all_data()` | Loads and joins all datasets, returning a cached dict |
| `compute_demand_gaps()` | Computes per-ward facility ratios vs. corporation averages |
| `detect_vacant_parcels()` | Isolation Forest-based vacant parcel detection |
| `generate_options()` | Generates 3 development options for a ward |
| `score_options()` | Weighted scoring with budget-fit penalty |
| `simulate_impact()` | Before/after impact simulation for top option |
| `generate_narration_llm()` | LLM-powered narration (falls back to template) |

### `app.py` — UI Modules

| Section | Pages/Features |
|---------|---------------|
| Landing Screen | Corporation-wide map (74 wards, color-coded by gap severity) |
| Ward Overview | Ward-level map, metrics, facility inventory with reviews |
| Demand-Gap Profile | Gap cards + detailed comparison table |
| Recommendations | Ranked option cards with scoring breakdown |
| Budget Allocation | Line-item cost breakdown |
| Impact Simulation | Before/after metric table |
| Reuse Mode | Underused building conversion candidates |
| Vacant Detection | ML-detected parcels table with confidence scores |

---

## Cost Model

| Cost Type | Rate | Source |
|-----------|------|--------|
| New Construction | ₹22,600 / sqm | 2026 Kerala market composite |
| Renovation (35% of new) | ₹7,900 / sqm | Industry retrofit ratio assumption |
| Cement (53-grade, 50kg) | ₹350 / bag | Kerala Ecostat EKM Q4 2025-26 |
| TMT Steel (12mm) | ₹57,500 / MT | Kerala Ecostat EKM Q4 2025-26 |
| Bricks (Class A) | ₹11,000 / 1000 | Kerala Ecostat EKM Q4 2025-26 |
| Mason Wage | ₹1,200 / day | Kerala Ecostat EKM Q4 2025-26 |

---

## Evaluation Criteria

Options are scored using a weighted sum model:

| Criterion | Weight (default) | Component |
|-----------|-----------------|-----------|
| Social Impact | 30% | Beneficiaries, community value |
| Economic Viability | 25% | Cost-effectiveness, ROI |
| Sustainability | 25% | Green features, long-term viability |
| Feasibility | 20% | Implementation ease, timeline |
| Gap-Addressed Bonus | — | +15 if matches top deficit, +5 otherwise |
| Budget Fit Penalty | — | Score scaled by `min(1.0, budget/cost)` |

---

## License

This project is open-source and intended for public sector urban planning use. Data sources retain their original licenses (ODbL for OSM, public domain for Census).

---

## Credits

- **Data**: [OpenStreetMap](https://www.openstreetmap.org), [Census of India](https://censusindia.gov.in), [Kerala Ecostat](https://ecostat.kerala.gov.in), [LGD](https://lgdirectory.gov.in), [Bhuvan ISRO](https://bhuvan.nrsc.gov.in)
- **Built with**: Streamlit, Scikit-learn, Folium, NumPy
- **Developers**: [Harigovind H](https://github.com/HarigovindH005)

---

For questions, feedback, or contributions, please [open an issue](https://github.com/HarigovindH005/citydev/issues).
