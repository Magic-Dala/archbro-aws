from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

PREFLIGHT_SCHEMA = "archbro.release.preflight.v1"
ROLE11_RELEASE_UNIT_SCHEMA = "archbro.release_unit.v1"
RUNNER_PLAN_SCHEMA = "archbro.release.runner_plan.v1"
ROLLBACK_SCHEMA = "archbro.release.rollback.v1"
FIXED_ACCEPTANCE_ORIGIN = "http://127.0.0.1:8013"
ACCEPTANCE_HARNESS_SCHEMA = "archbro.release.acceptance_harness.v1"
EXTERNAL_IMAGE_OBSERVATION_SCHEMA = "archbro.release.image_observation.v1"
ACCEPTANCE_HARNESS_FILES = (
    "qa/verify_archbro_release.py",
    "qa/archbro_release_evidence.py",
    "qa/playwright_release_acceptance.py",
    "qa/archbro_release_runtime.py",
)

ROLE11_IDENTITY_FIELDS = (
    "source_sha",
    "source_tree",
    "image_digest",
    "harness_hash",
    "dependency_fingerprint",
    "safe_config_fingerprint",
    "target_origin",
)

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)
_DIGEST_REF_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$", re.IGNORECASE)
_FORBIDDEN_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "cookie",
    "database_url",
    "storage_state",
    "authorization",
)
_ALLOWED_SENSITIVE_METADATA_KEYS = {"storage_state_mode"}

DEFAULT_RUNTIME_DEPENDENCIES = {
    "acceptance_origin": FIXED_ACCEPTANCE_ORIGIN,
    "candidate_app_port": 8013,
    "current_app_port": 8012,
    "mcp_sidecar_port": 8085,
    "postgres_host": "127.0.0.1",
    "postgres_port": 55432,
    "postgres_container": "archbro-s3-auth-pg",
}


class ContractError(ValueError):
    pass


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def acceptance_harness_manifest(root: str | Path | None = None) -> dict[str, Any]:
    repository_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[1]
    files: list[dict[str, str]] = []
    for relative in ACCEPTANCE_HARNESS_FILES:
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root)
        except ValueError as exc:
            raise ContractError("acceptance harness path escaped repository root") from exc
        if not path.is_file():
            raise ContractError(f"acceptance harness file missing: {relative}")
        files.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"schema": ACCEPTANCE_HARNESS_SCHEMA, "files": files}


def acceptance_harness_sha256(root: str | Path | None = None) -> str:
    return _canonical_sha256(acceptance_harness_manifest(root))


def normalize_external_image_observation(raw: Mapping[str, Any]) -> dict[str, str]:
    if raw.get("schema") != EXTERNAL_IMAGE_OBSERVATION_SCHEMA:
        raise ContractError(f"external image observation schema must be {EXTERNAL_IMAGE_OBSERVATION_SCHEMA}")
    reference = str(raw.get("image_reference") or "").strip()
    digest = str(raw.get("manifest_digest") or "").strip()
    image_id = str(raw.get("image_id") or "").strip()
    running_image_id = str(raw.get("running_image_id") or "").strip()
    if not _DIGEST_REF_RE.fullmatch(reference):
        raise ContractError("external image reference must be pinned with @sha256")
    if not _DIGEST_RE.fullmatch(digest) or not reference.endswith("@" + digest):
        raise ContractError("external image manifest digest must match the pinned reference")
    if not _DIGEST_RE.fullmatch(image_id):
        raise ContractError("external image ID must be sha256:<64-hex>")
    if running_image_id != image_id:
        raise ContractError("running image ID does not match the externally inspected image ID")
    return {
        "schema": EXTERNAL_IMAGE_OBSERVATION_SCHEMA,
        "image_reference": reference,
        "manifest_digest": digest.lower(),
        "image_id": image_id.lower(),
        "running_image_id": running_image_id.lower(),
    }


