# Hub connection readability — 2026-09-06

The Event bus screenshot exposed two routing faults: horizontal-first side selection packed six visual connectors into its left edge, even for peers far above it; the congestion score penalized shared segments but ignored almost-coincident parallel lines. Both faults made distinct relationships look joined near the hub.

Canvas layout v7 chooses the dominant separation axis for diagonal peers while retaining straight-first routing for aligned peers. Port spacing increases to 24 units where capacity allows. The router charges for parallel overlap within a 12-unit corridor, including terminal segments, rather than just exact shared grid links. Unobstructed straight connections remain exempt from congestion penalties. Canonical direction, reciprocal membership, node geometry, and accepted Architecture remain intact.

For Event bus, all 9 directed relationships remain visible as 8 visual connectors. Port distribution changes from left 6 / top 1 / bottom 1 to left 4 / top 3 / bottom 1. Minimum same-side port spacing increases from 10.4 to 17.333 units. Pairwise collinear shared length among those eight connectors decreases from 298 to zero. These are local observed measurements, not a claim of zero crossings across every graph.

Selected-component connections now use blue for incoming, green for outgoing, and purple for reciprocal relationships. Arrow direction and the Inspector's labeled relationship list remain authoritative, so color is an additional reading aid. The legend describes direction relative to the selected component, not a new relationship category.

Validation: 48 focused Python tests and 5 Node checks pass. The new Event bus regression requires all directed IDs, eight visual paths, port spacing of at least 16 units, no unrelated shared segment, and no card/header intersections. The renderer verifies the selected Event bus view, all three frozen corpora, nine authored journeys, reciprocal selection, 27 reversible group collapses, narrow layouts, and arrow alignment at 100%, 200%, and 400%, with zero page/console errors. The exported reader accepts a validated node query for direct comparison; unknown nodes fall back to the overview.

Evidence: `C:/AI/temp/archbro/2609051136/results/final-consolidated-20260905T0932/canvas-hub-20260906/`. See `event-bus-comparison.json`, `geometry.json`, `focused-tests.xml`, `views/visual-results.json`, and the exported reader `index.html?sample=01-commerce&node=event_bus`. Offline projection evidence only; no live acceptance or deployment is claimed.
