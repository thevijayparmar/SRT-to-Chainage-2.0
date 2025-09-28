#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Drone SRT & Chainage — Streamlit App
Creator: Vijay Parmar — for BGol (Community of Surveyors and GIS Experts)

Single-file entrypoint: streamlit_app.py
This module also exposes pure functions that our pytest tests import.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any

# Third-party (soft) deps
try:
    import streamlit as st
except Exception:  # pragma: no cover (allow tests to import without Streamlit runtime)
    st = None  # type: ignore

import numpy as np
import pandas as pd

# XML parsing
try:
    from lxml import etree as LET
    USE_LXML = True
except Exception:
    import xml.etree.ElementTree as LET  # type: ignore
    USE_LXML = False

# Projection stack (with fallback)
try:
    from pyproj import CRS, Transformer
    HAVE_PYPROJ = True
except Exception:
    HAVE_PYPROJ = False

try:
    from shapely.geometry import LineString, Point
    HAVE_SHAPELY = True
except Exception:
    HAVE_SHAPELY = False


# -------------------------
# Utilities & Data Models
# -------------------------
@dataclass
class SrtRecord:
    index: int
    time: datetime
    lat: float
    lon: float
    alt: float
    raw_block: str

@dataclass
class Flight:
    name: str
    records: List[SrtRecord]
    earliest: Optional[datetime]


def _parse_timestamp_from_block(block: str) -> Optional[datetime]:
    """Try multiple timestamp patterns; return naive datetime if possible."""
    # ISO-like date time
    m = re.search(r"(\\d{4}-\\d{2}-\\d{2})[ T](\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?)", block)
    if m:
        try:
            return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}".replace("T", " "))
        except Exception:
            pass
    # Sometimes only time is present in SRT, but we prefer absolute time; else None
    return None


DEFAULT_PATTERNS = {
    "lat": [
        re.compile(r"(?i)\\[latitude[:=]?\\s*([\\-0-9.]+)\\]"),
        re.compile(r"(?i)\\blat[:=]?\\s*([\\-0-9.]+)\\b"),
    ],
    "lon": [
        re.compile(r"(?i)\\[longitude[:=]?\\s*([\\-0-9.]+)\\]"),
        re.compile(r"(?i)\\blon[:=]?\\s*([\\-0-9.]+)\\b"),
    ],
    "alt": [
        re.compile(r"(?i)\\[altitude[:=]?\\s*([\\-0-9.]+)\\]"),
        re.compile(r"(?i)\\balt[:=]?\\s*([\\-0-9.]+)\\b"),
    ],
    "gps_pair": [
        re.compile(r"(?i)gps:\\s*([\\-0-9.]+)\\s*,\\s*([\\-0-9.]+)")
    ],
    "latlon_pair": [
        re.compile(r"(?i)lat:\\s*([\\-0-9.]+).*?lon:\\s*([\\-0-9.]+)")
    ],
    "timestamp": [
        re.compile(r"\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?")
    ]
}


