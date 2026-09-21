"""
CITYDEV — Main Streamlit Application (app.py)
Decision-support system for Kochi Corporation urban planning.
"""

import sys
import os
import json
import streamlit as st
import folium
from streamlit_folium import st_folium

# Add current dir to path
sys.path.insert(0, os.path.dirname(__file__))

from data_pipeline import (
    load_all_data, generate_options, score_options,
    simulate_impact, compute_budget_allocation,
    generate_narration, generate_narration_llm,
    FACILITY_CATEGORIES, FACILITY_LABELS, FACILITY_ICONS,
    FACILITY_TARGETS_PER_1000, COST_NOTE,
)

# ─── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CITYDEV — Kochi Urban Intelligence",
    page_icon="🕸️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Inject CSS ───────────────────────────────────────────────────────────────
CSS_PATH = os.path.join(os.path.dirname(__file__), "styles.css")
with open(CSS_PATH, encoding="utf-8") as f:
    css = f.read()

st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)

# ─── Inject JS ────────────────────────────────────────────────────────────────
JS = """
<script>
(function() {
  // Animated count-up for metric numbers
  function countUp(el, target, duration) {
    var start = 0;
    var step = target / (duration / 16);
    var timer = setInterval(function() {
      start += step;
      if (start >= target) { start = target; clearInterval(timer); }
      el.textContent = Math.round(start).toLocaleString();
    }, 16);
  }
  function initCountUps() {
    document.querySelectorAll('[data-countup]').forEach(function(el) {
      var target = parseFloat(el.getAttribute('data-countup'));
      if (!isNaN(target)) countUp(el, target, 1000);
    });
  }
  // Option card hover highlight
  function initOptionCards() {
    document.querySelectorAll('.option').forEach(function(card) {
      card.addEventListener('mouseenter', function() {
        document.querySelectorAll('.option').forEach(function(c) {
          c.style.opacity = '0.7';
        });
        card.style.opacity = '1';
      });
      card.addEventListener('mouseleave', function() {
        document.querySelectorAll('.option').forEach(function(c) {
          c.style.opacity = '1';
        });
      });
    });
  }
  // Run after DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      initCountUps(); initOptionCards();
    });
  } else {
    initCountUps(); initOptionCards();
  }
  // Also re-run on Streamlit re-renders
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'streamlit:render') {
      setTimeout(function() { initCountUps(); initOptionCards(); }, 300);
    }
  });
})();
</script>
"""
st.components.v1.html(JS, height=0)

# ─── Session State Defaults ───────────────────────────────────────────────────
if "mode" not in st.session_state:
    st.session_state.mode = None           # "BUILD" or "REUSE"
if "selected_ward" not in st.session_state:
    st.session_state.selected_ward = None
if "selected_option_idx" not in st.session_state:
    st.session_state.selected_option_idx = 0
if "data_loaded" not in st.session_state:
    st.session_state.data_loaded = False


# ─── Load Data (with spinner) ─────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def get_data():
    return load_all_data()


with st.spinner("🕸️ Loading CITYDEV data pipeline…"):
    DATA = get_data()

WARDS        = DATA["wards"]
WARD_LOOKUP  = DATA["ward_lookup"]
BUILDINGS    = DATA["buildings"]
ECOSTAT      = DATA["ecostat"]
CORP_AVG     = DATA["corp_avg"]
CENSUS       = DATA["census"]
VACANT       = DATA["vacant_candidates"]


# ─── Helper: fmt number ───────────────────────────────────────────────────────
def fmt(n, decimals=0):
    if n is None:
        return "—"
    if decimals == 0:
        return f"{int(n):,}"
    return f"{n:,.{decimals}f}"


def pct_bar(value, max_val=100, color="var(--accent)"):
    pct = min(100, max(0, (value / max_val * 100) if max_val else 0))
    return f'<div class="bar"><i style="width:{pct:.1f}%;background:{color}"></i></div>'


