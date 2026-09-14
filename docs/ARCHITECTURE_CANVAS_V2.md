# Architecture Canvas v2

This runbook defines release acceptance, not a second Architecture model. Canonical hierarchy, relationships, stable IDs, semantic kinds, layout, edge routes, versions, Trace Path, and Agent Context Manifest remain backend/domain-owned. The browser owns only local reading interaction such as selection, viewport, Focus/Isolate, collapse, tabs, and semantic disclosure.

## Required gates

Stage 3 covers full Canvas hierarchy, source-backed node/edge Inspector, Focus/Isolate/Clear, Collapse/Expand, pan/wheel/+/-/Fit/100%, semantic zoom, valid and invalid deep links, canonical Trace Path statuses, Context Tray preview/execution hash equality, stale-preview fail-closed behavior before provider dispatch, responsive and keyboard behavior, network mutation audit, Observation no accepted-Architecture mutation, structural Recommendation `PENDING` until Human Review, and a real authenticated project flow when that verifier environment exists.

The deterministic Canvas browser case is selected through `ARCHBRO_FINAL_FIX_CASES=architecture_canvas_interactions`. S2-04 owns that behavior case, so later integration extends Trace Path, semantic zoom, and viewport assertions in the same canonical case instead of S2-06 duplicating product topology or selectors.

## Status and evidence rules

`PASS` means the required check actually executed against the final integration candidate and its assertions passed. `FAIL` means the check executed and failed. `UNAVAILABLE` means a required browser, authenticated session, PostgreSQL runtime, native Site Tools host, or equivalent prerequisite was not available; `UNAVAILABLE` is never promoted to `PASS`. Static syntax/schema checks may support acceptance but cannot replace real browser behavior where the verifier requires browser evidence.

All Stage 3 verifiers must name and inspect the same `FINAL_INTEGRATION_CANDIDATE_SHA`. Evidence produced from a different commit is supporting history only, not terminal release proof.

## Performance evidence

The harness records deterministic projection/layout timing and serialized output payload size for supported 27-node and 40-node synthetic fixtures. Record median and maximum observed time across repeated runs, but do not derive or claim a production latency SLO from synthetic timing. Real-browser responsiveness is separate evidence and is required only when that browser environment is available.

## Authentication and cleanup

`qa/playwright_release_acceptance.py` may consume an externally supplied `ARCHBRO_STORAGE_STATE` for a real authenticated browser session. The harness must not synthesize a Firebase or production principal. When no real storage state is supplied, the legacy local-demo run remains clearly classified as local-demo evidence. Missing requested storage state is `UNAVAILABLE`.

Temporary projects and private test database schemas are disposable acceptance fixtures only. Cleanup must never delete a project that existed before the run. Release promotion remains outside this harness and requires the terminal same-SHA verifier set to pass independently.

## Legacy hierarchy diagnostic

`qa/probe_browser_hierarchy_drill.py` predates Canvas v2 semantic disclosure and may encode presentation assumptions such as globally hidden edge-label opacity. S2-06 keeps that probe as diagnostic evidence only. The hard browser gate is the maintained `architecture_canvas_interactions` case plus the surface/runtime gates; a legacy diagnostic mismatch cannot override those newer contracts, and S2-06 must not patch product behavior merely to satisfy the old presentation assumption.
