# Drone SRT & Chainage — Streamlit App

**Creator: Vijay Parmar — for BGol (Community of Surveyors and GIS Experts)**

## What's new
- **Always-project chainage**: every drone sample is projected to the closest point on the chain; no thresholds.
- **Multiple SRT merge**: upload several SRTs, auto-order by earliest timestamp, and merge into one continuous track.
- **Colored KML exports**: per-flight LineStrings styled with distinct colors; optional merged flight LineString.
- **Distance-to-chain**: every sample includes perpendicular distance (meters) to the chain, for QA.
- **Meter-accurate projection**: azimuthal equidistant projection centered on chain centroid using `pyproj`; graceful fallback if projection libs missing.

## Install & Run
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Understanding the outputs
- `chainage_km` is **kilometers (3 decimals)** computed from the chain start via nearest projection.
- `distance_to_chain_m` is the **perpendicular distance** from the drone sample to its projection on the chain (2 decimals).
- Merged outputs: `merged_track.csv`, `merged_track.srt`.
- Per-flight outputs: `<flight>.csv`, `<flight>.srt`, and `<flight>_distance_to_chain.csv` (same table, focused on inspection).
- KML: contains the chain, each flight with a distinct color, and optional merged flight. Start/End placemarks include the chainage labels.

## Fallback behavior
If `pyproj` or `shapely` is not available, the app falls back to an equirectangular approximation for XY meters.
A visible note is shown: **"Accurate projection libraries missing — results may have increased error; please install `pyproj` & `shapely` for meter-accurate projections."**

## Credits
This tool embeds:  
**Creator: Vijay Parmar — for BGol (Community of Surveyors and GIS Experts)** (also inserted in the exported KML description).
