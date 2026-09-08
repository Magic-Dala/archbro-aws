from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

MANDATORY_CHECKS: tuple[str, ...] = (
    "real_firebase_authenticated_api_pass",
    "real_backend_fixture_bootstrap_pass",
    "full_expanded_hierarchy_and_valid_deeplink_pass",
    "pan_zoom_fit_actual_semantic_disclosure_pass",
    "context_tray_exact_server_manifest_pass",
    "policy_and_context_telemetry_pass",
    "stale_preview_fails_closed_before_provider_pass",
    "ask_agent_exact_context_and_provider_telemetry_pass",
    "explore_exact_context_pass",
    "node_edge_inspector_and_trace_statuses_pass",
    "focus_isolate_clear_pass",
    "collapse_expand_local_reversible_pass",
    "invalid_deeplink_fail_soft_pass",
    "observation_does_not_mutate_architecture_pass",
    "ordinary_interaction_architecture_invariant_pass",
    "structural_recommendation_pending_pass",
    "explicit_human_review_required_for_architecture_mutation_pass",
    "browser_console_page_network_failures_recorded_pass",
)

ASSERTION_MAP: dict[str, str] = {
    "real_firebase_authenticated_api_pass": "Authenticated target API is usable with the declared real-auth mode; no synthesized production principal.",
    "real_backend_fixture_bootstrap_pass": "Fixture bootstrap succeeded only after release identity preflight and records run ownership.",
    "full_expanded_hierarchy_and_valid_deeplink_pass": "Expanded Canvas hierarchy and valid deep link resolve on the same release unit.",
    "pan_zoom_fit_actual_semantic_disclosure_pass": "Pan, wheel/zoom, Fit, 100%, and semantic disclosure execute in a real browser.",
    "context_tray_exact_server_manifest_pass": "Context Tray reflects the exact server-owned manifest and manifest hash.",
    "policy_and_context_telemetry_pass": "Context policy selection and server telemetry are observed without fabricated usage.",
    "stale_preview_fails_closed_before_provider_pass": "Exactly one linked stale-preview 409 fails closed before provider dispatch.",
    "ask_agent_exact_context_and_provider_telemetry_pass": "Ask Agent returns HTTP success, AgentRun SUCCESS, exact context/hash, and observed provider usage.",
    "explore_exact_context_pass": "Explore uses the exact selected context request and preview hash.",
    "node_edge_inspector_and_trace_statuses_pass": "Node/edge Inspector and canonical Trace Path statuses are source-backed and usable.",
    "focus_isolate_clear_pass": "Focus, Isolate, and Clear are local reading interactions and reversible.",
    "collapse_expand_local_reversible_pass": "Collapse/Expand is local, reversible, and does not mutate accepted Architecture.",
    "invalid_deeplink_fail_soft_pass": "Invalid deep link fails softly while leaving a usable product result.",
    "observation_does_not_mutate_architecture_pass": "Observation flow does not directly mutate accepted Architecture.",
    "ordinary_interaction_architecture_invariant_pass": "Ordinary reading/task interaction preserves accepted Architecture.",
    "structural_recommendation_pending_pass": "Structural recommendation remains PENDING until Human Review.",
    "explicit_human_review_required_for_architecture_mutation_pass": "Accepted Architecture mutation occurs only through explicit Human Review.",
    "browser_console_page_network_failures_recorded_pass": "Console, page, HTTP-status, and request-failed evidence are independently classified with narrow exemptions.",
}

IDENTITY_FIELDS: tuple[str, ...] = (
    "source_sha",
    "source_tree",
    "image_digest",
    "harness_hash",
    "dependency_fingerprint",
    "safe_config_fingerprint",
    "target_origin",
)
TERMINAL_CHECK_STATUSES = {"PASS", "FAIL", "UNAVAILABLE"}
ALL_CHECK_STATUSES = TERMINAL_CHECK_STATUSES | {"NOT_REACHED"}

