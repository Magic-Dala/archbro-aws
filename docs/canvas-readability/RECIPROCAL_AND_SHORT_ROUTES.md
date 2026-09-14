# Reciprocal connectors and shorter routes — 2026-09-06

Canvas layout v5 addresses two observed cases: a pair of opposite relationships that looked like unrelated wires, and Checkout → Payment taking a long perimeter detour despite an open channel.

The backend recognizes exactly one relationship in each direction between the same two distinct endpoints. It allocates one route, emits both original directed IDs with reversed point sequences, and marks their routing as ORTHOGONAL_CANVAS_RECIPROCAL. It aligns free reciprocal ports where possible. More than two relationships, parallel actions in one direction, and self loops remain separate. Labels, categories, protocols, provenance, and accepted Architecture data are never merged.

The frontend combines a pair only when both members are visible and the server-provided routes are exact reversals. A single visible direction stays single. Authored journeys and Trace Path retain directed displays. Selecting a reciprocal relationship exposes both directions and their individual facts in Inspector. Shown relationship counts count both IDs; the separate line count reflects the one visual connector. No browser routing or inferred reverse relationship is introduced.

The long Checkout detour had two causes: large congestion penalties and full-width group-header obstacles. Header labels now occupy a bounded region (up to 300 units at the default configuration); empty header space is traversable while label rectangles and cards remain protected. Distance dominates the route score, with small bend/crossing preferences and a congestion premium capped at 20% of a segment's length. This is not a claim that every route is a global shortest path.

Observed Checkout → Payment route length decreases from 1108 to 500 units, matching the Manhattan lower bound between its ports. Three fixed corpora preserve all 181 directed relationships, accepted hashes, and group frames. Their complete views contain 4, 1, and 7 reciprocal visual pairs respectively. The new header-label bounds are intentional geometry changes; layout v5 must not be presented as the previous release unit.

Validation: 44 focused Python checks, 5 Node checks, and the actual browser renderer across all three corpora. Checks cover obstacle avoidance, deterministic output, reciprocal IDs/reversed geometry, multi-edge exclusions, single-direction journeys, exact direction switching, relationship counts, zoom glyphs, reversible collapse, English text containment, and unchanged accepted data. Browser console/page errors: zero.

Evidence and interactive reader:
`C:/AI/temp/archbro/2609051136/results/final-consolidated-20260905T0932/canvas-reciprocal-20260906/`

See `route-comparison.json`, `geometry.json`, `focused-tests.xml`, and `views/visual-results.json`. Open `index.html`, select Warehouse system for the two-way example or Checkout service for the shorter route. Offline projection verification only; no live authentication/provider acceptance or deployment is claimed.