def parse_srt(file_bytes: bytes, file_name: str, custom_regex: Optional[Dict[str, List[str]]] = None
              ) -> Tuple[List[SrtRecord], List[str]]:
    """
    Robust SRT parser.
    Returns (records, warnings).
    """
    text = file_bytes.decode(errors="ignore")
    blocks = re.split(r"\\n\\s*\\n", text.strip())
    warnings: List[str] = []
    recs: List[SrtRecord] = []

    # Build regexes (custom first so user overrides defaults)
    patterns = {k: [] for k in DEFAULT_PATTERNS}
    if custom_regex:
        for key, plist in custom_regex.items():
            if key in patterns:
                for p in plist:
                    try:
                        patterns[key].append(re.compile(p, re.IGNORECASE))
                    except Exception as e:
                        warnings.append(f"{file_name}: Bad custom regex for {key}: {e}")
    for key, plist in DEFAULT_PATTERNS.items():
        patterns[key].extend(plist)

    idx_counter = 1
    for block in blocks:
        # Extract an "index" if present
        m_idx = re.match(r"\\s*(\\d+)\\s+\\n", block)
        if m_idx:
            try:
                idx_counter = int(m_idx.group(1))
            except Exception:
                pass

        # Time range line like "00:00:00,000 --> 00:00:01,000" is optional for our purposes

        # Extract lat/lon/alt using regex fallbacks
        lat = lon = alt = None  # type: ignore
        for p in patterns["lat"]:
            m = p.search(block)
            if m:
                lat = float(m.group(1)); break

        for p in patterns["lon"]:
            m = p.search(block)
            if m:
                lon = float(m.group(1)); break

        if (lat is None or lon is None):
            # Try pairs
            found_pair = False
            for p in patterns["gps_pair"] + patterns["latlon_pair"]:
                m = p.search(block)
                if m:
                    lat = float(m.group(1)); lon = float(m.group(2)); found_pair = True; break
            if not found_pair:
                warnings.append(f"{file_name}: Skipped a block lacking lat/lon.")
                continue

        for p in patterns["alt"]:
            m = p.search(block)
            if m:
                alt = float(m.group(1)); break
        if alt is None:
            alt = float('nan')

        # Timestamp
        tstamp = _parse_timestamp_from_block(block)
        if not tstamp:
            # Try explicit timestamp regex list
            for p in patterns["timestamp"]:
                m = p.search(block)
                if m:
                    try:
                        tstamp = datetime.fromisoformat(m.group(0).replace("T", " "))
                    except Exception:
                        pass
                    break
        if not tstamp:
            warnings.append(f"{file_name}: A block had coordinates but no absolute timestamp — using index order.")
            # fabricate monotonic time using idx (seconds)
            tstamp = datetime.fromtimestamp(idx_counter)

        recs.append(SrtRecord(index=idx_counter, time=tstamp, lat=float(lat), lon=float(lon),
                              alt=float(alt), raw_block=block))
        idx_counter += 1

    return recs, warnings


def parse_kml_first_linestring(file_bytes: bytes) -> Tuple[List[Tuple[float, float]], List[str]]:
    """
    Parse KML, return first LineString lat,lon list. Warnings for multi-lines.
    """
    warnings: List[str] = []
    try:
        tree = LET.parse(io.BytesIO(file_bytes))
    except Exception as e:
        raise ValueError(f"Failed to parse KML: {e}")

    NS = {"kml": "http://www.opengis.net/kml/2.2"}
    # Find all coordinate sets under LineString
    if USE_LXML:
        coords_nodes = tree.findall(".//kml:LineString/kml:coordinates", namespaces=NS)
    else:
        root = tree.getroot()
        coords_nodes = root.findall(".//{http://www.opengis.net/kml/2.2}LineString/{http://www.opengis.net/kml/2.2}coordinates")

    if not coords_nodes:
        raise ValueError("No LineString coordinates found in KML.")

    if len(coords_nodes) > 1:
        warnings.append("Multiple LineStrings found — using the first.")

    txt = coords_nodes[0].text or ""
    coords: List[Tuple[float,float]] = []
    for token in txt.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            lon = float(parts[0]); lat = float(parts[1])
            coords.append((lat, lon))

    if len(coords) < 2:
        raise ValueError("KML must contain at least two vertices in the first LineString.")
    return coords, warnings


def aeqd_transformer_for_chain(coords_latlon: List[Tuple[float,float]]):
    """
    Build azimuthal equidistant projection centered on chain centroid.
    Returns forward (lon,lat)->(x,y) and inverse transformers.
    """
    lats = [c[0] for c in coords_latlon]
    lons = [c[1] for c in coords_latlon]
    lat0 = float(np.mean(lats)); lon0 = float(np.mean(lons))

    if HAVE_PYPROJ:
        proj_str = f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs"
        src = CRS.from_epsg(4326)
        dst = CRS.from_proj4(proj_str)
        fwd = Transformer.from_crs(src, dst, always_xy=True).transform
        inv = Transformer.from_crs(dst, src, always_xy=True).transform
        return (lambda lat, lon: fwd(lon, lat),  # return x,y in meters
                lambda x, y: inv(x, y))
    else:
        # Fallback: equirectangular approximation around centroid (meters approx)
        R = 6371008.8
        lat0r = math.radians(lat0); lon0r = math.radians(lon0)
        def fwd_fallback(lat: float, lon: float):
            x = R * (math.radians(lon) - lon0r) * math.cos(lat0r)
            y = R * (math.radians(lat) - lat0r)
            return x, y
        def inv_fallback(x: float, y: float):
            lat = math.degrees(y/6371008.8 + lat0r)
            lon = math.degrees(x/(6371008.8*math.cos(lat0r)) + lon0r)
            return lat, lon
        return fwd_fallback, inv_fallback