ERROR_CLASSIFICATION_CASE_MATRIX: tuple[dict[str, str], ...] = (
    {"case": "exact_stale_preview_409", "result": "EXPECTED_STALE_PREVIEW", "rule": "Exact owned project + POST events path + business code + action/request link + count=1."},
    {"case": "other_409", "result": "UNEXPECTED_HTTP_STATUS", "rule": "No global 409 exemption."},
    {"case": "exact_external_resource_with_impact_proof", "result": "EXPECTED_EXTERNAL_NONPRODUCT", "rule": "Exact URL/kind plus explicit all-true no-product-impact evidence."},
    {"case": "external_resource_without_impact_proof", "result": "UNEXPECTED_EXTERNAL_FAILURE", "rule": "Host/path/text similarity is insufficient."},
    {"case": "linked_navigation_err_aborted", "result": "EXPECTED_NAVIGATION_ABORT", "rule": "Exact navigation/cancellation linkage and all-true final usable-result proof."},
    {"case": "unlinked_navigation_err_aborted", "result": "UNEXPECTED_REQUEST_FAILED", "rule": "ERR_ABORTED is not self-exempting."},
    {"case": "http_5xx", "result": "UNEXPECTED_HTTP_STATUS", "rule": "HTTP status is recorded separately from transport failure."},
    {"case": "request_failed", "result": "UNEXPECTED_REQUEST_FAILED", "rule": "Transport failures are not converted into HTTP status evidence."},
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^,;\s]+)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"(?i)((?:cookie|token|secret|password)\s*[:=]\s*)([^,;\s]+)"),
)


class EvidenceError(RuntimeError):
    pass


class IdentityPreflightError(EvidenceError):
    pass


class AgentRunEvidenceError(EvidenceError):
    pass


@dataclass(frozen=True)
class ReleaseIdentity:
    source_sha: str
    source_tree: str
    image_digest: str
    harness_hash: str
    dependency_fingerprint: str
    safe_config_fingerprint: str
    target_origin: str


@dataclass(frozen=True)
class Stale409Expectation:
    project_id: str
    action_id: str
    request_id: str
    method: str = "POST"
    path: str | None = None
    error_code: str = "agent_context_preview_stale"
    expected_count: int = 1

    def resolved_path(self) -> str:
        return self.path or f"/projects/{self.project_id}/events"


@dataclass(frozen=True)
class ExternalResourceExemption:
    url: str
    event_kind: str
    no_product_impact_evidence: Mapping[str, bool]


@dataclass(frozen=True)
class NavigationAbortExemption:
    navigation_id: str
    cancellation_action_id: str
    final_url: str
    usable_result_evidence: Mapping[str, bool]


@dataclass(frozen=True)
class OwnedFixture:
    project_id: str
    owner_run_id: str
    created_this_run: bool
    pre_existing: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_failure(value: object, *, limit: int = 1200) -> str:
    if isinstance(value, Mapping):
        detail = value.get("detail")
        if isinstance(detail, Mapping) and detail.get("error"):
            value = detail.get("error")
        elif value.get("error"):
            value = value.get("error")
        elif detail:
            value = detail
        elif value.get("message"):
            value = value.get("message")
    text = str(value if value is not None else "unknown failure")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + "***", text)
    return text[:limit]


