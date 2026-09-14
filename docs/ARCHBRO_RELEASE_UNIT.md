# ArchBro Release Unit: Runtime / Release Identity Contract

This document defines the role-12 release/runtime boundary used by recovery
integration (`13R`) and final verification (`14V`). It is deliberately **not**
a `FINAL_ACCEPTED` declaration and it is not a final `RELEASE_UNIT.json`.

## Frozen acceptance target

The default isolated acceptance origin is:

- `http://127.0.0.1:8013`

Port `8012` remains the existing service/public-route dependency and must not be
repointed to `8013` merely to bypass an authentication or browser capability
failure. Port `8085` remains the local MCP-sidecar mapping. The temporary
PostgreSQL dependency remains `127.0.0.1:55432` (`archbro-s3-auth-pg`) until the
actual consumers prove it is no longer needed.

An explicitly supplied release unit may instead freeze an HTTPS public origin,
including `https://archbro-jim.magicdala.com`. HTTP is restricted to loopback.
Origins cannot contain credentials, paths, query strings or fragments. The
inspector rejects a different request origin before network access, rejects
cross-origin redirects, and compares the runtime's observed target to the
request origin. A planned origin is never inserted into observed evidence.

The runner does not publish, recreate, or cut over any of these services.

## Identity layers

A valid release observation keeps these identities separate:

1. **Source identity** — exact Git commit SHA and Git tree.
2. **Baked source manifest** — SHA-256 for every runtime file copied from
   `src/` and `frontend/`. `/runtime-identity` re-hashes those files at runtime;
   an image label is never the sole source proof.
3. **Image identity** — Docker image ID and registry/manifest digest observed
   after build. These cannot be known from inside the build and therefore stay
   `null` until an external inspector supplies real observations.
4. **Base identity** — a real immutable image reference containing
   `@sha256:<digest>` plus the extracted manifest digest.
5. **Runtime dependency identity** — actual Python version plus a complete,
   sorted installed-distribution manifest produced inside the image.
6. **Harness identity** — exact SHA-256 of the maintained acceptance harness.
7. **Target identity** — fixed origin and actual auth mode.
8. **Database dependency** — PostgreSQL connectivity and required schema
   relations.

Unknown observations stay `null`. Expected values are never copied into
observed fields.

## Non-secret configuration fingerprint

`release_identity.safe_runtime_config()` only contains non-secret settings.
Firebase browser API-key material is represented as a presence boolean, not its
value. The fingerprint does **not** include:

- `DATABASE_URL`;
- edge tokens;
- OAuth/client secrets;
- bearer tokens;
- cookies;
- storage-state contents;
- credential files;
- raw MCP configuration values.

This prevents a public hash from becoming a durable fingerprint of secret
material.

## Liveness and readiness

`/healthz` remains process liveness and never touches PostgreSQL.

`/readyz` is the public cheap readiness boundary. It checks only:

- Firebase public browser configuration when Firebase auth is active;
- the API principal/auth-mode boundary.

It deliberately does **not** open PostgreSQL. External callers therefore cannot
turn a public readiness endpoint into repeated schema work.

`/internal/readyz` is loopback-only and adds PostgreSQL connectivity plus the
required ArchBro schema relations. The schema check is one bounded query over
the frozen required-relation list rather than one query per relation. Candidate
container healthchecks use this deep endpoint.

It explicitly reports `provider_probe = "not_run"`. Readiness must never make a
Gemini/Strands/provider request or any other paid live-model call; those belong
to `14V`.

`/runtime-identity` and public `/readyz` are intentionally non-secret probe endpoints
and may be called from inside the container without the edge token. They do not
grant an API principal. Ordinary API and frontend traffic keeps the existing
edge/auth boundary. `/internal/readyz` additionally requires a loopback client.

## Role-11 preflight handoff

Role 11 publishes `archbro.release_unit.v1` and
`archbro.release_acceptance_evidence.v1`. The final frozen release unit is owned
by `13R`, not Role 12.

`RELEASE_UNIT.expected_identity` must contain seven non-unknown fields:

- `source_sha`
- `source_tree`
- `image_digest`
- `harness_hash`
- `dependency_fingerprint`
- `safe_config_fingerprint`
- `target_origin`

Role 12 supplies **observed identity separately**. Source and runtime dependency
proof can originate in `/runtime-identity`, but image identity and acceptance
harness identity cannot be self-attested by that runtime. The release verifier
uses an external Docker/OCI observation for image digest/ID and computes the
canonical hash of its own trust-bearing harness files. A runtime declaration is
only a consistency check. Unknown or mismatched authority blocks fixture
admission before browser work.

The lower-level runner also accepts `archbro.release.preflight.v1`. Its
sections are:

- `source`: `git_sha`, `git_tree`, `clean`, `source_manifest_sha256`
- `image`: `id`, `manifest_digest`
- `base`: `reference`, `manifest_digest`
- `runtime`: `python_version`, `dependency_manifest_sha256`
- `harness`: `sha256`
- `target`: `origin`, `container`
- `auth`: `mode`, `browser_session_capability`, `storage_state_mode`
- `database`: `required`, `host`, `port`, `schema_verified`

