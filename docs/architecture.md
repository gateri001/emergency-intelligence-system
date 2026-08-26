# Architecture

## Goal

Turn scattered incident reports — citizen, officer, and bulk-imported — into a
single risk picture, fast enough to act on.

## Flow

```
Citizen reports (app/USSD/SMS)  ─┐
Officer reports (app)           ─┼─► /report/* endpoints ─► risk_surface.point_risk() ─► incidents table
Bulk imports (open data feeds)  ─┘                                │
                                                                    ▼
                                                          dashboard + /predict API
```

1. **Ingestion** — three entry points (`/report/citizen`, `/report/officer`,
   `/report/bulk`) write into one `incidents` table. Citizen reports are
   open; officer and bulk reports require an authenticated officer account.
   Every report requires real coordinates (`latitude`/`longitude`) - `area`
   is an optional free-text label for display only, never used for scoring.
2. **Risk scoring** (`src/risk_surface.py`) — real incidents can happen
   anywhere, at any time, and several at once; there is no fixed list of
   "the areas that matter." So risk isn't a lookup against named places -
   it's a continuous surface: every point on the map gets a risk score from
   a distance- and recency-weighted kernel over nearby incidents (closer and
   more recent incidents count more), optionally filtered to one hazard
   category (crime/hazard/medical) so a flood query isn't muddied by
   unrelated crime history at the same spot. `point_risk(lat, lon, type)` is
   the one function both `/predict` and incident-ingestion scoring call -
   one model, not two. An earlier version of this scored incidents with a
   RandomForest trained on 16 hardcoded area *names* as categorical labels;
   that's been removed - it couldn't answer for any location outside that
   fixed list, which defeats the point of "anywhere, anytime."
3. **Serving** — `/incidents` and `/predict` feed the dashboard
   (`static/index.html`), which shows recent reports on a map and lets
   anyone click any point and check predicted risk there - not limited to a
   dropdown of named areas.
4. **Safe routing** (`src/risk_surface.py`, `src/routing.py`) — turns raw
   incidents into a continuous spatial risk surface (a grid, each cell
   scored by distance- and recency-weighted nearby incidents — closer and
   more recent incidents count more), then A\*-searches from any point to
   the nearest genuinely low-risk zone, penalizing paths that cut through
   dangerous cells along the way. This is grid-based risk-aware pathfinding,
   not full street-level turn-by-turn navigation (that needs a real road
   graph, out of scope for now) — but it's real graph search over an actual
   risk model, not a nearest-neighbor lookup or a static heatmap.

5. **Broadcast alerts** (`src/broadcast.py`) — geo-targeted mass alerting,
   independent of anyone choosing to reshare a post (the actual problem
   with how missing-person alerts spread today). Anyone can opt in via
   `/subscribers` (phone number + location). An authenticated officer can
   trigger `/alert/broadcast` for a specific incident, which finds every
   subscriber within a radius (haversine distance) and sends through a
   pluggable `BroadcastProvider`. Deliberately officer-gated, not automatic
   off a severity score — a mass alert is a consequential action and needs
   a human decision behind it, matching the human-in-the-loop principle
   this project has had since the original design. Every broadcast is
   logged (`broadcasts` table) with who triggered it and how many people
   were reached.

   The default provider (`ConsoleBroadcastProvider`) logs what would be
   sent rather than sending real SMS — this lets the whole pipeline
   (geo-targeting, audit trail, API contract) be built and tested before
   there's a live SMS account behind it. `AfricasTalkingProvider` is
   stubbed with the exact integration shape for when real credentials
   exist; it deliberately refuses to run rather than pretend to send real
   messages without them.

6. **Real open data** (`scripts/ingest_gdacs.py`, `scripts/ingest_unosat_flood.py`) —
   two real, verified sources feed into the system alongside the synthetic
   training data:
   - **GDACS**: national-scale disaster alerts (flood/drought/wildfire),
     free, no registration, live-pollable. Stored in `external_events` -
     separate from the incidents table, since GDACS events are coarse
     (weeks-long, one imprecise point) and would distort the risk grid's
     spatial kernel if mixed with point-level reports.
   - **UNOSAT** (via the Humanitarian Data Exchange): satellite-derived,
     ground-truthed flood mapping for the April 2024 Kenya floods -
     precise flood-extent polygons and 12,211 individually-identified
     affected structures. This is a one-time historical snapshot, not a
     live feed, used two ways: (1) the flood extent polygon is served via
     `/events/flood-extents` and drawn on the dashboard as real ground
     truth, and (2) the affected-structure counts, aggregated by nearest
     known area, are used to weight `generate_synthetic_data.py`'s flood
     distribution - synthetic flood incidents are now concentrated in the
     areas real satellite data confirmed were actually flooded (Githurai,
     Kayole, Donholm, Kasarani, Eastleigh, Ruiru, Umoja), not spread
     uniformly across all 16 areas like before. This is real calibration,
     not just a visual add-on.

## Explicitly not yet built

- Street-level turn-by-turn routing (current routing is grid-based, not
  road-graph-based).
- Real (non-synthetic) incident data — training data today is generated by
  `scripts/generate_synthetic_data.py`, not sourced from real records.
- A live SMS provider behind the broadcast system (console-only for now;
  see `src/broadcast.py`).
- Real safe-zone locations (police stations, hospitals) — currently "safe"
  means "lowest-risk nearby cell," not a verified point of safety.
- Cell Broadcast (SMS-CB) for true no-opt-in-required reach — current
  broadcast only reaches people who've subscribed via `/subscribers`.
- Calibrated/absolute risk scores — the 0-1 risk value is normalized
  relative to the current data's own maximum, not an absolute probability.
  Severity buckets (Low/Medium/High, thresholds in
  `risk_surface.severity_bucket`) will shift as more data comes in.

## Data

Training data is 100% synthetic (see `scripts/generate_synthetic_data.py`)
— generated locations, times, and types, no real reports, no real people.
See `privacy_policy.md` for how real citizen/officer reports are handled
once they start flowing in.