# ─── SIDEBAR ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        '<div class="side-brand">🕸️ CITYDEV<small>Urban Intelligence · Kochi Corp</small></div>',
        unsafe_allow_html=True,
    )

    # Mode selector
    st.markdown("**Planning Mode**")
    mode_choice = st.radio(
        "mode", ["🏗️ BUILD — New Development", "♻️ REUSE — Convert Existing"],
        label_visibility="collapsed",
    )
    mode = "BUILD" if "BUILD" in mode_choice else "REUSE"
    st.session_state.mode = mode

    st.divider()

    # Ward selector
    ward_names = sorted([w["ward_lgd_name"] for w in WARDS])
    selected_name = st.selectbox(
        "Select Ward",
        ["— Select a ward —"] + ward_names,
        help="Choose a ward to analyze its demand profile and generate recommendations.",
    )

    if selected_name != "— Select a ward —":
        match = next((w for w in WARDS if w["ward_lgd_name"] == selected_name), None)
        st.session_state.selected_ward = match
    else:
        st.session_state.selected_ward = None

    st.divider()

    # Priority weights
    st.markdown("**Priority Weights** *(drag to re-rank options)*")
    w_social  = st.slider("🤝 Social Impact",      0, 100, 30, 5, key="w_social")
    w_econ    = st.slider("💰 Economic Viability",  0, 100, 25, 5, key="w_econ")
    w_sustain = st.slider("🌱 Sustainability",       0, 100, 25, 5, key="w_sustain")
    w_feas    = st.slider("⚙️ Feasibility",          0, 100, 20, 5, key="w_feas")

    total_w = w_social + w_econ + w_sustain + w_feas
    if total_w == 0:
        total_w = 100
    weights = {
        "social":        w_social / total_w,
        "economic":      w_econ / total_w,
        "sustainability": w_sustain / total_w,
        "feasibility":   w_feas / total_w,
    }

    st.divider()

    st.divider()

    # Budget input
    st.markdown("**Project Budget**")
    budget_cr = st.slider(
        "Available Budget (₹ Cr)",
        min_value=1, max_value=100, value=25, step=1,
        key="budget_cr",
        help="Total available budget in crores. Options exceeding this budget are penalized in scoring.",
    )
    budget_value = budget_cr * 1e7  # Convert to ₹

    st.divider()

    # Vacant candidates toggle
    show_vacant = st.checkbox("🛰️ Show ML-Detected Vacant Parcels", value=True, key="show_vacant")

    # Data notes
    st.markdown(
        """
        <div class="side-note">
        <strong>⚠ Data Transparency</strong><br>
        Population figures are <strong>estimated</strong> — density-based redistribution across redrawn ward boundaries (2011 census ÷ current 74 wards).<br><br>
        Building reviews are <strong>synthetic placeholder data</strong> from OSM attributes — not real public sentiment.<br><br>
        Amenity counts use OSM building tags, which <strong>undercount</strong> true amenity presence.<br><br>
        Construction costs use a <strong>cited market composite rate</strong> (₹22,600/sqm), cross-checked against Kerala Ecostat Q4 2025-26.<br><br>
        Vacant parcel detection uses <strong>simulation fallback</strong> — production uses DL model with weak supervision from OSM+Bhuvan labels.
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─── LANDING SCREEN ───────────────────────────────────────────────────────────
if st.session_state.selected_ward is None:

    st.markdown(
        """
        <div class="landing fade-in">
          <div class="landing-icon">🕸️</div>
          <h1 class="landing-title">CITY<span>DEV</span></h1>
          <p class="landing-sub">Urban Intelligence for Kochi Municipal Corporation</p>
          <p class="landing-text">
            Select a ward from the sidebar to analyze demand gaps, generate ranked
            development options, and simulate before/after community impact — all
            from open geospatial data.
          </p>
          <div class="landing-features">
            <span class="pill">📊 Demand-Gap Analytics</span>
            <span class="pill">🏗️ AI-Ranked Options</span>
            <span class="pill">🎛️ Live Re-Optimization</span>
            <span class="pill">🛰️ Vacant-Space Detection</span>
            <span class="pill">📈 Impact Simulation</span>
            <span class="pill">🤖 LLM Narration</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Corporation-wide overview map
    st.markdown("### 🗺️ Kochi Corporation — All 74 Wards")

    m = folium.Map(
        location=[9.9312, 76.2673],
        zoom_start=12,
        tiles="CartoDB dark_matter",
    )

    # Plot wards coloured by gap severity
    for w in WARDS:
        ring = w["coords"][0] if w["coords"] else []
        if len(ring) < 3:
            continue
        severity = w.get("gap_severity", 0)
        # Color: green (low gap) → red (high gap)
        r = min(255, int(severity * 2.55))
        g = min(255, int((100 - severity) * 1.5))
        color = f"#{r:02x}{g:02x}50"
        folium.Polygon(
            locations=[(pt[1], pt[0]) for pt in ring],
            color="#203844",
            weight=1,
            fill=True,
            fill_color=color,
            fill_opacity=0.45,
            tooltip=f"<b>{w['ward_lgd_name']}</b><br>Pop: {fmt(w.get('population',0))}<br>Gap Score: {w.get('gap_severity',0)}",
        ).add_to(m)

    if show_vacant:
        for vc in VACANT[:8]:
            folium.CircleMarker(
                location=[vc["centroid_lat"], vc["centroid_lon"]],
                radius=6,
                color="#61df73",
                fill=True,
                fill_color="#61df73",
                fill_opacity=0.7,
                tooltip=f"<b>ML-Detected Vacant</b><br>{vc['ward_name']}<br>~{vc['area_sqm']} sqm<br>Confidence: {vc['confidence']:.0%}<br>Method: {vc.get('method','unknown')}",
            ).add_to(m)

    st_folium(m, width=None, height=480, returned_objects=[])

    st.stop()


# ─── SELECTED WARD — Top Header ───────────────────────────────────────────────
ward = st.session_state.selected_ward

st.markdown(
    f"""
    <div class="top-header fade-in">
      <div class="brand-section">
        <span class="brand-icon">🕸️</span>
        <div>
          <div class="brand-name">CITYDEV</div>
          <small class="brand-subtitle">Kochi Urban Intelligence</small>
        </div>
      </div>
      <div class="location-info">
        <div class="loc-name">📍 {ward['ward_lgd_name']}</div>
        <div class="loc-coords">{ward['centroid_lat']:.4f}°N, {ward['centroid_lon']:.4f}°E</div>
      </div>
      <span class="badge badge-green">{'🏗️ BUILD' if mode == 'BUILD' else '♻️ REUSE'} MODE</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# ─── SECTION 1 — Ward Overview Map ───────────────────────────────────────────
st.markdown("## 🗺️ Ward Overview")

col_map, col_stats = st.columns([3, 2])

with col_map:
    m2 = folium.Map(
        location=[ward["centroid_lat"], ward["centroid_lon"]],
        zoom_start=14,
        tiles="CartoDB dark_matter",
    )
    # Highlight selected ward
    ring = ward["coords"][0] if ward["coords"] else []
    if len(ring) >= 3:
        folium.Polygon(
            locations=[(pt[1], pt[0]) for pt in ring],
            color="#61df73",
            weight=2.5,
            fill=True,
            fill_color="#61df73",
            fill_opacity=0.15,
            tooltip=ward["ward_lgd_name"],
        ).add_to(m2)

    # Plot ward buildings
    ward_buildings = [b for b in BUILDINGS if b.get("ward_lgd_code") == ward["ward_lgd_code"]]
    cat_colors = {
        "school":      "#3a9bd5",
        "clinic":      "#ff5b5b",
        "recreation":  "#61df73",
        "library":     "#f5c842",
        "commercial":  "#9b59b6",
        "public":      "#e67e22",
        "residential": "#728792",
        "generic":     "#4a5568",
    }
    for b in ward_buildings[:500]:  # limit for perf
        cat = b.get("category", "generic")
        color = cat_colors.get(cat, "#4a5568")
        folium.CircleMarker(
            location=[b["centroid_lat"], b["centroid_lon"]],
            radius=3,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.7,
            tooltip=f"{b.get('name','') or b['building_tag']} ({cat})",
        ).add_to(m2)

    # Vacant candidates in this ward
    if show_vacant:
        ward_vacant = [v for v in VACANT if v["ward_lgd_code"] == ward["ward_lgd_code"]]
        for vc in ward_vacant:
            folium.CircleMarker(
                location=[vc["centroid_lat"], vc["centroid_lon"]],
                radius=8,
                color="#61df73",
                fill=True,
                fill_color="#61df73",
                fill_opacity=0.5,
                tooltip=f"<b>ML-Detected Vacant</b><br>~{vc['area_sqm']} sqm<br>Confidence: {vc['confidence']:.0%}<br>Method: {vc.get('method','unknown')}",
            ).add_to(m2)

    st_folium(m2, width=None, height=380, returned_objects=[])

with col_stats:
    pop  = ward.get("population", 0)
    area = ward.get("area_sqkm", 0)
    dens = ward.get("density_per_sqkm", 0)
    n_b  = ward.get("total_buildings", 0)

    st.markdown(
        f"""
        <div class="metrics-row">
          <div class="metric-card">
            <div class="small-label">Population</div>
            <div class="metric accent" data-countup="{pop}">{fmt(pop)}</div>
            <div class="metric-sub">est. 2011+5%</div>
          </div>
          <div class="metric-card">
            <div class="small-label">Area</div>
            <div class="metric">{fmt(area, 2)}</div>
            <div class="metric-sub">sq km</div>
          </div>
        </div>
        <div class="metrics-row">
          <div class="metric-card">
            <div class="small-label">Density</div>
            <div class="metric">{fmt(dens)}</div>
            <div class="metric-sub">per sq km</div>
          </div>
          <div class="metric-card">
            <div class="small-label">Buildings</div>
            <div class="metric">{fmt(n_b)}</div>
            <div class="metric-sub">OSM footprints</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Amenity breakdown — expandable with building reviews
    counts = ward.get("facility_counts", {})
    reviews_data = DATA.get("reviews", {})

    st.markdown(
        '<div class="panel"><div class="panel-title">Facility Inventory</div>'
        '<div style="font-size:11px;color:var(--muted);margin-bottom:12px;">Click a category to view individual buildings and their reviews</div></div>',
        unsafe_allow_html=True,
    )

    for cat in FACILITY_CATEGORIES:
        cnt = counts.get(cat, 0)
        with st.expander(f"{FACILITY_ICONS[cat]} {FACILITY_LABELS[cat]} — {cnt} buildings", expanded=False):
            cat_buildings = [b for b in ward_buildings if b.get("category") == cat]
            if not cat_buildings:
                st.info(f"No {FACILITY_LABELS[cat].lower()} found in this ward.")
            else:
                # Summary stats
                with_reviews = [b for b in cat_buildings if b.get("avg_rating") is not None]
                if with_reviews:
                    avg_r = sum(b["avg_rating"] for b in with_reviews) / len(with_reviews)
                    sentiments = {}
                    for b in with_reviews:
                        s = b.get("sentiment_summary", "neutral")
                        sentiments[s] = sentiments.get(s, 0) + 1
                    pos = sentiments.get("positive", 0)
                    neu = sentiments.get("neutral", 0)
                    neg = sentiments.get("negative", 0)
                    st.markdown(
                        f'<div style="display:flex;gap:16px;margin-bottom:12px;font-size:12px;color:var(--muted2);">'
                        f'<span>⭐ Avg Rating: <b style="color:var(--yellow);">{avg_r:.1f}/5</b></span>'
                        f'<span>🟢 {pos} positive</span>'
                        f'<span>🟡 {neu} neutral</span>'
                        f'<span>🔴 {neg} negative</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                # Table header
                st.markdown(
                    '<div style="display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:6px;padding:8px 10px;'
                    'background:rgba(32,56,68,0.6);border-radius:6px 6px 0 0;font-size:11px;color:var(--muted);'
                    'text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">'
                    '<span>Building Name</span><span>Type</span><span>Rating</span><span>Sentiment</span><span>Reviews</span></div>',
                    unsafe_allow_html=True,
                )

                # Building rows
                display_buildings = cat_buildings[:50]
                for b in display_buildings:
                    b_id = b.get("building_id", "")
                    rv = reviews_data.get(b_id, {})
                    name = b.get("name") or rv.get("building_name") or f"Building ({b.get('building_tag', 'unknown')})"
                    b_type = b.get("building_tag", "—")
                    avg_r = b.get("avg_rating") or rv.get("avg_rating")
                    sentiment = b.get("sentiment_summary") or rv.get("sentiment_summary", "—")
                    num_reviews = len(rv.get("ratings", [])) if rv else 0

                    # Rating display
                    if avg_r is not None:
                        if avg_r >= 4:
                            r_cls, r_color = "pos", "var(--accent)"
                        elif avg_r >= 3:
                            r_cls, r_color = "", "var(--yellow)"
                        else:
                            r_cls, r_color = "neg", "var(--red)"
                        rating_html = f'<span style="color:{r_color};font-weight:600;">⭐ {avg_r:.1f}</span>'
                    else:
                        rating_html = '<span style="color:var(--muted);">—</span>'

                    # Sentiment badge
                    if sentiment == "positive":
                        sent_html = '<span style="background:rgba(97,223,115,0.12);border:1px solid rgba(97,223,115,0.3);color:var(--accent);padding:2px 8px;border-radius:4px;font-size:11px;">Positive</span>'
                    elif sentiment == "negative":
                        sent_html = '<span style="background:rgba(255,91,91,0.12);border:1px solid rgba(255,91,91,0.3);color:var(--red);padding:2px 8px;border-radius:4px;font-size:11px;">Negative</span>'
                    elif sentiment == "neutral":
                        sent_html = '<span style="background:rgba(245,200,66,0.12);border:1px solid rgba(245,200,66,0.3);color:var(--yellow);padding:2px 8px;border-radius:4px;font-size:11px;">Neutral</span>'
                    else:
                        sent_html = '<span style="color:var(--muted);">—</span>'

                    st.markdown(
                        f'<div style="display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:6px;padding:8px 10px;'
                        f'border-bottom:1px solid rgba(32,56,68,0.5);font-size:13px;transition:background 0.15s;">'
                        f'<span style="color:var(--text);font-weight:500;">{name[:40]}</span>'
                        f'<span style="color:var(--muted2);">{b_type}</span>'
                        f'<span>{rating_html}</span>'
                        f'<span>{sent_html}</span>'
                        f'<span style="color:var(--muted2);">{num_reviews}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                if len(cat_buildings) > 50:
                    st.caption(f"Showing 50 of {len(cat_buildings)} buildings")


# ─── SECTION 2 — Demand-Gap Profile ──────────────────────────────────────────
st.markdown("---")
st.markdown(
    '<div class="section-title">📊 Demand-Gap Profile</div>'
    '<div class="section-sub">Per-1,000-resident facility ratios vs. Kochi Corporation average. '
    'Red = deficit, green = surplus.</div>',
    unsafe_allow_html=True,
)

# Gap cards row
gap_html = '<div class="gap-cards-row">'
for cat in FACILITY_CATEGORIES:
    gap   = ward.get("demand_gap", {}).get(cat, 0)
    ratio = ward.get("facility_per1000", {}).get(cat, 0)
    is_surplus = gap >= 0
    css_cls = "gap-card surplus" if is_surplus else "gap-card"
    bar_pct = min(100, abs(gap) / max(CORP_AVG.get(cat, 0.001), 0.001) * 100)
    st.markdown(
        f"""
        <div class="{css_cls}">
          <div class="gap-name">{FACILITY_ICONS[cat]} {FACILITY_LABELS[cat]}</div>
          <div class="gap-number">{gap:+.3f}</div>
          <small>per 1,000 vs corp avg ({CORP_AVG.get(cat,0):.4f})</small>
          <div class="gap-bar"><i style="width:{bar_pct:.1f}%"></i></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
gap_html += "</div>"

# Demand-gap table
st.markdown(
    """
    <div class="panel">
      <div class="panel-title">Detailed Demand Table</div>
      <div class="table-head">
        <span>Facility Type</span>
        <span>Ward /1000</span>
        <span>Corp Avg /1000</span>
        <span>Gap</span>
      </div>
    """,
    unsafe_allow_html=True,
)
for cat in FACILITY_CATEGORIES:
    gap   = ward.get("demand_gap", {}).get(cat, 0)
    ratio = ward.get("facility_per1000", {}).get(cat, 0)
    avg   = CORP_AVG.get(cat, 0)
    gap_cls = "neg" if gap < 0 else "pos"
    sign = "+" if gap >= 0 else ""
    st.markdown(
        f"""
        <div class="table-row">
          <span>{FACILITY_ICONS[cat]} {FACILITY_LABELS[cat]}</span>
          <span>{ratio:.3f}</span>
          <span>{avg:.4f}</span>
          <span class="{gap_cls}">{sign}{gap:.3f}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
st.markdown("</div>", unsafe_allow_html=True)


# ─── SECTION 3 — Development Recommendations ─────────────────────────────────
st.markdown("---")
st.markdown(
    f'<div class="section-title">🏗️ {"Development" if mode == "BUILD" else "Conversion"} Recommendations</div>'
    '<div class="section-sub">Three ranked options generated from demand gap analysis. '
    'Adjust priority sliders and budget in the sidebar to re-optimize live.</div>',
    unsafe_allow_html=True,
)

options = generate_options(ward)
options = score_options(options, weights, ward, budget=budget_value)

# Option cards
cols = st.columns(3)
for i, (col, opt) in enumerate(zip(cols, options)):
    with col:
        rank_color = ["var(--accent)", "var(--yellow)", "var(--muted)"][i]
        border_cls = " selected" if st.session_state.selected_option_idx == i else ""
        # Budget fit indicator
        bf = opt.get("budget_fit", 1.0)
        if bf >= 1.0:
            budget_badge = '<span class="tag" style="margin-bottom:8px;">Within Budget</span>'
        elif bf >= 0.5:
            budget_badge = f'<span class="tag warn" style="margin-bottom:8px;">Over Budget ({100-bf*100:.0f}% penalty)</span>'
        else:
            budget_badge = f'<span class="tag red" style="margin-bottom:8px;">Way Over Budget ({100-bf*100:.0f}% penalty)</span>'
        st.markdown(
            f"""
            <div class="option{border_cls}">
              <div class="option-rank rank-{i+1}">#{opt['rank']}</div>
              <div class="option-icon">{opt['icon']}</div>
              <div class="option-title">{opt['title']}</div>
              <div class="option-description">{opt['description']}</div>
              <div class="score">{opt['total_score']}<span>/100</span></div>
              <div class="score-label">Weighted Score (budget-adjusted)</div>
              {budget_badge}
              <div class="option-meta">
                <div class="meta-item">💰 <span>{opt['cost_label']}</span></div>
                <div class="meta-item">👥 <span>{fmt(opt['people_served'])}</span> served</div>
                <div class="meta-item">📐 <span>{fmt(opt['area_sqm'])} sqm</span></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(f"Select Option {i+1}", key=f"sel_opt_{i}", use_container_width=True):
            st.session_state.selected_option_idx = i
            st.rerun()


# ─── SECTION 4 — Option Detail + Optimization ────────────────────────────────
top_opt = options[st.session_state.selected_option_idx]

st.markdown("---")
st.markdown(
    f'<div class="details-title">📋 {top_opt["title"]} — Detail View</div>',
    unsafe_allow_html=True,
)

col_d1, col_d2 = st.columns([3, 2])

with col_d1:
    bf = top_opt.get("budget_fit", 1.0)
    budget_status = "✅ Within Budget" if bf >= 1.0 else f"⚠️ Over Budget (score × {bf})"
    st.markdown(
        f"""
        <div class="detail-panel">
          <h2>{top_opt['icon']} {top_opt['title']}</h2>
          <p class="detail-description">{top_opt['description']}</p>
          <h3>Scoring Breakdown</h3>
          <div class="feature-list">
            <div class="detail-item">
              <span class="di-label">🤝 Social Impact</span>
              <span class="di-value accent">{top_opt['social_score']}/100</span>
            </div>
            <div class="detail-item">
              <span class="di-label">💰 Economic Viability</span>
              <span class="di-value">{top_opt['economic_score']}/100</span>
            </div>
            <div class="detail-item">
              <span class="di-label">🌱 Sustainability</span>
              <span class="di-value">{top_opt['sustainability_score']}/100</span>
            </div>
            <div class="detail-item">
              <span class="di-label">⚙️ Feasibility</span>
              <span class="di-value">{top_opt['feasibility_score']}/100</span>
            </div>
            <div class="detail-item">
              <span class="di-label">🏦 Budget Fit</span>
              <span class="di-value" style="color:{'var(--accent)' if bf >= 1.0 else 'var(--red)'};">{budget_status}</span>
            </div>
            <div class="detail-item">
              <span class="di-label">📊 Weighted Total</span>
              <span class="di-value accent">{top_opt['total_score']}/100</span>
            </div>
          </div>
          <h3>Key Figures</h3>
          <div class="detail-item">
            <span class="di-label">Estimated Cost</span>
            <span class="di-value">{top_opt['cost_label']}</span>
          </div>
          <div class="detail-item">
            <span class="di-label">Available Budget</span>
            <span class="di-value">₹{budget_value/1e7:.0f} Cr</span>
          </div>
          <div class="detail-item">
            <span class="di-label">Build Area</span>
            <span class="di-value">{fmt(top_opt['area_sqm'])} sqm</span>
          </div>
          <div class="detail-item">
            <span class="di-label">Projected Beneficiaries</span>
            <span class="di-value accent">{fmt(top_opt['people_served'])}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col_d2:
    # Budget allocation
    alloc = compute_budget_allocation(top_opt)
    st.markdown(
        '<div class="panel"><div class="panel-title">Budget Allocation</div>',
        unsafe_allow_html=True,
    )
    for a in alloc:
        amt_cr = a["amount"] / 1e7
        st.markdown(
            f"""
            <div class="allocation">
              <div class="alloc-header">
                <span class="alloc-label">{a['label']}</span>
                <span class="alloc-value">₹{amt_cr:.2f} Cr ({a['pct']}%)</span>
              </div>
              {pct_bar(a['pct'], 50)}
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # Ecostat material prices
    st.markdown(
        """
        <div class="panel">
          <div class="panel-title">Kerala Ecostat Prices (EKM Q4 2025-26)</div>
          <div class="detail-item">
            <span class="di-label">Cement 53-grade (50 kg bag)</span>
            <span class="di-value">₹350</span>
          </div>
          <div class="detail-item">
            <span class="di-label">TMT Steel 12mm (per MT)</span>
            <span class="di-value">₹57,500</span>
          </div>
          <div class="detail-item">
            <span class="di-label">Bricks Class A (/1000)</span>
            <span class="di-value">₹11,000</span>
          </div>
          <div class="detail-item">
            <span class="di-label">Mason Wage (per day)</span>
            <span class="di-value">₹1,200</span>
          </div>
          <div class="detail-item">
            <span class="di-label">Composite Rate (new)</span>
            <span class="di-value accent">₹22,600/sqm</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─── SECTION 5 — AI Narration ─────────────────────────────────────────────────
st.markdown("---")

with st.spinner("🤖 Generating AI narration..."):
    narration, used_llm = generate_narration_llm(
        ward, top_opt, CORP_AVG, budget=budget_value,
    )

if used_llm:
    model_tag = '<span class="tag" style="margin-left:auto">Powered by Gemini LLM</span>'
else:
    model_tag = '<span class="tag warn" style="margin-left:auto">Template fallback (API unavailable)</span>'

st.markdown(
    f"""
    <div class="ai-panel fade-in">
      <div class="ai-header">
        <span class="ai-icon">🤖</span>
        <span class="ai-label">AI Recommendation Narration</span>
        {model_tag}
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.markdown(narration)


# ─── SECTION 6 — Impact Simulation ───────────────────────────────────────────
st.markdown("---")
st.markdown(
    '<div class="section-title">📈 Impact Simulation</div>'
    f'<div class="section-sub">Before/after projections for <b>{top_opt["title"]}</b> in {ward["ward_lgd_name"]}.</div>',
    unsafe_allow_html=True,
)

impact_rows = simulate_impact(ward, top_opt, budget=budget_value)

st.markdown(
    """
    <div class="panel">
      <div class="impact-head">
        <span>Metric</span><span>Current</span><span>Projected</span><span>Change</span>
      </div>
    """,
    unsafe_allow_html=True,
)
for row in impact_rows:
    chg_cls = "imp-pos" if row.get("positive") else "imp-neg"
    st.markdown(
        f"""
        <div class="impact-row">
          <span>{row['metric']}</span>
          <span>{row['current']}</span>
          <span>{row['projected']}</span>
          <span class="{chg_cls}">{row['change']}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
st.markdown("</div>", unsafe_allow_html=True)


# ─── SECTION 7 — REUSE Mode ───────────────────────────────────────────────────
if mode == "REUSE":
    st.markdown("---")
    st.markdown(
        '<div class="section-title">♻️ Reuse Mode — Underused Building Conversion</div>'
        '<div class="section-sub">AI-matched conversion candidates for existing structures in this ward.</div>',
        unsafe_allow_html=True,
    )

    ward_bldgs = [
        b for b in BUILDINGS
        if b.get("ward_lgd_code") == ward["ward_lgd_code"]
        and b.get("category") in ("commercial", "public", "generic")
        and b.get("area_sqm", 0) > 300
    ][:10]

    if not ward_bldgs:
        st.info("No candidate buildings identified for reuse in this ward (minimum 300 sqm, commercial/public type).")
    else:
        st.markdown(
            """
            <div class="panel">
              <div class="panel-title">Conversion Candidates</div>
              <div class="table-head" style="grid-template-columns:2fr 1fr 1fr 1fr 1fr;">
                <span>Building</span><span>Type</span><span>Area sqm</span>
                <span>Reno Cost</span><span>Capital Avoided</span>
              </div>
            """,
            unsafe_allow_html=True,
        )
        for b in ward_bldgs:
            area     = b.get("area_sqm", 0)
            reno     = int(area * DATA["COST_RENOV_SQM"])
            new_cost = int(area * DATA["COST_NEW_SQM"])
            avoided  = new_cost - reno
            name     = b.get("name") or f"Building ({b['building_tag']})"
            st.markdown(
                f"""
                <div class="table-row" style="grid-template-columns:2fr 1fr 1fr 1fr 1fr;">
                  <span>{name[:30]}</span>
                  <span>{b['building_tag']}</span>
                  <span>{area:.0f}</span>
                  <span>₹{reno/1e5:.1f}L</span>
                  <span class="pos">₹{avoided/1e5:.1f}L</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown(
            f"""
            <div class="ai-panel">
              <div class="ai-header">
                <span class="ai-icon">♻️</span>
                <span class="ai-label">Reuse Mode Insight</span>
              </div>
              <p>
                In <strong>{ward['ward_lgd_name']}</strong>, there are
                <strong>{len(ward_bldgs)}</strong> candidate buildings for adaptive reuse.
                Converting these structures (avg area <strong>{int(sum(b.get('area_sqm',0) for b in ward_bldgs)/max(1,len(ward_bldgs)))} sqm</strong>)
                at the renovation rate of <strong>₹7,900/sqm</strong> avoids approximately
                <strong>₹{sum(int((b.get('area_sqm',0)*DATA['COST_NEW_SQM'])-(b.get('area_sqm',0)*DATA['COST_RENOV_SQM'])) for b in ward_bldgs)/1e7:.1f} crore</strong>
                in new construction capital.
                <br><em>Renovation rate: industry retrofit assumption (35% of new build rate). Not quantity-surveyed.</em>
              </p>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ─── SECTION 8 — Vacant Parcel Detection (ML-based) ──────────────────────────
st.markdown("---")
st.markdown(
    '<div class="section-title">🛰️ Vacant-Space Detection</div>'
    '<div class="section-sub">'
    'ML-detected candidate vacant parcels using <span class="tag">Isolation Forest</span> '
    'anomaly detection on spatial features (building density, proximity, coverage, diversity).'
    '</div>',
    unsafe_allow_html=True,
)

ward_vacant = [v for v in VACANT if v["ward_lgd_code"] == ward["ward_lgd_code"]]
nearby_vacant = ward_vacant if ward_vacant else VACANT[:3]

if nearby_vacant:
    # Method summary
    methods = set(v.get("method", "unknown") for v in nearby_vacant)
    method_label = "Isolation Forest" if "isolation_forest" in methods else "Simulation Fallback"

    st.markdown(
        f"""
        <div style="display:flex;gap:12px;margin-bottom:16px;font-size:12px;color:var(--muted2);">
          <span>Detection: <b style="color:var(--accent);">{method_label}</b></span>
          <span>Candidates in ward: <b style="color:var(--text);">{len(ward_vacant)}</b></span>
          <span>Total candidates: <b style="color:var(--text);">{len(VACANT)}</b></span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="panel">
          <div class="panel-title">Candidate Vacant Parcels</div>
          <div class="table-head" style="grid-template-columns:1fr 1fr 1fr 1fr 1fr 1fr;">
            <span>ID</span><span>Area (sqm)</span><span>Lat</span><span>Lon</span><span>Confidence</span><span>Method</span>
          </div>
        """,
        unsafe_allow_html=True,
    )
    for vc in nearby_vacant:
        conf = vc["confidence"]
        if conf >= 0.8:
            conf_cls, conf_color = "pos", "var(--accent)"
        elif conf >= 0.6:
            conf_cls, conf_color = "", "var(--yellow)"
        else:
            conf_cls, conf_color = "neg", "var(--red)"
        method = vc.get("method", "unknown")
        method_badge = (
            '<span style="background:rgba(97,223,115,0.12);border:1px solid rgba(97,223,115,0.3);'
            'color:var(--accent);padding:2px 6px;border-radius:4px;font-size:10px;">ML</span>'
            if method == "isolation_forest" else
            '<span style="background:rgba(245,200,66,0.12);border:1px solid rgba(245,200,66,0.3);'
            'color:var(--yellow);padding:2px 6px;border-radius:4px;font-size:10px;">Fallback</span>'
        )
        st.markdown(
            f"""
            <div class="table-row" style="grid-template-columns:1fr 1fr 1fr 1fr 1fr 1fr;">
              <span>{vc['candidate_id']}</span>
              <span>{vc['area_sqm']}</span>
              <span style="font-family:'JetBrains Mono',monospace;font-size:11px;">{vc['centroid_lat']}</span>
              <span style="font-family:'JetBrains Mono',monospace;font-size:11px;">{vc['centroid_lon']}</span>
              <span style="color:{conf_color};font-weight:600;">{conf:.0%}</span>
              <span>{method_badge}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # Method disclosure
    if "isolation_forest" in methods:
        st.markdown(
            """
            <div class="side-note" style="margin-top:12px">
              <strong>🔍 Detection Method: Isolation Forest (Unsupervised ML)</strong><br>
              Candidates are identified by <strong>Isolation Forest anomaly detection</strong> applied to a
              grid of spatial feature vectors computed for each cell:<br>
              <code>building_density, avg_nearest_dist, min_nearest_dist, building_coverage,
              facility_diversity, dist_to_center, gap_severity</code><br><br>
              Features are <strong>standardized</strong> (zero mean, unit variance) before fitting.
              The model flags cells that are anomalously empty relative to the ward's overall
              building pattern. Confidence reflects the anomaly score magnitude.<br><br>
              <strong>Why Isolation Forest?</strong> It works well on high-dimensional data, requires no labels,
              handles varying ward densities naturally, and is fast to train (~0.1s for all 74 wards).
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="side-note" style="margin-top:12px">
              <strong>⚠ Detection Method: Simulation Fallback</strong><br>
              ML detection produced no candidates for this ward. Candidates shown are
              generated by a heuristic fallback placed near the ward centroid.
            </div>
            """,
            unsafe_allow_html=True,
        )
else:
    st.info("No vacant parcel candidates found for this ward. Check nearby wards or use the full-corporation view.")


# ─── FOOTER ───────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="footer">
      <div class="footer-sources">
        <span>📦 Data Sources:</span>
        <a href="https://www.openstreetmap.org" target="_blank">OpenStreetMap (ODbL)</a>
        <a href="https://ecostat.kerala.gov.in" target="_blank">Kerala Ecostat Q4 2025-26</a>
        <a href="https://censusindia.gov.in" target="_blank">Census of India 2011</a>
        <a href="https://bhuvan.nrsc.gov.in" target="_blank">Bhuvan ISRO (LULC ref)</a>
        <a href="https://lgdirectory.gov.in" target="_blank">LGD Ward Boundaries</a>
      </div>
      <div>
        <span class="footer-warn">⚠ Population: density-estimated · Reviews: synthetic · Amenities: OSM (undercount) · Costs: market composite</span>
      </div>
      <div style="color:var(--muted)">CITYDEV v1.0 · Kochi Corp Planning Support</div>
    </div>
    """,
    unsafe_allow_html=True,
)