def _required_string(mapping: Mapping[str, Any], key: str, *, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in {"unknown", "none", "null"}:
        raise IdentityPreflightError(f"{where}.{key} is missing or unknown")
    return value.strip()


def identity_from_mapping(mapping: Mapping[str, Any], *, where: str) -> ReleaseIdentity:
    return ReleaseIdentity(**{field: _required_string(mapping, field, where=where) for field in IDENTITY_FIELDS})


def expected_identity_from_release_unit(release_unit: Mapping[str, Any]) -> ReleaseIdentity:
    if release_unit.get("schema") not in {None, "archbro.release_unit.v1"}:
        raise IdentityPreflightError(f"unsupported RELEASE_UNIT schema: {release_unit.get('schema')}")
    expected = release_unit.get("expected_identity")
    if not isinstance(expected, Mapping):
        raise IdentityPreflightError("RELEASE_UNIT.expected_identity is required")
    return identity_from_mapping(expected, where="RELEASE_UNIT.expected_identity")


def preflight_identity(expected: ReleaseIdentity, observed: ReleaseIdentity) -> dict[str, Any]:
    mismatches = {
        field: {"expected": getattr(expected, field), "observed": getattr(observed, field)}
        for field in IDENTITY_FIELDS
        if getattr(expected, field) != getattr(observed, field)
    }
    if mismatches:
        fields = ", ".join(sorted(mismatches))
        raise IdentityPreflightError(f"release identity mismatch: {fields}")
    return {"status": "PASS", "expected": asdict(expected), "observed": asdict(observed)}


def preflight_and_create_fixture(
    expected: ReleaseIdentity,
    observed: ReleaseIdentity,
    create_fixture: Callable[[], Any],
) -> Any:
    preflight_identity(expected, observed)
    return create_fixture()


def validate_agent_run_response(
    http_status: int,
    payload: Mapping[str, Any] | None,
    *,
    expected_manifest_hash: str | None = None,
    expected_architecture_version: int | None = None,
    require_provider_usage: bool = True,
) -> dict[str, Any]:
    if not 200 <= int(http_status) < 300:
        raise AgentRunEvidenceError(f"HTTP {http_status}: {sanitize_failure(payload)}")
    if not isinstance(payload, Mapping):
        raise AgentRunEvidenceError("HTTP success returned a non-object AgentRun payload")
    if payload.get("result") != "SUCCESS":
        cause = payload.get("error") or payload.get("summary") or payload.get("result")
        raise AgentRunEvidenceError(f"AgentRun {payload.get('result') or 'UNKNOWN'}: {sanitize_failure(cause)}")
    telemetry = payload.get("context_telemetry")
    if expected_manifest_hash is not None:
        if not isinstance(telemetry, Mapping) or telemetry.get("manifest_hash") != expected_manifest_hash:
            raise AgentRunEvidenceError("AgentRun context manifest hash does not match the executed preview")
    if expected_architecture_version is not None:
        if not isinstance(telemetry, Mapping) or telemetry.get("architecture_version") != expected_architecture_version:
            raise AgentRunEvidenceError("AgentRun context architecture version does not match the executed preview")
    usage = payload.get("provider_usage")
    if require_provider_usage and (not isinstance(usage, Mapping) or not usage):
        raise AgentRunEvidenceError("AgentRun provider_usage is unavailable")
    return {
        "status": "PASS",
        "http_status": int(http_status),
        "agent_result": "SUCCESS",
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "context_telemetry": dict(telemetry) if isinstance(telemetry, Mapping) else None,
        "provider_usage": dict(usage) if isinstance(usage, Mapping) else None,
    }


def _event_path(event: Mapping[str, Any]) -> str:
    path = event.get("path")
    if isinstance(path, str) and path:
        return path
    url = event.get("url")
    return urlsplit(url).path if isinstance(url, str) else ""


def _event_error_code(event: Mapping[str, Any]) -> str | None:
    if isinstance(event.get("error_code"), str):
        return str(event["error_code"])
    detail = event.get("detail")
    if isinstance(detail, Mapping) and isinstance(detail.get("error"), str):
        return str(detail["error"])
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        nested = payload.get("detail")
        if isinstance(nested, Mapping) and isinstance(nested.get("error"), str):
            return str(nested["error"])
    return None


def _all_true(mapping: Mapping[str, bool]) -> bool:
    return bool(mapping) and all(value is True for value in mapping.values())


def _matches_stale(event: Mapping[str, Any], expected: Stale409Expectation) -> bool:
    return (
        event.get("kind") == "http_response"
        and int(event.get("status", 0) or 0) == 409
        and str(event.get("method", "")).upper() == expected.method.upper()
        and _event_path(event) == expected.resolved_path()
        and event.get("project_id") == expected.project_id
        and _event_error_code(event) == expected.error_code
        and event.get("action_id") == expected.action_id
        and event.get("request_id") == expected.request_id
    )


def classify_browser_events(
    events: Sequence[Mapping[str, Any]],
    *,
    stale_expectation: Stale409Expectation | None = None,
    external_exemptions: Sequence[ExternalResourceExemption] = (),
    navigation_exemptions: Sequence[NavigationAbortExemption] = (),
) -> dict[str, Any]:
    stale_matches = [event for event in events if stale_expectation and _matches_stale(event, stale_expectation)]
    stale_count_ok = stale_expectation is None or len(stale_matches) == stale_expectation.expected_count
    classified: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    issues: list[str] = []
    if stale_expectation is not None and not stale_count_ok:
        issues.append(
            f"expected {stale_expectation.expected_count} exact stale-preview 409, observed {len(stale_matches)}"
        )

    for event in events:
        kind = str(event.get("kind", ""))
        status = int(event.get("status", 0) or 0)
        url = str(event.get("url", ""))
        error_text = str(event.get("error_text") or event.get("message") or "")

        if stale_expectation is not None and stale_count_ok and _matches_stale(event, stale_expectation):
            classified.append({"classification": "EXPECTED_STALE_PREVIEW", "event": dict(event)})
            continue

        external = next(
            (item for item in external_exemptions if item.url == url and item.event_kind == kind and _all_true(item.no_product_impact_evidence)),
            None,
        )
        if external is not None:
            classified.append({"classification": "EXPECTED_EXTERNAL_NONPRODUCT", "event": dict(event)})
            continue

        if kind == "request_failed" and "ERR_ABORTED" in error_text:
            navigation = next(
                (
                    item
                    for item in navigation_exemptions
                    if item.navigation_id == event.get("navigation_id")
                    and item.cancellation_action_id == event.get("cancellation_action_id")
                    and item.final_url == event.get("final_url")
                    and _all_true(item.usable_result_evidence)
                ),
                None,
            )
            if navigation is not None:
                classified.append({"classification": "EXPECTED_NAVIGATION_ABORT", "event": dict(event)})
                continue

        if kind == "http_response" and status < 400:
            classified.append({"classification": "HTTP_OK", "event": dict(event)})
            continue
        if kind == "http_response" and status >= 400:
            classification = "UNEXPECTED_HTTP_STATUS"
        elif kind == "request_failed":
            classification = "UNEXPECTED_REQUEST_FAILED"
        elif kind == "console_error":
            classification = "UNEXPECTED_CONSOLE_ERROR"
        elif kind == "page_error":
            classification = "UNEXPECTED_PAGE_ERROR"
        else:
            classification = "UNCLASSIFIED_EVENT"
        item = {"classification": classification, "event": dict(event)}
        unexpected.append(item)
        classified.append(item)

    return {
        "status": "PASS" if not unexpected and not issues else "FAIL",
        "classified": classified,
        "unexpected": unexpected,
        "issues": issues,
        "http_error_count": sum(1 for item in unexpected if item["classification"] == "UNEXPECTED_HTTP_STATUS"),
        "request_failed_count": sum(1 for item in unexpected if item["classification"] == "UNEXPECTED_REQUEST_FAILED"),
    }


def cleanup_owned_fixtures(
    fixtures: Iterable[OwnedFixture],
    *,
    run_id: str,
    delete_project: Callable[[str], int],
    project_exists: Callable[[str], bool],
    expected_delete_status: int = 204,
) -> dict[str, Any]:
    deleted: list[str] = []
    preserved: list[str] = []
    absence_evidence: dict[str, bool] = {}
    failures: list[dict[str, Any]] = []
    for fixture in fixtures:
        owned = fixture.owner_run_id == run_id and fixture.created_this_run and not fixture.pre_existing
        if not owned:
            preserved.append(fixture.project_id)
            continue
        try:
            status = int(delete_project(fixture.project_id))
        except Exception as exc:  # noqa: BLE001 - surfaced as terminal evidence
            failures.append({"project_id": fixture.project_id, "failure": sanitize_failure(exc)})
            continue
        if status != expected_delete_status:
            failures.append({"project_id": fixture.project_id, "delete_status": status, "expected_status": expected_delete_status})
            continue
        try:
            absent = not bool(project_exists(fixture.project_id))
        except Exception as exc:  # noqa: BLE001
            failures.append({"project_id": fixture.project_id, "absence_check_failure": sanitize_failure(exc)})
            continue
        absence_evidence[fixture.project_id] = absent
        if not absent:
            failures.append({"project_id": fixture.project_id, "absence_evidence": False})
            continue
        deleted.append(fixture.project_id)
    return {
        "status": "PASS" if not failures else "FAIL",
        "run_id": run_id,
        "deleted": deleted,
        "preserved": preserved,
        "absence_evidence": absence_evidence,
        "failures": failures,
    }


def cleanup_evidence_passes(cleanup: Mapping[str, Any]) -> bool:
    failures = cleanup.get("failures")
    absence = cleanup.get("absence_evidence")
    return bool(
        cleanup.get("status") == "PASS"
        and (not isinstance(failures, list) or not failures)
        and (
            not isinstance(absence, Mapping)
            or all(value is True for value in absence.values())
        )
    )


def new_attempt_report(
    *,
    run_id: str,
    test_layer: str,
    auth_mode: str,
    expected_identity: ReleaseIdentity | None = None,
    observed_identity: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    return {
        "schema": "archbro.release_acceptance_evidence.v1",
        "run_id": run_id,
        "test_layer": test_layer,
        "auth_mode": auth_mode,
        "result": "RUNNING",
        "started_at": utc_now(),
        "identity": {
            "expected": asdict(expected_identity) if expected_identity else None,
            "observed": asdict(observed_identity) if observed_identity else None,
            "status": "NOT_RUN",
        },
        "fixture_admission": "BLOCKED_UNTIL_IDENTITY_PASS",
        "checks": {name: {"status": "NOT_REACHED"} for name in MANDATORY_CHECKS},
        "steps": [],
        "browser": None,
        "cleanup": None,
        "auth_state_lifecycle": {"mode": auth_mode, "profile_deleted": False},
        "database_lifecycle": {"managed_by_verifier": False},
    }


def mark_identity_pass(report: MutableMapping[str, Any], evidence: Mapping[str, Any]) -> None:
    report["identity"] = {**dict(evidence), "status": "PASS"}
    report["fixture_admission"] = "ALLOWED"


def set_check(
    report: MutableMapping[str, Any],
    name: str,
    status: str,
    *,
    evidence: Any = None,
    reason: str | None = None,
    blocked_by: str | None = None,
) -> None:
    if name not in ASSERTION_MAP:
        raise EvidenceError(f"unknown mandatory check: {name}")
    if status not in ALL_CHECK_STATUSES:
        raise EvidenceError(f"invalid check status: {status}")
    item: dict[str, Any] = {"status": status, "assertion": ASSERTION_MAP[name]}
    if evidence is not None:
        item["evidence"] = evidence
    if reason is not None:
        item["reason"] = sanitize_failure(reason)
    if blocked_by is not None:
        item["blocked_by"] = blocked_by
    report["checks"][name] = item


def block_unreached_checks(report: MutableMapping[str, Any], *, root_cause: str) -> None:
    for name in MANDATORY_CHECKS:
        if report["checks"][name].get("status") == "NOT_REACHED":
            set_check(report, name, "NOT_REACHED", blocked_by=sanitize_failure(root_cause))


class StepCheckpoint:
    def __init__(self, report: MutableMapping[str, Any], name: str):
        self.report = report
        self.name = name
        self.started = 0.0
        self.item: dict[str, Any] = {}

    def __enter__(self) -> "StepCheckpoint":
        self.started = time.perf_counter()
        self.item = {"name": self.name, "status": "RUNNING", "started_at": utc_now()}
        self.report["steps"].append(self.item)
        return self

    def add(self, **evidence: Any) -> None:
        self.item.setdefault("evidence", {}).update(evidence)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.item["finished_at"] = utc_now()
        self.item["duration_s"] = round(time.perf_counter() - self.started, 6)
        if exc is None:
            self.item["status"] = "PASS"
        else:
            self.item["status"] = "FAIL"
            self.item["failure"] = sanitize_failure(exc)
        return False


def validate_auth_mode(auth_mode: str, capability_evidence: Mapping[str, Any] | None = None) -> None:
    if auth_mode == "storage_state":
        return
    if auth_mode == "workbench_attach":
        if not isinstance(capability_evidence, Mapping) or capability_evidence.get("supported") is not True:
            raise EvidenceError("workbench_attach requires measured supported capability evidence")
        return
    raise EvidenceError(f"unsupported auth mode: {auth_mode}")


def finalize_report(
    report: MutableMapping[str, Any],
    *,
    cleanup: Mapping[str, Any],
    browser: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report["cleanup"] = dict(cleanup)
    if browser is not None:
        report["browser"] = dict(browser)
    missing = [name for name in MANDATORY_CHECKS if report["checks"][name].get("status") == "NOT_REACHED"]
    failed = [name for name in MANDATORY_CHECKS if report["checks"][name].get("status") == "FAIL"]
    unavailable = [name for name in MANDATORY_CHECKS if report["checks"][name].get("status") == "UNAVAILABLE"]
    report["required"] = list(MANDATORY_CHECKS)
    report["missing"] = missing
    report["failed"] = failed
    report["unavailable"] = unavailable
    cleanup_failed = not cleanup_evidence_passes(cleanup)
    browser_failed = browser is not None and browser.get("status") != "PASS"
    identity_failed = report.get("identity", {}).get("status") != "PASS"
    verifier_failed = bool(report.get("failure"))
    step_failed = any(
        not isinstance(step, Mapping) or step.get("status") != "PASS"
        for step in report.get("steps", [])
    )
    if identity_failed or cleanup_failed or browser_failed or failed or verifier_failed or step_failed:
        result = "FAIL"
    elif missing:
        result = "INCOMPLETE"
    elif unavailable:
        result = "UNAVAILABLE"
    else:
        result = "PASS"
    report["result"] = result
    report["finished_at"] = utc_now()
    return dict(report)
