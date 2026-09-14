# ArchBro Canvas readability — decisions from 2026-09-06 discussion

## Confirmed by the user

- First open: fit the entire architecture in the viewport. Keep every hierarchy level expanded.
- Full hierarchy does not mean full prose. Hide unnecessary detailed text at overview; disclose details when zoomed or selected without changing canonical geometry.
- Overview connections: show the primary flow only. Selecting a component reveals its other relevant canonical relationships.
- Use https://github.com/tt-a1i/archify as a design reference, without copying the entire product or visual design.
- Continue using the same three retained v1 reference projects and their frozen architecture inputs for comparisons.

## Existing boundaries remain binding

- Backend owns canonical hierarchy, relationship identity and direction, deterministic layout and routes.
- Frontend owns presentation and interaction state. All relationships remain available; visual filtering is explicit and must not claim the displayed subset is the canonical total.
- Full expanded hierarchy remains the default. Do not turn this into root-only navigation.
- Ordinary viewing never mutates accepted architecture. Structural recommendations require human review.
- No Threaden, additional worktrees, unrelated repository changes, or blind reruns of previously accepted suites.

## Reference reviewed (documentation, not a live interaction verification)

- https://github.com/tt-a1i/archify
- https://github.com/tt-a1i/archify/blob/main/DESIGN.md
- https://tt-a1i.github.io/archify/

Relevant ideas: primary path priority, progressive disclosure, compact contextual details, semantic styling, direction-preserving authored relationships, route and label clearance validation.

## Confirmed primary flow and current scope

- Default to the project-wide cross-system backbone, with named business journeys available as reading views. The user accepted this direction with「好定」.
- Do not infer accepted business semantics from component names, canvas geometry, or arbitrary frontend ranking.
- The earlier design-only pause ended when the user requested「好開額外branch 開始實作」and「好開始」.
- Implementation proceeds in the existing repository on feat/canvas-readability-20260906. No Threaden or additional worktree.

See CANVAS_RULEBOOK.md for the detailed design proposal and acceptance rules.

This file records decisions. See README.md and the generated evidence for implementation checks; no production deployment is claimed.
