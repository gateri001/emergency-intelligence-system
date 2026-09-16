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

   The risk surface covers all of Kenya (widened 2026-08-26 from an
   earlier Nairobi-only box), at ~10km/cell resolution nationally — a
   deliberate tradeoff (national coverage over fine detail) that's
   flagged, not hidden; see "Explicitly not yet built" below. Routing
   builds its own separate, finer local grid (~800m/cell, ~33km window)
   around each query point rather than searching the coarse national one,
   since a useful route needs street-block resolution and a "nearest safe
   zone" recommendation shouldn't be able to reach hundreds of km away.

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
   there's a live SMS account behind it. Two real providers exist, both
   code-complete but unverified against a live account/device:
   `SMSGateProvider` (self-hosted, open-source, an Android phone with a
   real SIM as your own gateway — no aggregator markup, the scrappy
   no-funding default) and `AfricasTalkingProvider` (aggregator API, the
   option to move to once there's funding and the delivery guarantees of a
   paid provider are worth it). Both deliberately refuse to run rather than
   pretend to send real messages without real credentials.

6. **Real open data** (`scripts/ingest_gdacs.py`, `scripts/ingest_unosat_flood.py`) —
   two real, verified sources feed into the system alongside the synthetic
   training data:
   - **GDACS**: national-scale disaster alerts (flood/drought/wildfire),
     free, no registration, live-pollable. Stored in `external_events` -
     separate from the incidents table, since GDACS events are coarse
     (weeks-long, one imprecise point) and would distort the risk grid's
     spatial kernel if mixed with point-level reports.
   - **NASA FIRMS** (via HDX): near-real-time fire detections, no API key
     needed. Precise point-level data with a real timestamp, so - unlike
     GDACS/UNOSAT - it's ingested directly into the `incidents` table, not
     kept separate. Important caveat: VIIRS detects all thermal anomalies,
     including routine agricultural burning (very common in East Africa) -
     ~1,100 raw detections/day in Kenya's bounding box, filtered to the
     more significant ~20% (FRP >= 10 MW, confidence != low) rather than
     ingested wholesale. See `scripts/ingest_firms.py` for the exact
     threshold and why.
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

## Confidence scoring, visibility scoping, and alert tiers

**Visibility scoping** (`IncidentReport.visibility`, `public` | `officers_only`) -
a reporter can keep a time-critical but unverified report (e.g. a crime
witnessed in progress) out of the public feed entirely, so it reaches
officers (`/incidents/all`) without exposing the reporter's presence/location
to the person they just reported. `/incidents` (the public feed) filters
`officers_only` reports out at the SQL level, not in the client - it never
leaves the server. No reporter identity is collected on any report in the
first place, so this isn't "hidden from the public but visible to officers
including who sent it" - officers don't see who reported it either, by the
same reasoning.

**Confidence scoring** (`src/confidence.py`) - type-specific, not one
universal formula, because the cost of a false positive differs by category:
a fabricated flood report has essentially no motive, while a false crime
accusation can cause real harm. A report starts at a base confidence set by
its source (officer/bulk-verified-sensor reports start high; citizen reports
start low-to-moderate, varying by category) and self-reported evidence,
then gets a bonus for independent nearby reports of the same type within a
2km/6hr window at creation time. After that, anyone can move it further via
`POST /report/{id}/vote` (confirm raises it, dispute lowers it) - this is
the crowd-corroboration mechanism, and it's anonymous by the same design
choice as reporting itself.

**Fire pipeline specifics**: `scripts/ingest_firms.py` now runs every
detection through this same confidence/tier logic (it previously bypassed
it entirely, so a real satellite-confirmed fire could never reach
`/alert/recommended` no matter how severe - fixed). Two things worth
knowing: (1) each FIRMS run treats the incidents table as a fresh 24h
snapshot for `source='bulk' AND type='fire'` rows only, deleting the
previous run's before inserting - FIRMS data is rolling, not historical,
so keeping old detections around would pollute the risk surface with
stale "fire happened here" signal; citizen/officer reports are untouched
by this. (2) `count_nearby_reports()` takes an `exclude_source` param used
here to stop adjacent VIIRS pixels from the same fire (a real fire lights
up several nearby sensor pixels in one pass) from counting as independent
corroboration against each other - only a citizen/officer report nearby
adds a corroboration bonus to a bulk detection. Known limitation, not yet
solved: every FIRMS detection that survives the significance filter lands
in `critical` tier, since sensor confidence (0.8) exceeds the hazard
critical threshold (0.6) regardless of the fire's actual scale - FRP
(radiative power, already present in the raw data) could differentiate a
borderline 10MW detection from a 200MW blaze, but isn't used for that yet.