Missing observations remain `null`. Secret-bearing keys are rejected.

Role-11 auth modes are explicit: `storage_state` is supported as an opaque local
protected mode; `workbench_attach` is accepted only when measured capability
evidence says it is supported. A declared label is not capability evidence.

## Runner interfaces

`qa/archbro_release_runtime.py` provides:

- `plan` — outputs the release unit target, dependencies, dry-run phases and rollback
  capture requirements.
- `preflight` — validates the role-11/13R evidence structure without filling
  unknowns.
- `release-unit-preflight` — compares `archbro.release_unit.v1`
  `expected_identity` to separately observed seven-field identity and refuses
  fixture admission on unknown/mismatch.
- `build` — produces a Docker build command only by default. `--execute-build`
  is explicit and still refuses any base image not pinned with `@sha256`.
  Building an image does not switch a service.
- `inspect` — calls `/runtime-identity` and public `/readyz` at the fixed
  acceptance origin. Deep database readiness is measured by loopback
  `/internal/readyz` on the candidate/runtime lane.
- `observe-image` — externally inspects a digest-pinned Docker image and the
  running container image ID, producing `archbro.release.image_observation.v1`.
- `rollback` — preserves the original owned route/image/config identity.
  Execution remains refused unless the phase is exactly `15D` and all explicit
  promotion gates are true. The actual route mutation stays with the 15D
  deployment executor; no generic shell/Git-history rewrite is embedded here.

## Candidate image build

`deploy/release/Dockerfile.candidate` has no floating default base image.
`BASE_IMAGE` is mandatory and must be a digest-pinned reference. The build
records:

- exact source commit and tree supplied by `13R`;
- exact base manifest digest;
- harness SHA-256 computed by the controller from the canonical trust-bearing
  acceptance harness files; an arbitrary caller-supplied hash is refused;
- runtime file hashes;
- the complete installed dependency manifest. Runtime identity re-enumerates
  installed distributions and publishes the dependency fingerprint only when
  that observation exactly matches the baked manifest.

Image ID and image manifest digest are post-build observations and therefore
must be inspected after build rather than invented in image metadata.

## Browser/auth capability boundary

Use supported operations for the existing Workbench/Firebase session, under
the execution client's actual authority. Threaden is an optional coordinator;
a user-authorized direct repository workflow does not need a Threaden lane.
An actual browser or host denial remains a blocker for that operation. Do not
replace authentication with synthetic principals, raw CDP session extraction,
or bearer-token transfers. Protected auth state stays local and opaque.

The direct workflow records `promotion_admitted` for independently established
promotion admission. Older `threaden_promotion_admitted` ledgers remain readable.
Neither field substitutes for full 14V acceptance, explicit user launch, or
exact CAS; a false current admission never falls back to a true older field.

## Local candidate configuration

`deploy/release/compose.local-candidate.yml` and `local_entrypoint.py` capture
the Jim runtime setup in this repository. The candidate reuses the existing
`archbro-jim-config` volume and an explicitly supplied existing ADC file as
read-only mounts. The service runs as UID/GID `10001:10001`, matching the owner
of the existing mode-0400 `runtime.env`; its permissions are preserved.

The entrypoint loads the existing literal `KEY=value` configuration before app
creation. Explicit container settings take precedence. `ARCHBRO_DB_HOST` and
`ARCHBRO_DB_PORT` change only the DSN endpoint, preserving its credentials and
other options. Compose defaults to the observed local DB mapping
`host.docker.internal:55432` and publishes the candidate on loopback `8014`.

Before starting, supply the independently inspected `ARCHBRO_IMAGE_REFERENCE`
(digest-pinned), `ARCHBRO_IMAGE_ID`, `ARCHBRO_IMAGE_MANIFEST_DIGEST`, the frozen
`ARCHBRO_ACCEPTANCE_TARGET_ORIGIN`, and `ARCHBRO_ADC_FILE`. Compose has no build
step or automatic image pull. The image must contain the checked-in entrypoint;
older images do not gain it by changing the source checkout. The candidate
Dockerfile includes the entrypoint in its baked runtime source manifest.

Validate configuration with `docker compose -f deploy/release/compose.local-candidate.yml config --quiet`.
Starting this candidate does not change the public route or promote Git.
After an authorized reversible route activation, measure identity at the
public origin itself. Loopback identity and a configured target string alone
are not public routing evidence. Any changed source/harness/image requires a
fresh release freeze and attempt; prior image acceptance does not transfer.

## Rollback invariant

Before any 15D cutover, capture the exact owned:

- route identity;
- image ID;
- image manifest digest;
- safe configuration fingerprint.

Rollback restores those identities. It never rewrites Git history, never
changes an unrelated tunnel/container, and never treats a healthy old
container as evidence that the new release role is satisfied.
