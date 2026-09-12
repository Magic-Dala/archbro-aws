from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
from pathlib import Path
from typing import Any, Mapping

RUNTIME_IDENTITY_SCHEMA = "archbro.runtime.identity.v1"
RUNTIME_READINESS_SCHEMA = "archbro.runtime.readiness.v1"
SOURCE_MANIFEST_SCHEMA = "archbro.release.source_manifest.v1"
DEPENDENCY_MANIFEST_SCHEMA = "archbro.release.dependencies.v1"
RELEASE_METADATA_SCHEMA = "archbro.release.metadata.v1"

ROLE11_IDENTITY_FIELDS = (
    "source_sha",
    "source_tree",
    "image_digest",
    "harness_hash",
    "dependency_fingerprint",
    "safe_config_fingerprint",
    "target_origin",
)

_DEFAULT_RELEASE_DIR = Path("/app/.archbro-release")
_REQUIRED_DB_RELATIONS = (
    "public.archbro_row_seq",
    "public.projects",
    "public.architectures",
    "public.tasks",
    "public.proposals",
    "public.events",
    "public.agent_runs",
    "public.event_processing",
    "public.planner_checkpoints",
    "public.notes",
)

# These values are safe to expose or fingerprint. Secret-bearing settings such
# as DATABASE_URL, edge tokens, OAuth credentials, cookies and storage state
# deliberately never enter the public identity document.
_SAFE_ENV_KEYS = (
    "ARCHBRO_ENV",
    "ARCHBRO_PERSISTENCE",
    "ARCHBRO_AUTH_MODE",
    "ARCHBRO_EDGE_GUARD",
    "ARCHBRO_PROVIDER",
    "HUMAN_AGENT_PROVIDER",
    "GEMINI_MODEL",
    "GEMINI_GOAL_MODEL",
    "GEMINI_ROUTINE_MODEL",
    "GEMINI_SYSTEM_MAP_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "GEMINI_FALLBACK_MODELS",
    "GEMINI_GOAL_FALLBACK_MODELS",
    "GEMINI_ROUTINE_FALLBACK_MODELS",
    "GEMINI_BOOTSTRAP_FALLBACK_MODELS",
    "GEMINI_GOAL_MODEL_TIMEOUT_SECONDS",
    "GEMINI_ROUTINE_MODEL_TIMEOUT_SECONDS",
    "GEMINI_ARCHITECTURE_MODEL_TIMEOUT_SECONDS",
    "GEMINI_ARCHITECTURE_PHASE_TIMEOUT_SECONDS",
    "GEMINI_ARCHITECTURE_TOTAL_TIMEOUT_SECONDS",
    "GEMINI_ARCHITECTURE_MAX_OUTPUT_TOKENS",
    "GEMINI_ARCHITECTURE_THINKING_LEVEL",
    "GEMINI_SYSTEM_MAP_MAX_OUTPUT_TOKENS",
    "GEMINI_SCOPE_MAX_OUTPUT_TOKENS",
    "GEMINI_RECONCILE_MAX_OUTPUT_TOKENS",
    "GEMINI_SYSTEM_MAP_THINKING_LEVEL",
    "GEMINI_SCOPE_THINKING_LEVEL",
    "GEMINI_RECONCILE_THINKING_LEVEL",
    "GEMINI_ARCHITECTURE_RETRY_ATTEMPTS",
    "GEMINI_RETRY_INITIAL_DELAY_SECONDS",
    "GEMINI_RETRY_MAX_DELAY_SECONDS",
    "GEMINI_RETRY_EXP_BASE",
    "GEMINI_RETRY_JITTER",
    "GEMINI_ARCHITECTURE_MAX_CONCURRENCY",
    "GEMINI_ARCHITECTURE_QUEUE_TIMEOUT_SECONDS",
    "GEMINI_HTTP_TIMEOUT_MS",
    "GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS",
    "ARCHBRO_GOAL_REQUEST_TIMEOUT_SECONDS",
    "HUMAN_AGENT_GOAL_REQUEST_TIMEOUT_SECONDS",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def safe_runtime_config(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    values = {key.lower(): _clean(env.get(key)) for key in _SAFE_ENV_KEYS}
    project_id = _clean(env.get("FIREBASE_PROJECT_ID")) or _clean(
        env.get("GOOGLE_CLOUD_PROJECT")
    )
    values["firebase"] = {
        "project_id": project_id,
        "auth_domain": _clean(env.get("ARCHBRO_FIREBASE_AUTH_DOMAIN")),
        # Firebase's browser key is intentionally represented by presence only.
        # This keeps the release identity useful without normalising the habit
        # of hashing credential-looking material into a public document.
        "api_key_present": bool(_clean(env.get("ARCHBRO_FIREBASE_API_KEY"))),
        "app_id_present": bool(_clean(env.get("ARCHBRO_FIREBASE_APP_ID"))),
    }
    raw_mcp = _clean(env.get("ARCHBRO_MCP_SERVERS_JSON"))
    values["connected_mcp_config_present"] = bool(raw_mcp)
    return values


def safe_config_fingerprint(
    environ: Mapping[str, str] | None = None,
) -> str:
    return canonical_sha256(safe_runtime_config(environ))


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _release_dir(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = _clean(os.getenv("ARCHBRO_RELEASE_DIR"))
    return Path(override) if override else _DEFAULT_RELEASE_DIR


def _manifest_entries(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, dict):
        return None
    if value.get("schema") != SOURCE_MANIFEST_SCHEMA:
        return None
    entries = value.get("files")
    if not isinstance(entries, list) or not entries:
        return None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        path = entry.get("path")
        sha256 = entry.get("sha256")
        if not isinstance(path, str) or not isinstance(sha256, str):
            return None
        path = path.replace("\\", "/")
        if (
            any(part in {"", ".", ".."} for part in path.split("/"))
            or ":" in path
            or "\x00" in path
            or path in seen
            or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
        ):
            return None
        seen.add(path)
        normalized.append({"path": path, "sha256": sha256.lower()})
    return sorted(normalized, key=lambda item: item["path"])


def verify_source_manifest(
    manifest: Any,
    *,
    app_root: str | Path = "/app",
) -> dict[str, Any]:
    entries = _manifest_entries(manifest)
    if entries is None:
        return {
            "status": "unknown",
            "matches": None,
            "expected_manifest_sha256": None,
            "observed_manifest_sha256": None,
            "mismatched_paths": [],
        }

    expected_doc = {"schema": SOURCE_MANIFEST_SCHEMA, "files": entries}
    observed: list[dict[str, str]] = []
    mismatched: list[str] = []
    root = Path(app_root).resolve()

    for entry in entries:
        try:
            candidate = (root / entry["path"]).resolve()
            candidate.relative_to(root)
            if not candidate.is_file():
                mismatched.append(entry["path"])
                continue
            observed_sha = file_sha256(candidate)
        except (OSError, ValueError, RuntimeError):
            mismatched.append(entry["path"])
            continue
        observed.append({"path": entry["path"], "sha256": observed_sha})
        if observed_sha != entry["sha256"]:
            mismatched.append(entry["path"])

    # A matching subset is not evidence for all runtime source. These are the
    # exact source roots copied and inventoried by Dockerfile.candidate.
    inventory_complete = True
    try:
        runtime_paths = {
            path.relative_to(root).as_posix()
            for prefix in ("src", "frontend", "deploy/release")
            for path in (root / prefix).rglob("*")
            if path.is_file()
        }
        mismatched.extend(sorted(runtime_paths - {entry["path"] for entry in entries}))
    except (OSError, ValueError, RuntimeError):
        inventory_complete = False
    matches = inventory_complete and not mismatched and len(observed) == len(entries)
    observed_doc = {"schema": SOURCE_MANIFEST_SCHEMA, "files": observed}
    return {
        "status": "verified" if matches else "mismatch",
        "matches": matches,
        "expected_manifest_sha256": canonical_sha256(expected_doc),
        "observed_manifest_sha256": canonical_sha256(observed_doc),
        "mismatched_paths": sorted(mismatched),
    }


def _dependency_packages(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, dict) or value.get("schema") != DEPENDENCY_MANIFEST_SCHEMA:
        return None
    packages = value.get("packages")
    if not isinstance(packages, list):
        return None
    normalized: list[dict[str, str]] = []
    for item in packages:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
            return None
        normalized.append({"name": name.strip(), "version": version.strip()})
    return sorted(normalized, key=lambda item: (item["name"].lower(), item["version"]))


def installed_dependency_manifest(distributions: Any = None) -> dict[str, Any]:
    source = importlib.metadata.distributions() if distributions is None else distributions
    packages: list[dict[str, str]] = []
    for distribution in source:
        metadata = getattr(distribution, "metadata", {})
        name = metadata.get("Name") if hasattr(metadata, "get") else None
        version = getattr(distribution, "version", None)
        if isinstance(name, str) and name.strip() and isinstance(version, str) and version.strip():
            packages.append({"name": name.strip(), "version": version.strip()})
    packages.sort(key=lambda item: (item["name"].lower(), item["version"]))
    return {"schema": DEPENDENCY_MANIFEST_SCHEMA, "packages": packages}


def verify_dependency_manifest(manifest: Any, *, distributions: Any = None) -> dict[str, Any]:
    expected_packages = _dependency_packages(manifest)
    if expected_packages is None:
        return {
            "status": "unknown",
            "matches": None,
            "expected_manifest_sha256": None,
            "observed_manifest_sha256": None,
        }
    expected = {"schema": DEPENDENCY_MANIFEST_SCHEMA, "packages": expected_packages}
    observed = installed_dependency_manifest(distributions)
    matches = observed == expected
    return {
        "status": "verified" if matches else "mismatch",
        "matches": matches,
        "expected_manifest_sha256": canonical_sha256(expected),
        "observed_manifest_sha256": canonical_sha256(observed),
    }


def build_runtime_identity(
    *,
    release_dir: str | Path | None = None,
    app_root: str | Path = "/app",
    environ: Mapping[str, str] | None = None,
    distributions: Any = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    root = _release_dir(release_dir)
    metadata = _read_json(root / "release.json")
    source_manifest = _read_json(root / "source-manifest.json")
    dependency_manifest = _read_json(root / "dependencies.json")

    source_verification = verify_source_manifest(source_manifest, app_root=app_root)
    metadata = (
        metadata if isinstance(metadata, dict)
        and metadata.get("schema") == RELEASE_METADATA_SCHEMA else {}
    )
    source_meta = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    base_meta = metadata.get("base") if isinstance(metadata.get("base"), dict) else {}
    harness_meta = metadata.get("harness") if isinstance(metadata.get("harness"), dict) else {}
    source_sha = source_meta.get("git_sha")
    source_tree = source_meta.get("git_tree")
    source_verified = source_verification["matches"] is True and all(
        isinstance(value, str)
        and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value)
        for value in (source_sha, source_tree)
    )
    # Build declarations remain diagnostic only when the actual source cannot
    # be verified. Never let unchanged labels mask changed or absent files.
    observed_source_sha = source_sha if source_verified else None
    observed_source_tree = source_tree if source_verified else None

    dependency_verification = verify_dependency_manifest(
        dependency_manifest,
        distributions=distributions,
    )
    dependency_hash = (
        dependency_verification["expected_manifest_sha256"]
        if dependency_verification["matches"] is True
        else None
    )
    config_fingerprint = safe_config_fingerprint(env)
    image_id = _clean(env.get("ARCHBRO_IMAGE_ID"))
    image_digest = _clean(env.get("ARCHBRO_IMAGE_MANIFEST_DIGEST"))
    target_origin = _clean(env.get("ARCHBRO_ACCEPTANCE_TARGET_ORIGIN"))

    # The seven top-level fields intentionally match Role 11's
    # archbro.release_unit.v1 observed-identity contract. They are observations,
    # never expected-value fallbacks: missing external image/target evidence
    # therefore remains null and Role 11 will fail closed before fixture launch.
    role11_observed_identity = {
        "source_sha": observed_source_sha,
        "source_tree": observed_source_tree,
        "image_digest": image_digest,
        "harness_hash": harness_meta.get("sha256"),
        "dependency_fingerprint": dependency_hash,
        "safe_config_fingerprint": config_fingerprint,
        "target_origin": target_origin,
    }

    return {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        **role11_observed_identity,
        "source": {
            "git_sha": observed_source_sha,
            "git_tree": observed_source_tree,
            "declared_git_sha": source_sha,
            "declared_git_tree": source_tree,
            "manifest_sha256": source_verification["expected_manifest_sha256"],
            "observed_manifest_sha256": source_verification["observed_manifest_sha256"],
            "manifest_matches": source_verification["matches"],
            "manifest_status": source_verification["status"],
        },
        "image": {
            # Image identity is observed outside the image. Missing values stay
            # unknown rather than being copied from a label or expected value.
            "id": image_id,
            "manifest_digest": image_digest,
        },
        "base": {
            "manifest_digest": base_meta.get("manifest_digest"),
            "reference": base_meta.get("reference"),
        },
        "runtime": {
            "python_version": platform.python_version(),
            "dependency_manifest_sha256": dependency_hash,
            "dependency_manifest_present": dependency_hash is not None,
            "dependency_manifest_matches": dependency_verification["matches"],
            "expected_dependency_manifest_sha256": dependency_verification["expected_manifest_sha256"],
            "observed_dependency_manifest_sha256": dependency_verification["observed_manifest_sha256"],
            "dependency_manifest_status": dependency_verification["status"],
        },
        "harness": {"sha256": harness_meta.get("sha256")},
        "target": {
            "origin": target_origin,
            "auth_mode": _clean(env.get("ARCHBRO_AUTH_MODE")),
        },
        "database": {
            "dependency": (
                "postgres"
                if (_clean(env.get("ARCHBRO_PERSISTENCE")) or "postgres").lower()
                == "postgres"
                else _clean(env.get("ARCHBRO_PERSISTENCE"))
            ),
        },
        "config": {
            "fingerprint_sha256": config_fingerprint,
            "values": safe_runtime_config(env),
        },
    }


def database_readiness(repository: Any) -> dict[str, Any]:
    connect = getattr(repository, "_connect", None)
    if not callable(connect):
        return {
            "status": "not_ready",
            "connectivity": "unknown",
            "schema": "unknown",
            "missing_relations": [],
            "reason": "repository_probe_unavailable",
        }

    try:
        with connect() as connection:
            rows = connection.execute(
                """
                SELECT required.relation, to_regclass(required.relation)::text AS resolved
                FROM unnest(%s::text[]) AS required(relation)
                """,
                (list(_REQUIRED_DB_RELATIONS),),
            ).fetchall()
            observed = {
                (row.get("relation") if hasattr(row, "get") else row[0]):
                (row.get("resolved") if hasattr(row, "get") else row[1])
                for row in rows
            }
            missing = [relation for relation in _REQUIRED_DB_RELATIONS if not observed.get(relation)]
            connectivity_ok = True
    except Exception as exc:  # readiness must fail closed without leaking DSNs
        return {
            "status": "not_ready",
            "connectivity": "failed",
            "schema": "unknown",
            "missing_relations": [],
            "reason": "database_probe_failed",
            "error_type": type(exc).__name__,
        }

    schema_ok = not missing
    return {
        "status": "ready" if connectivity_ok and schema_ok else "not_ready",
        "connectivity": "ready" if connectivity_ok else "failed",
        "schema": "ready" if schema_ok else "missing",
        "missing_relations": missing,
        "reason": None if connectivity_ok and schema_ok else "database_schema_incomplete",
    }


def build_public_readiness_report(
    *,
    environment: str,
    auth_mode: str,
    public_firebase_config: Mapping[str, str] | None,
    principal_provider: Any,
) -> tuple[dict[str, Any], bool]:
    firebase = firebase_public_config_readiness(
        auth_mode=auth_mode,
        environment=environment,
        public_config=public_firebase_config,
    )
    auth = auth_boundary_readiness(
        auth_mode=auth_mode,
        environment=environment,
        principal_provider=principal_provider,
    )
    ready = firebase["status"] == "ready" and auth["status"] == "ready"
    return (
        {
            "schema": RUNTIME_READINESS_SCHEMA,
            "scope": "public",
            "status": "ready" if ready else "not_ready",
            "checks": {
                "firebase_public_config": firebase,
                "api_identity_boundary": auth,
                "database": {"status": "internal_only"},
            },
            "provider_probe": "not_run",
        },
        ready,
    )


def firebase_public_config_readiness(
    *,
    auth_mode: str,
    environment: str,
    public_config: Mapping[str, str] | None,
) -> dict[str, Any]:
    if auth_mode != "firebase":
        allowed = environment != "production" and auth_mode == "local"
        return {
            "status": "ready" if allowed else "not_ready",
            "required": False,
            "missing": [],
            "reason": None if allowed else "production_requires_firebase",
        }

    config = public_config or {}
    required = ("apiKey", "projectId", "authDomain")
    missing = [key for key in required if not _clean(config.get(key))]
    return {
        "status": "ready" if not missing else "not_ready",
        "required": True,
        "missing": missing,
        "reason": None if not missing else "firebase_public_config_incomplete",
    }


def auth_boundary_readiness(
    *,
    auth_mode: str,
    environment: str,
    principal_provider: Any,
) -> dict[str, Any]:
    if auth_mode == "firebase":
        ready = principal_provider is not None
        return {
            "status": "ready" if ready else "not_ready",
            "mode": auth_mode,
            "reason": None if ready else "firebase_principal_provider_unavailable",
        }
    ready = auth_mode == "local" and environment != "production"
    return {
        "status": "ready" if ready else "not_ready",
        "mode": auth_mode,
        "reason": None if ready else "local_auth_not_allowed",
    }


def build_readiness_report(
    *,
    repository: Any,
    environment: str,
    auth_mode: str,
    public_firebase_config: Mapping[str, str] | None,
    principal_provider: Any,
) -> tuple[dict[str, Any], bool]:
    database = database_readiness(repository)
    firebase = firebase_public_config_readiness(
        auth_mode=auth_mode,
        environment=environment,
        public_config=public_firebase_config,
    )
    auth = auth_boundary_readiness(
        auth_mode=auth_mode,
        environment=environment,
        principal_provider=principal_provider,
    )
    ready = all(
        item["status"] == "ready"
        for item in (database, firebase, auth)
    )
    return (
        {
            "schema": RUNTIME_READINESS_SCHEMA,
            "scope": "internal",
            "status": "ready" if ready else "not_ready",
            "checks": {
                "database": database,
                "firebase_public_config": firebase,
                "api_identity_boundary": auth,
            },
            "provider_probe": "not_run",
        },
        ready,
    )