def observe_docker_image_identity(
    *,
    image_reference: str,
    container: str,
    runner=subprocess.run,
) -> dict[str, str]:
    if not _DIGEST_REF_RE.fullmatch(image_reference):
        raise ContractError("image observation requires a digest-pinned reference")
    digest = "sha256:" + image_reference.rsplit("@sha256:", 1)[1].lower()

    def run_inspect(args: list[str]) -> str:
        completed = runner(args, check=False, capture_output=True, text=True)
        if int(completed.returncode) != 0:
            raise ContractError("Docker image observation failed")
        return str(completed.stdout).strip()

    image_id = run_inspect(["docker", "image", "inspect", "--format", "{{.Id}}", image_reference])
    running_image_id = run_inspect(["docker", "inspect", "--format", "{{.Image}}", container])
    return normalize_external_image_observation({
        "schema": EXTERNAL_IMAGE_OBSERVATION_SCHEMA,
        "image_reference": image_reference,
        "manifest_digest": digest,
        "image_id": image_id,
        "running_image_id": running_image_id,
    })


def validate_target_origin(origin: str) -> str:
    """Validate an explicit origin without substituting a planned observation."""
    if not isinstance(origin, str):
        raise ContractError("target origin must be a string")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise ContractError("target origin must be a valid HTTP(S) origin") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in origin)
        or "\\" in origin
        or (port is not None and port < 1)
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ContractError("target origin must be HTTPS or loopback HTTP, with no credentials, path, query or fragment")
    return origin


