# Connection colors follow architecture boundaries — 2026-09-06

When a component is selected, each directly connected line and arrow takes the color of the other endpoint's top-level architecture boundary. Nested subsystems inherit their top-level boundary color. A same-boundary connection uses that shared boundary color; a reciprocal connector uses the same peer-boundary color in both directions. Arrowheads continue to express direction.

The frame, line, and Inspector badge share one domain palette lookup. Each related relationship in the Inspector includes a color dot and the boundary's display name, so color is not the only cue. The previous incoming-blue / outgoing-green / reciprocal-purple mapping is removed. Without a selected component, overview lines retain their neutral presentation.

This is a frontend presentation change. Layout v7, routes, ports, reciprocal membership, fixture data, and accepted Architecture are unchanged. The three existing backend fixture envelopes are reused byte-for-byte from `canvas-hub-20260906`; no backend or database suites were rerun.

Validation: six Node checks pass, including incoming/outgoing equivalence, reversed selection, nested membership, same-domain edges, self loops, and absence of fabricated color associations for unrelated edges. The actual browser checks 40 directed relationship presentations across the three reference selections and Event bus: line and arrow stroke match the peer root frame, while the Inspector dot and label match that same domain. All three references, nine journeys, reciprocal selection, 27 group collapses, zoom checks, and narrow layouts pass with zero page/console errors.

Preview and evidence: `C:/AI/temp/archbro/2609051136/results/final-consolidated-20260905T0932/canvas-domain-colors-20260906/`. Open `index.html?sample=01-commerce&node=event_bus`. This remains offline projection evidence; no release acceptance or production deployment is claimed.