def chain_segments_xy(chain_latlon: List[Tuple[float,float]]):
    """
    Precompute XY arrays and segment vectors for chain.
    """
    fwd, _ = aeqd_transformer_for_chain(chain_latlon)
    XY = np.array([fwd(lat, lon) for lat, lon in chain_latlon], dtype=float)  # (x,y)
    A = XY[:-1]               # segment starts
    B = XY[1:]                # segment ends
    V = B - A                 # vectors
    L2 = np.sum(V*V, axis=1)  # squared lengths
    L = np.sqrt(L2)
    cum = np.zeros(len(XY))
    cum[1:] = np.cumsum(L)
    return XY, A, V, L, L2, cum


def project_points_to_chain(points_latlon: List[Tuple[float,float]], chain_latlon: List[Tuple[float,float]]):
    """
    For each point, find nearest projection on chain and return:
    chainage_m, distance_to_chain_m, projected XY, chosen segment index, t.
    """
    fwd, _ = aeqd_transformer_for_chain(chain_latlon)
    XY_chain, A, V, L, L2, cum = chain_segments_xy(chain_latlon)
    P = np.array([fwd(lat, lon) for lat, lon in points_latlon], dtype=float)

    chainage_m = np.zeros(len(P))
    dist_m = np.zeros(len(P))
    proj_xy = np.zeros_like(P)
    seg_idx = np.zeros(len(P), dtype=int)
    t_vals = np.zeros(len(P))

    for i, p in enumerate(P):
        # Vector from all A to p: (Nseg,2)
        AP = p - A
        # Parameter t along each segment
        t_all = np.where(L2>0, (AP*V).sum(axis=1)/L2, 0.0)
        t_clamped = np.clip(t_all, 0.0, 1.0)
        proj_all = A + (t_clamped[:,None] * V)
        # Distances to each projection
        dists = np.linalg.norm(proj_all - p, axis=1)
        j = int(np.argmin(dists))
        best_t = float(t_clamped[j])
        best_proj = proj_all[j]
        d = float(dists[j])

        chainage = float(cum[j] + best_t * L[j])

        chainage_m[i] = chainage
        dist_m[i] = d
        proj_xy[i] = best_proj
        seg_idx[i] = j
        t_vals[i] = best_t

    return chainage_m, dist_m, proj_xy, seg_idx, t_vals


# -------------------------
# KML Utilities
# -------------------------
KML_COLORS_ABGR = [
    "ff1f77b4","ffff7f0e","ff2ca02c","ffd62728","ff9467bd","ff8c564b",
    "ffe377c2","ff7f7f7f","ffbcbd22","ff17becf","ffa55194","ff393b79"
]

def kml_document_header(description: str = "") -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\\n"
        "<kml xmlns='http://www.opengis.net/kml/2.2'>\\n"
        "<Document>\\n"
        f"<name>Drone SRT & Chainage</name>\\n"
        f"<description>{description}</description>\\n"
        "<Style id='chainStyle'><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>\\n"
        "<Style id='mergedStyle'><LineStyle><color>ff00ff00</color><width>3</width></LineStyle></Style>\\n"
    )

def kml_flight_style(i: int) -> str:
    color = KML_COLORS_ABGR[i % len(KML_COLORS_ABGR)]
    return f"<Style id='flight{i}'><LineStyle><color>{color}</color><width>3</width></LineStyle></Style>\\n"

def kml_linestring(name: str, coords_xyz: List[Tuple[float,float,float]], style_url: str) -> str:
    coord_txt = "\\n".join([f"{lon:.6f},{lat:.6f},{alt:.2f}" for lat, lon, alt in coords_xyz])
    return (
        f"<Placemark><name>{name}</name><styleUrl>#{style_url}</styleUrl>"
        f"<LineString><tessellate>1</tessellate><coordinates>\\n{coord_txt}\\n</coordinates></LineString></Placemark>\\n"
    )

