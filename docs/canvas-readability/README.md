Latest refinement: [Early connection summaries and reversible component groups](CONNECTION_SUMMARIES.md).

Latest refinement: [Readable approach lanes and source tracing](EDGE_TRACING.md).

Latest refinement: [Connection colors follow architecture boundaries](DOMAIN_COLORS.md).

Latest refinement: [Hub connection readability](HUB_CONNECTIONS.md).

Latest refinement: [Straight connections first](STRAIGHT_FIRST.md).

Latest refinement: [Reciprocal connectors and shorter routes](RECIPROCAL_AND_SHORT_ROUTES.md).

Latest connection and arrow refinement: [Connections edition](CONNECTIONS_EDITION.md).

# Canvas readability implementation — 2026-09-06

The current English edition and interactive reference reader are documented in [ENGLISH_EDITION.md](ENGLISH_EDITION.md). The material below records the first edition at `6849917`.

The full-system Canvas now opens with every hierarchy level visible and the whole graph fitted into the available drawing area. It shows the backend's project backbone initially; selecting a component reveals all of its direct canonical relationships. The reading selector also offers all relationships and the nine authored journeys in the three frozen reference projects.

Backend geometry uses `archbro.canvas-layout.v2`: compact cards and nested frames, deterministic packing, distinct endpoint ports, and orthogonal obstacle-aware routes. Every relationship retains its own direction and ID. The frontend filters visibility without recomputing layout. Long responsibility text moves to the Inspector; measured labels stay within cards. The Inspector opens on selection or through **Details & Agent**. Journey order is available there when no component is selected.

Fit, pan, zoom and density calculations use the actual SVG aspect ratio. Collapse clips canonical routes at the collapsed frame, preserves crossing relationship identity, and restores the original geometry on expansion. Nested collapse preserves unrelated self loops. Ordinary read interactions do not mutate the accepted architecture.

## Verification

- 36 focused Python checks passed: `test_diagram_layout.py`, `test_canvas_api_unit.py`, `test_canvas_readability.py`.
- 4 Node checks passed in `qa/test_architecture_canvas.mjs`, including nested collapse and asset content hashes. An obsolete cache-label assertion was replaced with a check of the actual JS/CSS SHA-256 URL keys.
- Actual repo renderer, headless Chromium: all three frozen projects, all nine journeys, exact selected incoming/outgoing edge sets, all-relationships view, edge Inspector, 27 groups collapsed and restored, Isolate/Clear, 100% text bounds, desktop and 760px width.
- No card/header route intersections in the three reference layouts; no text overflow or browser console/page errors in the recorded renderer checks. Input reordering produces identical geometry. Accepted architecture hashes remain unchanged.
- The corpus remains 36/42, 40/63 and 40/76 nodes/relationships. Overview displays 23, 30 and 33 relationships respectively; hidden relationships remain in the full response.

The renderer harness disables app startup and authentication, and blocks unexpected requests. It uses the real frontend files with fixed accepted input, without a DB, Firebase session or provider. These checks are **offline renderer evidence**, not real-auth/provider acceptance or 14V/15D. Existing DB and long offline suites were not rerun. No deployment is included.

## Reproduce

From the repository root, with project dependencies and Playwright Chromium installed:

```powershell
python -m pytest tests/test_diagram_layout.py tests/test_canvas_api_unit.py tests/test_canvas_readability.py -q
node --test qa/test_architecture_canvas.mjs
python qa/canvas_readability_fixtures.py
python qa/canvas_readability_views.py
```

Generated files go to `qa/playwright_artifacts/canvas-readability/` by default. Both Python scripts accept `--output`; the renderer also accepts `--fixtures` and an optional source `--candidate` directory. No production project is created or deleted.

This run's durable evidence and visual gallery are at:

`C:\AI\temp\archbro\2609051136\results\final-consolidated-20260905T0932\canvas-readability-20260906\index.html`

`VALIDATION.json` records the checked source file hashes and check scope. The external evidence directory also contains `geometry.json`, `route-quality.json`, all fixed envelopes and renderer screenshots.

## Limits

All-relationships views remain dense. Measured strict perpendicular crossings for full/backbone views are 64/23 (commerce), 208/30 (AI), and 317/55 (manufacturing); shared segments also remain. Counts compare two subsets of this layout, not an improvement ratio against the old layout. The router minimizes congestion but does not promise a planar graph or uniform screen direction for cycles.

Named journeys are explicitly tied to the exact accepted hashes of the three frozen samples. They are withheld for changed, missing or ambiguous relationships. Other projects receive the structural backbone and all-relationships view; this change does not implement automatic business-journey generation. Authored journeys are not observed runtime traces.

The 36–40-node corpus does not establish readability for hundreds or thousands of nodes. Real-auth/provider behavior and release identity must be revalidated for a future deployment of this source. See [CANVAS_RULEBOOK.md](CANVAS_RULEBOOK.md) for the design contract and [REFERENCE_SYNTHESIS.md](REFERENCE_SYNTHESIS.md) for reference decisions.
