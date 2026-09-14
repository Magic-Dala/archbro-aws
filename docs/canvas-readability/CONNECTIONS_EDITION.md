# Connection readability — 2026-09-06

This revision addresses the supplied screenshots: endpoint hairpins, oversized arrowheads, apparent junction dots, and overlapping relationship paths. It uses the same three frozen reference projects and the English presentation catalog.

## Routing and rendering

Canvas layout v4 aligns the 12-unit terminal runs with the 12-unit card exclusion zone. The previous 18-unit terminal runs could overshoot each other inside a 32-unit card gap and reverse immediately before an arrow. Added parallel routing tracks and a higher shared-segment penalty reduce shared trunks without dropping edges. Positions, hierarchy, IDs, source/target direction, and accepted architecture hashes remain unchanged; route geometry has a new version.

Full Canvas uses continuous strokes and six-pixel arrow glyphs matching the line color. Only the glyph scales inversely with the viewport; its tip stays at the canonical target port. Source junction dots are removed. Four-unit corners and a white casing separate crossings. Hover or keyboard focus emphasizes one relationship; selecting it brings its line above other lines and opens the exact direction and relationship ID in Inspector. Semantic categories remain available in Inspector and API data.

## Observed results

| Frozen reference | Default view: reversals before → after | Default view: shared length before → after | Default view: strict crossings before → after |
| --- | --- | --- | --- |
| Commerce | 18 → 0 | 54 → 0 | 19 → 13 |
| AI workspace | 23 → 0 | 870 → 183.333 | 53 → 47 |
| Manufacturing | 26 → 0 | 504 → 132 | 42 → 40 |

Across all 181 relationships, immediate reversals decrease from 130 to zero. Shared length is the sum of pairwise collinear overlap in canonical coordinates. Counts use the saved English v3 fixtures as the baseline. Full-detail crossing counts do not all improve: commerce 78 → 70, AI 202 → 237, manufacturing 317 → 365. These graphs are not claimed to be crossing-free or sharing-free; the default backbone, authored journeys, and individual relationship highlighting remain the primary reading paths.

Validation: 42 focused Python checks and 4 Node checks pass. The actual renderer checks every visible overview arrow at 100%, 200%, and 400% for a six-pixel glyph, exact target alignment, matching stroke color, and absence of source dots. All three references, nine authored journeys, 27 reversible group collapses, selected dependencies, narrow layouts, canonical invariants, and browser errors are checked. The exported reader also verifies hover isolation and click-through to the exact relationship direction and ID, with zero external requests or page/console errors.

Evidence and interactive preview:
`C:/AI/temp/archbro/2609051136/results/final-consolidated-20260905T0932/canvas-connections-20260906/`

Open `index.html`; use the reference selector for all three projects. Detailed evidence is in `connection-quality.json`, `geometry.json`, `focused-tests.xml`, `views/visual-results.json`, and `showcase-checks.json`. This is offline projection evidence. No authentication, provider, release acceptance, promotion, or deployment is claimed by this revision.
