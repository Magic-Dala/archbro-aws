# Early connection summaries

Compatible connections now converge near their source boundary, leaving one shared trunk across the rest of the canvas. Payment router and Order outbox still represent distinct event actions; the blue summary preserves both directed relationship IDs and exposes their exact source, destination and action on inspection.

## Reading rules

- A summary requires a common endpoint, one direction, the same peer architecture boundary, and the same explicit relationship category. Reciprocal pairs and parallel actions between the same two nodes remain separate from this rule.
- An incoming summary has one arrow at the shared destination and a source count. An outgoing summary shares its approach until it branches toward the destinations, each with its own arrow. It never invents a reverse relationship.
- A filled 4 CSS-pixel dot marks each backend-authored branch junction. Its size stays constant during zoom and resize; it uses the connection color. Ordinary crossings and turns receive no dot. The dot has a larger invisible hit area and reuses the summary hover/focus/click interaction. It is not an architecture component.
- Hover or keyboard focus reveals every individual action and endpoint. Hovering an Inspector row highlights only that member. Click or Enter expands the original connections; **Regroup lines** restores the summary.
- All relationships, authored journeys, Trace Path, partial focus, collapsed endpoints, a selected individual edge, and selection of a source member retain their explicit relationship display.
- Existing hierarchy groups now expose a component count and a fold/expand control. Folding is browser-local and reversible. The default remains the complete expanded hierarchy, without repacking coordinates or automatically hiding architecture.

## Backend geometry

`archbro.connection-summaries.v1`, routing policy `coverage-shared-trunk.v2`, is additive to the existing canvas response. The backend first preserves the existing primary route, then evaluates compatible peer coverage before using total shared ink as a tie-break. When the fixed primary cannot cover the group, it may evaluate another existing representative as the trunk, but only when at least two peers join safely and the grouped result reduces independent ink by at least 20%. Candidate anchors remain derived from existing routes and obstacle boundaries. This is a bounded deterministic search, not a claim of a globally optimal Steiner tree.

New branches preserve the source port and outward direction, avoid padded component cards and hierarchy headers, and cannot share a segment with an unrelated connection. Hairpins and loops are rejected. A member's added distance is limited to 24 canvas units plus the shared hub-port displacement, capped at another 24 units. The common hub node and relationship semantics stay fixed. Crossing lines retain their casings. A safe existing intersection remains a fallback when an earlier merge cannot satisfy these constraints.

The backend supplies the summary paths, full member paths and shared trunk. The browser validates identity/version/membership and renders that projection; it does not calculate a second layout. Original canonical routes, nodes, group frames, accepted architecture, IDs, reading views and presentation remain unchanged and are restored on expansion.

## Verification

- 15 focused Python checks passed: summary eligibility, early merge, obstacle and unrelated-line avoidance, port allowance, reversal/loop rejection, deterministic input ordering and the canvas API.
- 8 Node checks passed, including exact member selection, view exclusions, normalization, reversible collapse and asset content hashes.
- Three frozen projects retain 116 nodes and 181 directed relationships. Ten summary groups preserve 20 member relationships. All member paths clear cards and titles; accepted input and complete original geometry compare equal to the previous edition.
- Headless Chromium checked all 181 individual relationships, all 10 summaries and 20 individual source traces, expansion/regrouping, actual group fold controls, nine journeys, all 27 hierarchy groups, arrow sizing, text bounds, desktop and narrow viewports. No recorded console/page errors.
- The exported reader passed independently for all three samples, with no external requests. Existing DB/offline long suites and unchanged layout generation were not rerun.

For the highlighted commerce pair, the junction moves from `(1098, 812)` to `(672, 800)`, beside Order outbox. The shared trunk grows from 113.3 to 551.3 canvas units; separate routes no longer continue side by side across the destination boundary. Coordinates describe deterministic canvas geometry, not screen pixels or runtime telemetry.

These are offline product/renderer checks. They do not establish real-auth/provider acceptance, 14V, 15D or a deployment. This edition is not deployed.

Evidence: `C:\AI\temp\archbro\2609051136\results\final-consolidated-20260905T0932\canvas-summaries-20260906`.

## Focused reproduction

```powershell
python -m pytest tests/test_canvas_connection_summaries.py tests/test_canvas_api_unit.py -q
node --test qa/test_architecture_canvas.mjs
python qa/canvas_readability_views.py --fixtures <evidence-directory> --output <output-directory>
python qa/export_canvas_showcase.py --fixtures <evidence-directory> --output <output-directory>/index.html
```

The evidence directory also includes the projection invariance checker, exported-reader smoke script and source checkpoint. Summary eligibility is conservative: different boundaries/categories, reciprocal/parallel actions, unavailable members and routes without a safe merge stay explicit. Fixed examples do not establish readability for arbitrary graphs with thousands of nodes.

## Junction marker refinement

The follow-up marker change passed the existing 8 Node checks and focused exported-reader checks for all 10 summaries across the three samples: exact backend junction coordinates, 4px size at Fit/100%/zoom-in, connection color, pointer hover, click expansion, regrouping and restoration of the original geometry. All 181 directed relationships remain available; ordinary connections have no junction markers. No page/console errors or external requests were recorded. Backend routing and accepted fixtures were unchanged, so backend suites were not rerun.

Evidence: `canvas-junctions-20260906/junction-marker-checks.json`, alongside `canvas_junction_marker_checks.py` and the interactive `index.html`. Run the saved script with `--output <evidence-directory>` to repeat the focused check.