def kml_point(name: str, lat: float, lon: float, alt: float) -> str:
    return (f"<Placemark><name>{name}</name>"
            f"<Point><coordinates>{lon:.6f},{lat:.6f},{alt:.2f}</coordinates></Point></Placemark>\\n")

def kml_footer() -> str:
    return "</Document></kml>"


# -------------------------
# Merging logic
# -------------------------
def merge_flights_in_order(flights: List[Flight]) -> Tuple[List[SrtRecord], List[Tuple[int,int,int]]]:
    """
    Merge flights preserving order; reindex starting at 1.
    Returns merged records and list of (start_idx,end_idx,flight_i) per original.
    """
    merged: List[SrtRecord] = []
    boundaries: List[Tuple[int,int,int]] = []
    cur = 1
    for fi, f in enumerate(flights):
        start = cur
        for r in f.records:
            merged.append(SrtRecord(index=cur, time=r.time, lat=r.lat, lon=r.lon, alt=r.alt, raw_block=r.raw_block))
            cur += 1
        end = cur-1
        boundaries.append((start, end, fi))
    return merged, boundaries


# -------------------------
# Exports
# -------------------------
def records_to_dataframe(recs: List[SrtRecord], chainage_m: np.ndarray, dist_m: np.ndarray) -> pd.DataFrame:
    df = pd.DataFrame({
        "index": [r.index for r in recs],
        "timestamp": [r.time.isoformat(sep=" ") for r in recs],
        "lat": [r.lat for r in recs],
        "lon": [r.lon for r in recs],
        "alt": [r.alt for r in recs],
        "chainage_km": np.round(chainage_m/1000.0, 3),
        "distance_to_chain_m": np.round(dist_m, 2),
    })
    return df

def dataframe_to_srt(df: pd.DataFrame) -> str:
    # Minimal SRT where the text is "lat lon alt | chainage | distance"
    lines = []
    for i, row in df.iterrows():
        idx = int(row["index"])
        # fabricate a trivial time range
        t0 = f"{idx//3600:02d}:{(idx%3600)//60:02d}:{idx%60:02d},000"
        t1 = f"{idx//3600:02d}:{(idx%3600)//60:02d}:{idx%60:02d},999"
        text = (f"[latitude: {row['lat']:.6f}] [longitude: {row['lon']:.6f}] [altitude: {row['alt']:.2f}] "
                f"{row['timestamp']} | chainage: {row['chainage_km']:.3f} km | d2chain: {row['distance_to_chain_m']:.2f} m")
        lines.append(f"{idx}\\n{t0} --> {t1}\\n{text}\\n")
    return "\\n".join(lines)


def build_kml(chain_latlon: List[Tuple[float,float]], flights: List[Flight], merged_df: pd.DataFrame,
              boundaries: List[Tuple[int,int,int]], include_merged: bool = False) -> str:
    # credit in description
    kml = [kml_document_header(description="Creator: Vijay Parmar — for BGol (Community of Surveyors and GIS Experts)")]
    kml.append("<name>Drone Chain & Flights</name>\\n")
    # Define flight styles
    for i in range(len(flights)):
        kml.append(kml_flight_style(i))

    # Chain polyline
    chain_coords = "\\n".join([f"{lon:.6f},{lat:.6f},0" for lat, lon in chain_latlon])
    kml.append("<Placemark><name>Chain</name><styleUrl>#chainStyle</styleUrl>"
               f"<LineString><tessellate>1</tessellate><coordinates>\\n{chain_coords}\\n</coordinates></LineString></Placemark>\\n")

    # Per-flight lines + start/end if have chainage
    for i, f in enumerate(flights):
        # records subset via boundaries
        b = boundaries[i]
        start_idx, end_idx, _ = b
        # Build coords list
        coords = [(r.lat, r.lon, r.alt) for r in f.records]
        kml.append(kml_linestring(f.name, coords, f"flight{i}"))
        # Start/End placemarks if chainage present
        try:
            srow = merged_df[merged_df["index"] == start_idx].iloc[0]
            erow = merged_df[merged_df["index"] == end_idx].iloc[0]
            kml.append(kml_point(f"Start: {srow['chainage_km']:.3f} km", srow["lat"], srow["lon"], srow["alt"]))
            kml.append(kml_point(f"End: {erow['chainage_km']:.3f} km", erow["lat"], erow["lon"], erow["alt"]))
        except Exception:
            pass

    if include_merged and len(merged_df):
        coords = list(zip(merged_df["lat"].tolist(), merged_df["lon"].tolist(), merged_df["alt"].fillna(0).tolist()))
        kml.append(kml_linestring("Merged Flight", coords, "mergedStyle"))

    kml.append(kml_footer())
    return "".join(kml)


