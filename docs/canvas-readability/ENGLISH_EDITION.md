# English reference collection

The September 6 English edition builds on `6849917`. All three frozen projects now have English titles, component names, responsibilities, relationship actions and journey labels. These are version-pinned display translations: the accepted architecture, identifiers, directions, counts and corpus hashes remain unchanged. Other projects retain their authored text.

The Canvas uses a single project heading, a compact reading toolbar, quiet domain colors and clearer directional arrows. Selecting a journey highlights its path and shows an ordered, clickable sequence while retaining the whole hierarchy. English wrapping respects word boundaries; long identifiers and CJK text remain supported. Domain colors distinguish containers and do not encode health or permission.

Backend layout `archbro.canvas-layout.v3` orders sibling subtrees using canonical call, event and data relationships. The fixed union of the project's authored journeys receives higher ordering and routing priority. This union is identical for every reading view; browser selection never reorders nodes or routes. Cycles retain their actual direction. All relationships remain available.

The English display catalog ships as package data. The API returns it separately from the canonical Diagram, bound to its architecture hash and version. The renderer applies only label and responsibility fields, retaining original text alongside the displayed values. No names are used to infer topology, no production project is rewritten, and no auth/provider behavior is changed.

## Interactive preview

Open the single-file reader:

`C:\AI\temp\archbro\2609051136\results\final-consolidated-20260905T0932\canvas-english-20260906\index.html`

Use the reference selector to switch projects. The real renderer supports reading-view selection, component and relationship inspection, pan, zoom, Focus, Isolate and local Collapse. The footer identifies it as an offline reference. Authentication, live API, provider actions and live Trace Path are unavailable in this exported reader. The production app retains those capabilities.

To reproduce after generating the fixed envelopes:

```powershell
python qa/canvas_readability_fixtures.py --output <evidence-directory>
python qa/canvas_readability_views.py --fixtures <evidence-directory> --output <evidence-directory>/views
python qa/export_canvas_showcase.py --fixtures <evidence-directory> --output <evidence-directory>/index.html
```

## Evidence and limits

- 40 focused Python checks passed, including English coverage, unchanged canonical facts, exact-version withholding, deterministic reordered inputs and shared FULL/MAP geometry.
- 4 Node checks passed, including asset content hashes and nested-collapse direction preservation.
- All nine English journeys, selected direct-edge lists, 27 collapse/expand groups and Isolate/Clear were exercised in the actual renderer. Visible overview, selected Inspector and journey text were checked for untranslated CJK content. At 100%, no card text overflow was detected.
- Desktop, 760px and 390px viewports were checked for horizontal overflow. Full-system names become small at phone-width Fit; zoom and selection provide detail.
- The exported reader was exercised across all three projects with zero external requests and zero console/page errors. The packaged wheel contains the English catalog; that wheel is a packaging check, not a verified deployment release unit.
- The complete corpus remains 116 nodes and 181 relationships. No provider, DB, real-auth acceptance, promotion or deployment was run for this edition.

Dense all-relationships views still contain crossings and shared segments. Compared with the preceding layout, results are mixed: commerce/manufacturing overview crossings decreased (23→18 and 55→41), while AI overview crossings increased (30→53). Full-view crossings are 77, 199 and 314. Fixed journeys have zero shared segments in the measured geometry; six of nine have zero strict perpendicular crossings. These are measured layout facts, not a claim that all routing problems are solved or that users have judged this version superior.

`ENGLISH_VALIDATION.json` records checked source hashes. The evidence directory contains `focused-tests.xml`, `route-quality.json`, `geometry.json`, `views/visual-results.json`, `showcase-checks.json` and the screenshots. The preceding `VALIDATION.json` remains historical evidence for the first edition.
