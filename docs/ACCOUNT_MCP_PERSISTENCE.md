# Account-scoped MCP authorization and handoff regression repair

## Identity and storage contract

First-party OAuth credentials belong to the verified Archbro principal (`user_id`; Firebase UID in production), not a browser, localStorage entry, project, or container process. A new browser session using the same Archbro identity loads the same account connection. A different principal cannot load that credential. Project repository bindings remain independent and still restrict repository reads.

Completed first-party OAuth connections are encrypted with authenticated Fernet encryption before insertion into PostgreSQL. The envelope binds both `user_id` and provider, and read-back validates both identities. Public connection/status responses contain metadata, persistence state and connection ID, never the access token, refresh token or client secret.

A newly authorized connection is verified before replacing the previous committed credential. Failed reconnection leaves the last committed connection intact. An explicit Remove deletes the database credential before reporting success; persistence failure must not pretend removal succeeded. Late saves from removed connections are rejected. Concurrent refresh/commit/remove operations use the existing per-account gateway with a reentrant credential lock; normal MCP reads are not globally serialized. If token refresh succeeds but its database save fails, a subsequent request retries the save before using that refreshed credential rather than rotating the token again.

On process startup, a new registry lazily reconstructs connections from the authenticated user's stored rows. The connection ID is retained. A restored connection is not falsely marked freshly probed: readiness is checked by normal discovery/probe/read operations.

## Deployment prerequisites (not applied by this repair)

The deployment must provide a stable `ARCHBRO_PROVIDER_CREDENTIAL_KEY` together with its PostgreSQL `DATABASE_URL`. Keep the key in the deployment secret store, separate from application images and database backups, and retain it across ordinary deployments. Do not generate a new key on every release. No real key, OAuth configuration or production permissions were changed during this repair.

Startup validates an encrypted key canary. A wrong key fails explicitly rather than presenting existing credentials as absent. Concurrent initializations serialize the schema/canary transaction. Losing the key prevents decrypting existing credentials; replacing it is not a supported rotation procedure.

Production first-party OAuth with no encrypted store is rejected. This does not make GitHub mandatory: a deployment without configured first-party OAuth can still create projects and run general tasks. `INITIAL_ARCHITECTURE` is excluded before provider runtime creation. The project repository setting remains optional until the user asks for repository evidence.

Old memory-only credentials that disappeared before persistence existed cannot be recovered from an empty process. The first cutover may require one controlled authorization or separately approved migration; repeated normal deployments must not require reauthorization afterward. Provider-side revocation or expiry without a valid refresh token can still legitimately require reconnection.

The existing single-worker OAuth transaction constraint is retained. Completed grants persist; an unfinished authorization transaction is still transient. Multi-replica refresh/revocation coordination and mid-OAuth deployment recovery are not claimed by this implementation.

## User feedback and account isolation

OAuth completion refreshes server status and connection metadata, updates provider cards and the connected count, opens the Connected view, and shows an accessible success notice. No tab-switch is required. Saved first-party credentials and session-only local helpers are labeled differently. Removed connections clear stale success notices.

Connection/status reads carry account-generation guards; an old account's delayed response cannot repaint another account after logout/login. Within one account, an invalidated provider status cannot overwrite a newer connect result. Frontend identity checks protect display freshness only; backend authorization remains authoritative.

Custom stdio/manual connections and host-bound gcloud helpers are not silently converted into portable account credentials. Other first-party OAuth providers share the storage boundary, but live provider acceptance beyond GitHub has not been performed in this repair.

## Repository and navigation corrections

The handoff's eight issues are implemented in the same local candidate:

1. Same-projection History navigation retains a pending map. A current resource whose projection really changed gets one bounded replacement or an explicit settled retry state; cross-project guards remain.
2. Execution targets inside straight, curly, CJK, single or backtick quotes are parsed before discovery. Repository scope rejection becomes terminal for that session; no bound-repository fallback or stale successful evidence is usable afterward.
3. Branch/path operands are distinguished from explicit repository targets using context, not only directory-name blacklists. Bare repositories, GitHub URLs including `.git` and file URLs, and repo/repository prefixes remain supported.
4. Explicit sidebar Tasks and `view=tasks` win over remembered Review; explicit `workspace=review` still works.
5. Workspace home hides and makes project tab panels inert, clears task-detail ownership, and keeps refresh/empty-home behavior clean.
6. Remaining visible uppercase Personal Workspace text is renamed.
7. The small-phone header gives the title a full row beside the sidebar control and moves auxiliary controls to another row. Touch targets remain 44px; delayed sidebar focus cannot override a user's chosen focus.
8. Unbound project GitHub tools return `PROJECT_REPOSITORY_REQUIRED` with `OPEN_PROJECT_REPOSITORY_SETTINGS`, not an incorrect reconnect instruction.

## Reproducible local checks

Use a disposable PostgreSQL database, never the live application database. Existing `tests/conftest.py` creates a separate test schema per case and drops it afterward.

```text
python -m pytest tests -q
node --test qa/test_*.mjs
python -m pytest qa/playwright_handoff_regression.py -q
```

Browser tests use real Chromium and real frontend assets with an isolated recording API fixture. They do not authorize GitHub, contact the deployed application, call a paid model, or modify real projects/repository bindings. A fixture-only DELETE verifies last-project cleanup. The OAuth completion browser test is a simulated successful provider callback, not a real OAuth grant.

The handoff regression tests were first run against the unfixed source: repository tests reproduced eight failures; navigation tests reproduced three failures. Three additional persistence race tests reproduced failures before their correction. Test receipts, final content hashes and the complete review patch are retained with the local worktree's repair report.

## Explicitly not performed

- Production deployment, push, pull request, merge or real OAuth/secret changes.
- Live GitHub authorization, real cross-computer login, real provider revocation and token rotation.
- Multi-replica acceptance or encryption-key rotation.
- Linux-only deploy shell acceptance and paid-provider tests when skipped by the local platform/configuration.

Local fixture verification must not be described as a deployed fix.