**Medical pipeline specifics**: unlike fire, there's no equivalent of FIRMS
for medical emergencies - no public sensor feed detects "someone collapsed
here." The report -> confidence -> corroboration -> tier flow is otherwise
identical to every other category. What medical specifically needed
instead was a different kind of routing: risk-avoidance safe routing
(`/route/safe`) answers "route away from danger," which is the wrong
question for a medical emergency - the useful one is "route toward real
help." `scripts/ingest_health_facilities.py` pulls real hospital/clinic
locations from the Kenya Healthsites dataset (healthsites.io via HDX,
refreshed roughly every 90 days - a slowly-changing registry, updated by
`osm_id` on re-run rather than replaced like FIRMS's rolling snapshot),
filtered to hospital/clinic/doctors (960 with usable coordinates as of
this writing; ~26% of the raw dataset lacks point coordinates - OSM
building-outline features without a computed centroid - and is skipped,
not force-fit). `GET /facilities/nearest` ranks real facilities by
haversine distance from any point; `has_emergency` is only reliably
populated for ~4% of entries (an OSM tagging gap, not a data quality
signal about the facility itself) so it's surfaced as a bonus badge, not
used as a hard filter.

**Alert tiers** (`alert_tier()`): `in_app` -> `sms_recommended` -> `critical`,
at category-specific thresholds (hazard/medical cross into `sms_recommended`
at 30% confidence and `critical` at 60%, matching the original design
discussion's illustrative numbers; crime is set higher - 50%/80% - since an
unproven crime accusation reaching a wider audience carries real cost if
wrong). **This tier is a recommendation, not an automatic trigger** -
`GET /alert/recommended` (officer-only) surfaces incidents that have crossed
a threshold, but actually sending an SMS broadcast still goes through the
existing officer-gated `/alert/broadcast` endpoint. Auto-firing a real mass
SMS off a brand-new, unvalidated scrappy heuristic was a deliberate line not
to cross yet - see "Explicitly not yet built" below.

## Explicitly not yet built

- Street-level turn-by-turn routing (current routing is grid-based, not
  road-graph-based).
- Real (non-synthetic) incident data — training data today is generated by
  `scripts/generate_synthetic_data.py`, not sourced from real records.
- A live SMS provider behind the broadcast system (console-only for now;
  see `src/broadcast.py`).
- Real safe-zone locations for crime/flood/fire (police stations) —
  "safe" for risk-avoidance routing still means "lowest-risk nearby cell,"
  not a verified point of safety. Partially resolved for medical: real
  hospital/clinic locations now exist (see "Medical pipeline specifics"
  below) - police stations are the remaining gap.
- Cell Broadcast (SMS-CB) for true no-opt-in-required reach — current
  broadcast only reaches people who've subscribed via `/subscribers`.
- Fine-grained national risk resolution — the general risk surface is
  ~10km/cell nationally (routing gets ~800m locally, see above). Building
  the whole country at city-block resolution is a real cost (~25x more
  cells) deferred deliberately, not solved; if it becomes a bottleneck,
  the fix is querying/caching a local window per request instead of
  building the full national grid every time, the same pattern routing
  already uses.
- Calibrated/absolute risk scores — the 0-1 risk value is normalized
  relative to the current data's own maximum, not an absolute probability.
  Severity buckets (Low/Medium/High, thresholds in
  `risk_surface.severity_bucket`) will shift as more data comes in.
- Real evidence upload — `has_evidence` is a self-attested boolean, not an
  actual photo/video upload with storage, moderation, or reporter-safety
  review before display. No file storage infrastructure exists yet; this
  was scoped down deliberately for the pilot rather than left unaddressed.
- Automatic SMS/alert triggering off confidence score — `alert_tier` is
  computed and surfaced (`/alert/recommended`) but does not fire a real
  broadcast by itself. See "Confidence scoring, visibility scoping, and
  alert tiers" above for why.
- The full role/permission model — only citizen (unauthenticated) and
  officer (JWT-authenticated) exist. Verified-reporter, trusted-verifier,
  admin, and NGO/government-partner tiers are designed in conversation, not
  built.
- Tier-3 "unmissable" full-screen takeover alerts (alarm sound, blocked
  screen, deliberate multi-step dismissal) — confirmed technically feasible
  on Android via full-screen intent notifications, not possible on iOS
  through a normal app (Apple restricts this to their own Critical Alerts
  entitlement, sound+notification only, specially approved per app). Also
  requires a native app - a browser/PWA cannot take over the screen this
  way. Out of scope until the native app exists.
- Live satellite/hydrological cross-referencing for flood confidence —
  Copernicus GloFAS/Sentinel-1 Global Flood Monitoring is the identified
  real, free, self-serve source (see session notes), not yet integrated.
  Google Flood Hub is live and free but its API is currently waitlist-gated.
- Real SMS delivery is code-complete but UNTESTED against a live account -
  `AfricasTalkingProvider` (`src/broadcast.py`) implements the actual SDK
  call, but no real AT_USERNAME/AT_API_KEY have been used against it yet.
  Treat the first real send as a test, not a known-working path.
- A hardcoded JWT secret fallback was removed (`src/auth.py` now generates a
  random per-process secret if `EIS_SECRET_KEY` isn't set) but no real
  secret has been provisioned for any actual deployment yet.

## Data

Training data is 100% synthetic (see `scripts/generate_synthetic_data.py`)
— generated locations, times, and types, no real reports, no real people.
See `privacy_policy.md` for how real citizen/officer reports are handled
once they start flowing in.