# -------------------------
# Streamlit UI
# -------------------------
def run_app():
    st.set_page_config(page_title="Drone SRT & Chainage — BGol", layout="wide")
    st.title("Drone SRT & Chainage (Always-Project, Multi-SRT Merge)")
    st.caption("Creator: Vijay Parmar — for BGol (Community of Surveyors and GIS Experts)")

    st.markdown("### 1) Upload KML (LineString) and SRT files")
    kml_file = st.file_uploader("Chain KML (first LineString will be used)", type=["kml","xml"])
    srt_files = st.file_uploader("Drone .srt files (you can select multiple)", type=["srt"], accept_multiple_files=True)

    warnings_all: List[str] = []

    chain_latlon: Optional[List[Tuple[float,float]]] = None
    if kml_file is not None:
        try:
            chain_latlon, w = parse_kml_first_linestring(kml_file.read())
            warnings_all.extend(w)
        except Exception as e:
            st.error(str(e))
            return

        st.success(f"KML loaded: {len(chain_latlon)} vertices.")
        # Chain info
        st.markdown("#### KML Summary")
        st.write(f"- Vertices: **{len(chain_latlon)}**")
        st.write(f"- First vertex (lat, lon): **{chain_latlon[0][0]:.6f}, {chain_latlon[0][1]:.6f}**")
        st.write(f"- Last vertex  (lat, lon): **{chain_latlon[-1][0]:.6f}, {chain_latlon[-1][1]:.6f}**")

        # Compute length in km using projection distances
        XY, A, V, L, L2, cum = chain_segments_xy(chain_latlon)
        total_len_km = float(np.sum(L)/1000.0)
        st.write(f"- Total chain length: **{total_len_km:.3f} km**")

        swap = st.toggle("Swap start/end (reverse chain direction)")
        if swap:
            chain_latlon = list(reversed(chain_latlon))
            XY, A, V, L, L2, cum = chain_segments_xy(chain_latlon)
            total_len_km = float(np.sum(L)/1000.0)

    # Advanced regex area
    with st.expander("Advanced: Custom regex (optional)"):
        st.write("Paste JSON with keys lat/lon/alt/gps_pair/latlon_pair/timestamp -> list of regex strings.")
        custom_txt = st.text_area("Custom regex JSON", height=120, placeholder='{"lat": ["(?i)Latitude\\s*[:=]\\s*([\\-0-9.]+)"]}')
        custom_regex = None
        if custom_txt.strip():
            try:
                import json
                custom_regex = json.loads(custom_txt)
                st.success("Custom patterns parsed.")
            except Exception as e:
                st.warning(f"Custom regex JSON error: {e}")
                custom_regex = None

    # Need both
    if not (chain_latlon and srt_files):
        st.info("Upload both KML and SRT(s) to continue.")
        st.stop()

    # 2) SRT parsing / file ordering
    flights: List[Flight] = []
    for f in srt_files:
        recs, w = parse_srt(f.read(), f.name, custom_regex=custom_regex)
        warnings_all.extend(w)
        earliest = min((r.time for r in recs), default=None)
        flights.append(Flight(name=f.name, records=recs, earliest=earliest))

    # default order by earliest
    flights.sort(key=lambda F: (F.earliest or datetime.max))

    # Editable order UI
    st.markdown("### 2) SRT list & order")
    if "flight_order" not in st.session_state:
        st.session_state.flight_order = list(range(len(flights)))
    # Reset to datetime order
    if st.button("Reset to DateTime order"):
        st.session_state.flight_order = list(range(len(flights)))

    # Display with up/down
    for i, idx in enumerate(st.session_state.flight_order):
        col1, col2, col3 = st.columns([6,1,1])
        with col1:
            label = flights[idx].name
            t = flights[idx].earliest.isoformat(sep=" ") if flights[idx].earliest else "n/a"
            st.write(f"{i+1}. **{label}** — earliest: {t}")
        with col2:
            if st.button("▲", key=f"up{idx}") and i>0:
                order = st.session_state.flight_order
                order[i-1], order[i] = order[i], order[i-1]
                st.session_state.flight_order = order
                st.experimental_rerun()
        with col3:
            if st.button("▼", key=f"dn{idx}") and i < len(st.session_state.flight_order)-1:
                order = st.session_state.flight_order
                order[i+1], order[i] = order[i], order[i+1]
                st.session_state.flight_order = order
                st.experimental_rerun()

    ordered_flights = [flights[i] for i in st.session_state.flight_order]

    st.markdown("### 3) Compute chainage (merge & project)")
    include_merged_kml = st.checkbox("Include merged flight in KML", value=False)
    exclude_missing = st.checkbox("Exclude blocks missing lat/lon from outputs", value=True)

    if st.button("Compute Chainage (merge & project)"):
        with st.spinner("Computing projections and chainage..."):
            # Optionally filter any records with missing lat/lon (already skipped in parser)
            # Merge
            merged, boundaries = merge_flights_in_order(ordered_flights)
            pts = [(r.lat, r.lon) for r in merged]
            chainage_m, dist_m, _, _, _ = project_points_to_chain(pts, chain_latlon)

            merged_df = records_to_dataframe(merged, chainage_m, dist_m)

            st.markdown("### 5) Results preview")
            st.dataframe(merged_df.head(20))

            # Plotly preview
            import plotly.graph_objects as go
            fig = go.Figure()
            # Chain at z=0
            fig.add_trace(go.Scatter3d(
                x=[c[1] for c in chain_latlon], y=[c[0] for c in chain_latlon], z=[0]*len(chain_latlon),
                mode="lines", name="Chain", line=dict(width=4)
            ))
            # Flights colored
            for i, f in enumerate(ordered_flights):
                xs = [r.lon for r in f.records]
                ys = [r.lat for r in f.records]
                zs = [r.alt if not math.isnan(r.alt) else 0.0 for r in f.records]
                fig.add_trace(go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", name=f.name))
            fig.update_layout(height=600, scene=dict(xaxis_title="Lon", yaxis_title="Lat", zaxis_title="Alt (m)"))
            st.plotly_chart(fig, use_container_width=True)

            # Warnings panel
            if warnings_all:
                st.warning("\\n".join(warnings_all))

            # Build per-flight and merged exports
            outputs = {}

            # merged
            outputs["merged_track.csv"] = merged_df.to_csv(index=False).encode()
            outputs["merged_track.srt"] = dataframe_to_srt(merged_df).encode()

            # per-flight
            for i, f in enumerate(ordered_flights):
                start_idx, end_idx, _ = boundaries[i]
                sub_df = merged_df[(merged_df["index"] >= start_idx) & (merged_df["index"] <= end_idx)].copy()
                base = f.name.rsplit(".",1)[0]
                outputs[f"{base}.csv"] = sub_df.to_csv(index=False).encode()
                outputs[f"{base}.srt"] = dataframe_to_srt(sub_df).encode()
                outputs[f"{base}_distance_to_chain.csv"] = sub_df.to_csv(index=False).encode()

            # KML
            kml_text = build_kml(chain_latlon, ordered_flights, merged_df, boundaries, include_merged=include_merged_kml)
            outputs["flights.kml"] = kml_text.encode("utf-8")

            # ZIP
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in outputs.items():
                    zf.writestr(name, data)
            zbuf.seek(0)
            st.download_button("Download all outputs (ZIP)", data=zbuf.getvalue(), file_name="outputs.zip", mime="application/zip")

            st.caption("Creator: Vijay Parmar — for BGol (Community of Surveyors and GIS Experts)")

            # Projection warning if missing libs
            if not (HAVE_PYPROJ and HAVE_SHAPELY):
                st.info("Accurate projection libraries missing — results may have increased error; please install `pyproj` & `shapely` for meter-accurate projections.")

# Only run UI when launched via `streamlit run`
if __name__ == "__main__":
    if st is not None and getattr(st, "_is_running_with_streamlit", False):
        run_app()