def _contains_forbidden_key(value: Any, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            child_path = f"{path}.{key}" if path else str(key)
            if (
                key_text not in _ALLOWED_SENSITIVE_METADATA_KEYS
                and any(part in key_text for part in _FORBIDDEN_KEY_PARTS)
            ):
                return child_path
            found = _contains_forbidden_key(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _contains_forbidden_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(mapping: Mapping[str, Any], key: str) -> Any | None:
    return mapping.get(key) if key in mapping else None


def _unknown_string(value: Any) -> bool:
    return (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().lower() in {"unknown", "none", "null"}
    )


def expected_identity_from_release_unit(
    release_unit: Mapping[str, Any],
) -> dict[str, str]:
    if release_unit.get("schema") != ROLE11_RELEASE_UNIT_SCHEMA:
        raise ContractError(
            f"release unit schema must be {ROLE11_RELEASE_UNIT_SCHEMA}"
        )
    expected = release_unit.get("expected_identity")
    if not isinstance(expected, Mapping):
        raise ContractError("RELEASE_UNIT.expected_identity is required")

    normalized: dict[str, str] = {}
    for field in ROLE11_IDENTITY_FIELDS:
        value = expected.get(field)
        if _unknown_string(value):
            raise ContractError(
                f"RELEASE_UNIT.expected_identity.{field} is missing or unknown"
            )
        normalized[field] = str(value).strip()

    validate_target_origin(normalized["target_origin"])
    if not _SHA_RE.fullmatch(normalized["source_sha"]):
        raise ContractError("RELEASE_UNIT.expected_identity.source_sha must be exact hex")
    if not _SHA_RE.fullmatch(normalized["source_tree"]):
        raise ContractError("RELEASE_UNIT.expected_identity.source_tree must be exact hex")
    if not _DIGEST_RE.fullmatch(normalized["image_digest"]):
        raise ContractError(
            "RELEASE_UNIT.expected_identity.image_digest must be sha256:<64-hex>"
        )
    for field in (
        "harness_hash",
        "dependency_fingerprint",
        "safe_config_fingerprint",
    ):
        value = normalized[field].removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", value, re.IGNORECASE):
            raise ContractError(
                f"RELEASE_UNIT.expected_identity.{field} must contain 64 hex digits"
            )
    return normalized


def _source_manifest_verified(identity: Mapping[str, Any]) -> bool:
    source = _mapping(identity.get("source"))
    manifest_hash = source.get("manifest_sha256")
    return bool(
        source.get("manifest_matches") is True
        and source.get("manifest_status") == "verified"
        and isinstance(manifest_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", manifest_hash, re.IGNORECASE)
        and source.get("observed_manifest_sha256") == manifest_hash
        and all(
            isinstance(identity.get(flat), str)
            and _SHA_RE.fullmatch(identity[flat])
            and source.get(nested) == identity[flat]
            for flat, nested in (("source_sha", "git_sha"), ("source_tree", "git_tree"))
        )
    )


def _dependency_manifest_verified(identity: Mapping[str, Any]) -> bool:
    runtime = _mapping(identity.get("runtime"))
    expected_hash = runtime.get("expected_dependency_manifest_sha256")
    return bool(
        runtime.get("dependency_manifest_matches") is True
        and runtime.get("dependency_manifest_status") == "verified"
        and isinstance(expected_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_hash, re.IGNORECASE)
        and runtime.get("observed_dependency_manifest_sha256") == expected_hash
        and identity.get("dependency_fingerprint") == expected_hash
    )


def observed_identity_from_runtime(
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    # The flat contract is valid only with consistent source observations.
    # Expected values and unchanged build labels cannot repair missing proof.
    observed = {
        field: runtime_identity.get(field)
        for field in ROLE11_IDENTITY_FIELDS
    }
    if not _source_manifest_verified(runtime_identity):
        observed["source_sha"] = None
        observed["source_tree"] = None
    if isinstance(runtime_identity.get("runtime"), Mapping) and not _dependency_manifest_verified(runtime_identity):
        observed["dependency_fingerprint"] = None
    return observed


def authoritative_observed_identity(
    runtime_identity: Mapping[str, Any],
    *,
    external_image_observation: Mapping[str, Any],
    harness_root: str | Path | None = None,
) -> dict[str, Any]:
    observed = observed_identity_from_runtime(runtime_identity)
    image = normalize_external_image_observation(external_image_observation)
    harness_hash = acceptance_harness_sha256(harness_root)

    runtime_image = _mapping(runtime_identity.get("image")).get("manifest_digest")
    if runtime_image not in {None, image["manifest_digest"]}:
        raise ContractError("runtime image declaration differs from external image observation")
    runtime_harness = _mapping(runtime_identity.get("harness")).get("sha256")
    if runtime_harness not in {None, harness_hash}:
        raise ContractError("runtime harness declaration differs from actual acceptance harness")

    observed["image_digest"] = image["manifest_digest"]
    observed["harness_hash"] = harness_hash
    return observed


def compare_release_unit_identity(
    release_unit: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    expected = expected_identity_from_release_unit(release_unit)
    observed = observed_identity_from_runtime(runtime_identity)
    unknown_observed = [
        field for field, value in observed.items() if _unknown_string(value)
    ]
    mismatches = {
        field: {
            "expected": expected[field],
            "observed": observed[field],
        }
        for field in ROLE11_IDENTITY_FIELDS
        if field not in unknown_observed and observed[field] != expected[field]
    }
    return {
        "schema": "archbro.release.identity_preflight.v1",
        "status": "PASS" if not unknown_observed and not mismatches else "FAIL",
        "expected": expected,
        "observed": observed,
        "unknown_observed": unknown_observed,
        "mismatches": mismatches,
        "fixture_admission": (
            "ALLOWED"
            if not unknown_observed and not mismatches
            else "BLOCKED"
        ),
    }


def normalize_preflight(raw: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = _contains_forbidden_key(raw)
    if forbidden:
        raise ContractError(
            f"preflight contains forbidden secret-bearing field: {forbidden}"
        )
    schema = raw.get("schema")
    if schema not in {None, PREFLIGHT_SCHEMA, ROLE11_RELEASE_UNIT_SCHEMA}:
        raise ContractError(f"unsupported preflight schema: {schema!r}")

    expected_identity: dict[str, str] | None = None
    contract_source: str | None = None
    source_raw = raw
    if schema == ROLE11_RELEASE_UNIT_SCHEMA:
        expected_identity = expected_identity_from_release_unit(raw)
        contract_source = ROLE11_RELEASE_UNIT_SCHEMA
        # A release unit carries expectations, not observations. Leave every
        # observed role-12 field unknown until measured evidence is supplied.
        source_raw = {}

    source = _mapping(source_raw.get("source"))
    image = _mapping(source_raw.get("image"))
    base = _mapping(source_raw.get("base"))
    runtime = _mapping(source_raw.get("runtime"))
    harness = _mapping(source_raw.get("harness"))
    target = _mapping(source_raw.get("target"))
    auth = _mapping(source_raw.get("auth"))
    database = _mapping(source_raw.get("database"))

    if expected_identity is None:
        explicit_expected = source_raw.get("expected_identity")
        if isinstance(explicit_expected, Mapping):
            expected_identity = {
                field: explicit_expected.get(field)
                for field in ROLE11_IDENTITY_FIELDS
            }

    # Every observed contract field is explicit. Missing observations remain
    # None; the runner never copies expected values into observed slots.
    return {
        "schema": PREFLIGHT_SCHEMA,
        "contract_source": contract_source,
        "expected_identity": expected_identity,
        "source": {
            "git_sha": _field(source, "git_sha"),
            "git_tree": _field(source, "git_tree"),
            "clean": _field(source, "clean"),
            "source_manifest_sha256": _field(source, "source_manifest_sha256"),
        },
        "image": {
            "id": _field(image, "id"),
            "manifest_digest": _field(image, "manifest_digest"),
        },
        "base": {
            "reference": _field(base, "reference"),
            "manifest_digest": _field(base, "manifest_digest"),
        },
        "runtime": {
            "python_version": _field(runtime, "python_version"),
            "dependency_manifest_sha256": _field(
                runtime, "dependency_manifest_sha256"
            ),
        },
        "harness": {"sha256": _field(harness, "sha256")},
        "target": {
            "origin": _field(target, "origin"),
            "container": _field(target, "container"),
        },
        "auth": {
            "mode": _field(auth, "mode"),
            "browser_session_capability": _field(
                auth, "browser_session_capability"
            ),
            "storage_state_mode": _field(auth, "storage_state_mode"),
        },
        "database": {
            "required": _field(database, "required"),
            "host": _field(database, "host"),
            "port": _field(database, "port"),
            "schema_verified": _field(database, "schema_verified"),
        },
    }


def validate_preflight(preflight: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_preflight(preflight)
    errors: list[str] = []
    source = normalized["source"]
    base = normalized["base"]
    target = normalized["target"]
    auth = normalized["auth"]
    database = normalized["database"]
    expected_identity = normalized.get("expected_identity")

    if source["git_sha"] is not None and not _SHA_RE.fullmatch(
        str(source["git_sha"])
    ):
        errors.append("source.git_sha must be an exact hexadecimal Git object id")
    if source["git_tree"] is not None and not _SHA_RE.fullmatch(
        str(source["git_tree"])
    ):
        errors.append("source.git_tree must be an exact hexadecimal Git object id")
    if base["reference"] is not None and not _DIGEST_REF_RE.fullmatch(
        str(base["reference"])
    ):
        errors.append("base.reference must be an image reference pinned with @sha256")
    if base["manifest_digest"] is not None and not _DIGEST_RE.fullmatch(
        str(base["manifest_digest"])
    ):
        errors.append("base.manifest_digest must be sha256:<64-hex>")
    if target["origin"] is not None:
        try:
            validate_target_origin(target["origin"])
        except (ContractError, TypeError) as exc:
            errors.append(str(exc))
        if isinstance(expected_identity, Mapping) and target["origin"] != expected_identity.get("target_origin"):
            errors.append("target.origin differs from the frozen expected_identity.target_origin")
    if auth["mode"] is not None and auth["mode"] not in {
        "firebase",
        "local",
        "storage_state",
        "workbench_attach",
    }:
        errors.append(
            "auth.mode must be firebase, local, storage_state, or workbench_attach"
        )
    if database["port"] is not None and int(database["port"]) != 55432:
        errors.append("role-12 acceptance DB port must remain 55432")
    if isinstance(expected_identity, Mapping):
        try:
            expected_identity_from_release_unit(
                {
                    "schema": ROLE11_RELEASE_UNIT_SCHEMA,
                    "expected_identity": expected_identity,
                }
            )
        except ContractError as exc:
            errors.append(str(exc))

    unknown = [
        path
        for path, value in (
            ("source.git_sha", source["git_sha"]),
            ("source.git_tree", source["git_tree"]),
            ("base.reference", base["reference"]),
            ("base.manifest_digest", base["manifest_digest"]),
            ("target.origin", target["origin"]),
            ("auth.mode", auth["mode"]),
            ("auth.browser_session_capability", auth["browser_session_capability"]),
            ("database.schema_verified", database["schema_verified"]),
        )
        if value is None
    ]
    return {
        "schema": "archbro.release.preflight_result.v1",
        "valid": not errors,
        "errors": errors,
        "unknown": unknown,
        "preflight": normalized,
    }


def build_command(
    *,
    base_image: str,
    source_sha: str,
    source_tree: str,
    harness_sha256: str | None,
    image_tag: str,
    dockerfile: str = "deploy/release/Dockerfile.candidate",
    harness_root: str | Path | None = None,
) -> list[str]:
    if not _DIGEST_REF_RE.fullmatch(base_image):
        raise ContractError(
            "BASE_IMAGE must be a real image reference pinned with @sha256; "
            "floating tags are refused"
        )
    if not _SHA_RE.fullmatch(source_sha):
        raise ContractError("source SHA must be exact hex")
    if not _SHA_RE.fullmatch(source_tree):
        raise ContractError("source tree must be exact hex")
    actual_harness = acceptance_harness_sha256(harness_root)
    if harness_sha256 is not None and not _DIGEST_RE.fullmatch(
        f"sha256:{harness_sha256}"
        if not str(harness_sha256).startswith("sha256:")
        else str(harness_sha256)
    ):
        raise ContractError("harness SHA-256 must contain exactly 64 hex digits")
    if harness_sha256 is not None and str(harness_sha256).removeprefix("sha256:").lower() != actual_harness:
        raise ContractError("declared harness SHA-256 differs from actual acceptance harness")
    harness_value = actual_harness
    return [
        "docker",
        "build",
        "--file",
        dockerfile,
        "--build-arg",
        f"BASE_IMAGE={base_image}",
        "--build-arg",
        f"ARCHBRO_SOURCE_SHA={source_sha}",
        "--build-arg",
        f"ARCHBRO_SOURCE_TREE={source_tree}",
        "--build-arg",
        f"ARCHBRO_HARNESS_SHA256={harness_value}",
        "--tag",
        image_tag,
        ".",
    ]


def runner_plan(preflight: Mapping[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_preflight(preflight or {})
    origin = (
        _mapping(normalized.get("expected_identity")).get("target_origin")
        or normalized["target"]["origin"]
        or FIXED_ACCEPTANCE_ORIGIN
    )
    validate_target_origin(origin)
    return {
        "schema": RUNNER_PLAN_SCHEMA,
        "mode": "dry-run",
        "fixed_target": {
            "origin": origin,
            "public_route_switch": False,
            "notes": (
                "The release unit fixes the acceptance origin. Planning and "
                "inspection do not change a service or public route."
            ),
        },
        "dependencies": {
            **DEFAULT_RUNTIME_DEPENDENCIES,
            "acceptance_origin": origin,
            "candidate_app_port": (
                urlsplit(origin).port
                if urlsplit(origin).hostname in {"127.0.0.1", "localhost", "::1"}
                else None
            ),
        },
        "role11_contract": {
            "release_unit_schema": ROLE11_RELEASE_UNIT_SCHEMA,
            "identity_fields": list(ROLE11_IDENTITY_FIELDS),
            "expected_and_observed_are_separate": True,
            "fixture_admission": "blocked until exact identity PASS",
        },
        "phases": [
            "preflight",
            "build_exact_source",
            "inspect_built_identity",
            "serve_fixed_target_without_public_route_change",
            "inspect_runtime_identity_and_readiness",
            "freeze_for_14V",
        ],
        "service_switch_default": "refused",
        "rollback": {
            "capture_before_switch": [
                "owned_route_identity",
                "image_id",
                "image_manifest_digest",
                "safe_config_fingerprint",
            ],
            "execution_owner": "15D",
        },
        "preflight": normalized,
    }


def inspect_target(
    origin: str,
    *,
    release_unit: Mapping[str, Any] | None = None,
    expected_source_sha: str | None = None,
    expected_source_tree: str | None = None,
    expected_image_id: str | None = None,
    expected_manifest_digest: str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    validate_target_origin(origin)
    if release_unit is not None and origin != expected_identity_from_release_unit(release_unit)["target_origin"]:
        raise ContractError("inspection origin differs from the frozen release unit target_origin")

    def get_json(path: str) -> tuple[int, Any]:
        request = urllib.request.Request(
            origin + path,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                actual = urlsplit(response.geturl())
                if f"{actual.scheme}://{actual.netloc}" != origin:
                    raise ContractError("inspection redirected to a different origin")
                return int(response.status), json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
            except Exception:
                body = None
            return int(error.code), body

    identity_status, identity = get_json("/runtime-identity")
    readiness_status, readiness = get_json("/readyz")
    mismatches: list[str] = []
    role11_identity: dict[str, Any] | None = None

    if identity_status != 200 or not isinstance(identity, Mapping):
        mismatches.append("runtime_identity_unavailable")
    else:
        source = _mapping(identity.get("source"))
        image = _mapping(identity.get("image"))
        if identity.get("target_origin") != origin:
            mismatches.append("target_origin")
        if expected_source_sha is not None and source.get("git_sha") != expected_source_sha:
            mismatches.append("source_git_sha")
        if expected_source_tree is not None and source.get("git_tree") != expected_source_tree:
            mismatches.append("source_git_tree")
        if not _source_manifest_verified(identity):
            mismatches.append("source_manifest")
        if expected_image_id is not None and image.get("id") != expected_image_id:
            mismatches.append("image_id")
        if (
            expected_manifest_digest is not None
            and image.get("manifest_digest") != expected_manifest_digest
        ):
            mismatches.append("image_manifest_digest")
        if release_unit is not None:
            role11_identity = compare_release_unit_identity(release_unit, identity)
            if role11_identity["status"] != "PASS":
                mismatches.extend(
                    f"release_identity:{field}"
                    for field in role11_identity["unknown_observed"]
                )
                mismatches.extend(
                    f"release_identity:{field}"
                    for field in role11_identity["mismatches"]
                )

    if readiness_status != 200 or not isinstance(readiness, Mapping):
        mismatches.append("readiness")
    elif readiness.get("status") != "ready":
        mismatches.append("readiness")

    return {
        "schema": "archbro.release.inspect.v1",
        "origin": origin,
        "identity_http_status": identity_status,
        "readiness_http_status": readiness_status,
        "identity": identity,
        "release_unit_identity_preflight": role11_identity,
        "readiness": readiness,
        "matches": not mismatches,
        "mismatches": sorted(set(mismatches)),
    }


def authorize_service_switch(
    *,
    phase: str,
    gates: Mapping[str, Any] | None,
) -> bool:
    if phase != "15D" or not isinstance(gates, Mapping):
        return False
    required = (
        "explicit_user_launch",
        "acceptance_14v_pass",
        "exact_cas_ready",
    )
    # A direct repository controller may provide exact-CAS admission too.
    # Preserve old ledgers, but an explicit new false must never fall back to
    # an older Threaden true value.
    admitted = gates.get("promotion_admitted", gates.get("threaden_promotion_admitted"))
    return admitted is True and all(gates.get(key) is True for key in required)


def rollback_contract(
    snapshot: Mapping[str, Any],
    *,
    phase: str = "12",
    gates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    forbidden = _contains_forbidden_key(snapshot)
    if forbidden:
        raise ContractError(
            f"rollback snapshot contains forbidden secret-bearing field: {forbidden}"
        )
    required = (
        "owned_route_identity",
        "image_id",
        "image_manifest_digest",
        "safe_config_fingerprint",
    )
    missing = [key for key in required if snapshot.get(key) in {None, ""}]
    authorized = not missing and authorize_service_switch(phase=phase, gates=gates)
    return {
        "schema": ROLLBACK_SCHEMA,
        "snapshot": {key: snapshot.get(key) for key in required},
        "missing": missing,
        "phase": phase,
        "authorized_to_execute": authorized,
        "git_history_rewrite": False,
        "action": (
            "restore exact owned route/image/config identity"
            if authorized
            else "dry-run only; service switch refused"
        ),
    }


def _read_json_file(path: str | Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ContractError("JSON document must be an object")
    return value


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ArchBro exact-source release runtime runner"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--preflight-json")

    preflight = sub.add_parser("preflight")
    preflight.add_argument("preflight_json")

    build = sub.add_parser("build")
    build.add_argument("--base-image", required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--source-tree", required=True)
    build.add_argument("--harness-sha256")
    build.add_argument("--image-tag", required=True)
    build.add_argument("--execute-build", action="store_true")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--origin", default=FIXED_ACCEPTANCE_ORIGIN)
    inspect.add_argument("--release-unit-json")
    inspect.add_argument("--expected-source-sha")
    inspect.add_argument("--expected-source-tree")
    inspect.add_argument("--expected-image-id")
    inspect.add_argument("--expected-manifest-digest")

    observe_image = sub.add_parser("observe-image")
    observe_image.add_argument("--image-reference", required=True)
    observe_image.add_argument("--container", required=True)

    rollback = sub.add_parser("rollback")
    rollback.add_argument("snapshot_json")
    rollback.add_argument("--phase", default="12")
    rollback.add_argument("--gates-json")
    rollback.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            preflight = _read_json_file(args.preflight_json) if args.preflight_json else {}
            _emit(runner_plan(preflight))
            return 0

        if args.command == "preflight":
            result = validate_preflight(_read_json_file(args.preflight_json))
            _emit(result)
            return 0 if result["valid"] else 2

        if args.command == "build":
            command = build_command(
                base_image=args.base_image,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
                harness_sha256=args.harness_sha256,
                image_tag=args.image_tag,
            )
            if not args.execute_build:
                _emit(
                    {
                        "schema": "archbro.release.build_plan.v1",
                        "mode": "dry-run",
                        "command": command,
                    }
                )
                return 0
            completed = subprocess.run(command, check=False)
            return int(completed.returncode)

        if args.command == "inspect":
            release_unit = (
                _read_json_file(args.release_unit_json)
                if args.release_unit_json
                else None
            )
            result = inspect_target(
                args.origin,
                release_unit=release_unit,
                expected_source_sha=args.expected_source_sha,
                expected_source_tree=args.expected_source_tree,
                expected_image_id=args.expected_image_id,
                expected_manifest_digest=args.expected_manifest_digest,
            )
            _emit(result)
            return 0 if result["matches"] else 2

        if args.command == "observe-image":
            _emit(observe_docker_image_identity(
                image_reference=args.image_reference,
                container=args.container,
            ))
            return 0

        if args.command == "rollback":
            snapshot = _read_json_file(args.snapshot_json)
            gates = _read_json_file(args.gates_json) if args.gates_json else None
            contract = rollback_contract(snapshot, phase=args.phase, gates=gates)
            if args.execute and not contract["authorized_to_execute"]:
                _emit(contract)
                return 3
            if args.execute:
                # The route/image/config mutation belongs to 15D's exact owned
                # deployment executor. This runner supplies the gate and immutable
                # rollback identity but never invents a generic shell mutation.
                contract["execution"] = "authorized_for_15D_external_executor"
            _emit(contract)
            return 0

    except (ContractError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"archbro-release-runtime: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
