# Readable approach lanes and source tracing — 2026-09-06

Layout v8 reserves space for connections before routing them. The previous leaf-group title gap was 28 units; two 12-unit obstacle margins left only 4 units for parallel approaches. Leaf-level entry gaps are now 60 units, and sibling gaps 52 units. The geometry remains backend-owned, deterministic, and independent of viewport or selection. Cards remain 224 × 80; accepted architecture, node/edge IDs, directed semantics, and the three frozen reference inputs are unchanged.

After the initial shortest-path search, a bounded joint pass separates nearby parallel segments, exchanges crossing lanes, and swaps ports on the same card side when that improves the combined drawing. Crossings among the relationships of high-degree nodes receive more weight. A move must clear obstacles, preserve segment directions and bend count, and stay within 48 units of the initial vertices; route length may grow by at most 48 units. Clear two-point connectors are fixed. A reciprocal pair participates once and returns two exact reversed canonical routes. This is a bounded heuristic, not a claim of globally optimal or crossing-free layout.

## Reading a connection

Hover a line or a relationship in the Inspector, or focus a line with the keyboard. The exact route and its two endpoints are highlighted together. A compact readout names the directed source, destination, and action. Both directions are listed for a reciprocal connector. The Inspector row uses the same connected-boundary color. Leaving or blurring restores the view without changing selection, viewport, layout, or accepted architecture.

The temporary highlight paints a copy of the existing route above other lines; it does not move the focusable SVG element. All route points remain canonical. Labels for every focused relationship no longer occupy the shared approach lanes: on-route labels are reserved for a chosen relationship or an authored journey. Direction arrowheads and boundary colors retain their previous meaning. Existing white casings distinguish ordinary crossings from junctions.

## References and adopted principles

- [Eclipse ELK repository](https://github.com/eclipse-elk/elk) and its [edge-spacing contract](https://eclipse.dev/elk/reference/options/org-eclipse-elk-spacing-edgeEdge.html): line separation needs explicit geometry space. ArchBro reserves that space in the server projection.
- [Adaptagrams/libavoid repository](https://github.com/mjwybrow/adaptagrams) and [routing options](https://www.adaptagrams.org/documentation/namespaceAvoid.html): separate overlapping segments and corners, coordinate shared-endpoint routes, and preserve short paths. ArchBro applies an independently implemented bounded channel pass, keeping its own canonical model and reciprocal identities.

These were consulted on 2026-09-06. No routing dependency was installed and no third-party implementation was copied.

## Evidence

The three fixed reference selections improve as follows. Crowding is the summed parallel overlap length times the shortfall from 12-unit spacing; it includes near overlaps that the old exact-overlap check missed.

| Selection | Directed IDs / visual lines | Crossings before → after | Crowding before → after | Shared length after |
|---|---:|---:|---:|---:|
| Business event bus | 9 / 8 | 5 → 1 | 3488 → 0 | 0 |
| Agent worker | 9 / 9 | 8 → 3 | 2328 → 0 | 0 |
| Manufacturing execution | 15 / 13 | 22 → 13 | 9132 → 1908 | 0 |

The manufacturing selection still contains nearby segments and crossings. Showing every relationship in each full graph also retains some shared/nearby paths; the detailed measurements include those cases. The hover/focus inspection works for all 181 directed relationships (169 visual connectors), including both members of all reciprocal pairs. Zero crowding is not claimed for the entire corpus.

Validation: 48 focused Python checks and six Node checks pass. Actual-renderer Chromium checks cover all 181 directed source/target readouts, preserved keyboard focus, restored hover state, unchanged route geometry/viewport/selection/architecture, 40 domain-color assertions, nine authored journeys, 27 reversible collapses, three zoom levels, and narrow widths. No card/header intersections, text overflow, or page/console errors were observed. The exported offline reader was separately checked for all three samples, all relationship IDs, actual Inspector hover, and zero external requests.

Evidence and interactive preview: `C:/AI/temp/archbro/2609051136/results/final-consolidated-20260905T0932/canvas-edge-tracing-20260906/`. Open `index.html?sample=01-commerce&node=event_bus`. Backend fixtures were regenerated because layout changed. The 178 DB tests and old 275 checks were not rerun. This is offline canvas evidence; no 14V, promotion, or production deployment is claimed.
