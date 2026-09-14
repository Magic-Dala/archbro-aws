# ArchBro repository closeout — 2026-09-05

The working checkout is `C:/AI/archbro`, branch
`fix/final-closeout-20260905`, based on the complete consolidated commit
`6f93f3cfe78dbc944ff2879e785e83086b64dc5d`. This repair uses the existing
checkout directly, as requested. No Threaden controller, additional worktree,
Vyrnu change, Git history rewrite or canonical promotion is part of it.

## Reconciled source

The 21 recorded pre-consolidation heads remain preserved by archive refs and
the original history bundle. Divergent lane histories were integrated as
source in `b222d9f` and `2b3d186`; historical commit ancestry alone is therefore
not a reliable missing-feature test. The reconciliation compares file contents
with the current tree and its history, then reviews differing implementations.

Of the 27 paths in the preserved dirty draft:

- 23 match the current source after line-ending normalization.
- `archbro-webmcp.js` has its exact draft version in current history, followed
  by the accepted active-project/deep-link repair.
- `app.js` had omitted the distinction between unavailable provider usage and
  an agent that has never run. This repair restores it and recognizes any
  recorded run; a genuinely observed zero still displays `0 tokens`.
- `qa/test_architecture_canvas.mjs` retains the newer project-sync assertions
  and now executes the real Context Tray renderer for missing, null and zero
  usage, instead of recovering the old source-string assertions.
- `gemini.py` retains the current invocation-isolated metadata and stricter
  provider failure classification. The older draft would remove those fixes.

Older unmatched committed file versions were also reviewed: current sources
retain the later Canvas path/Explore/viewport coverage, bounded-context and
stale-preview checks, safe telemetry serialization and protected provider
failure classification. The earlier release-runtime test's credential-shaped
literal has already been sanitized. These older versions are preserved in the
archive, not copied over their tested successors.

## Repairs now owned by this repository

- `deploy/release/local_entrypoint.py` and `compose.local-candidate.yml` capture
  the existing local runtime setup, including UID 10001, opaque read-only
  configuration and explicit DB endpoint overrides (local default 55432).
- The candidate Dockerfile includes the entrypoint in its source manifest.
- The release inspector accepts an explicitly frozen HTTPS public origin as
  well as loopback HTTP. It rejects wrong targets, cross-origin redirects and
  missing observed target identity. Public plans leave unknown local app ports
  unknown.
- Direct promotion admission can be recorded without Threaden, while full 14V,
  explicit user launch and exact-CAS gates remain required.
- Context Tray reports unavailable actual usage accurately without fabricating
  a number.

## Verification and release state

Only affected Python runtime/entrypoint checks, the focused Canvas Node checks
and Compose configuration validation were run. The previously passed 178 DB
tests, 275 retained checks and unchanged long browser suites were not rerun.

The existing candidate image belongs to the earlier `6f93f3c` release. This
repair changes source, Docker inputs and harness files, so it needs a new image
and release freeze before a fresh 14V attempt. No image rebuild, public route
switch, Firebase fixture, paid-provider request or 15D deployment happened in
this repository-repair step. Source integration is not final acceptance.

Machine evidence and the archive reconciliation are under:
`C:/AI/temp/archbro/2609051136/results/final-consolidated-20260905T0932/repo-repair-20260905/`.
