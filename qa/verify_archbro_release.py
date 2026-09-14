from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from qa.archbro_release_evidence import (
    AgentRunEvidenceError,
    ExternalResourceExemption,
    IdentityPreflightError,
    MANDATORY_CHECKS,
    NavigationAbortExemption,
    ReleaseIdentity,
    Stale409Expectation,
    StepCheckpoint,
    block_unreached_checks,
    classify_browser_events,
    expected_identity_from_release_unit,
    finalize_report,
    identity_from_mapping,
    mark_identity_pass,
    new_attempt_report,
    preflight_identity,
    sanitize_failure,
    set_check,
    validate_agent_run_response,
    validate_auth_mode,
)
from qa.archbro_release_runtime import authoritative_observed_identity


_RECOMPUTED_CHECKS = {
    "ask_agent_exact_context_and_provider_telemetry_pass",
    "stale_preview_fails_closed_before_provider_pass",
    "browser_console_page_network_failures_recorded_pass",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp.replace(path)


def _identity_report(expected: ReleaseIdentity | None, observed: ReleaseIdentity | None, *, status: str, failure: str | None = None) -> dict[str, Any]:
    expected_map = expected.__dict__.copy() if expected else None
    observed_map = observed.__dict__.copy() if observed else None
    item: dict[str, Any] = {
        "status": status,
        "expected": expected_map,
        "observed": observed_map,
    }
    if expected_map:
        item.update({f"expected_{key}": value for key, value in expected_map.items()})
    if observed_map:
        item.update({f"observed_{key}": value for key, value in observed_map.items()})
    if failure:
        item["failure"] = sanitize_failure(failure)
    return item


def _stale_expectation(value: object) -> Stale409Expectation | None:
    if not isinstance(value, Mapping):
        return None
    if int(value.get("expected_count", 1)) != 1:
        raise ValueError("release verification policy requires exactly one stale-preview 409")
    return Stale409Expectation(
        project_id=str(value["project_id"]),
        action_id=str(value["action_id"]),
        request_id=str(value["request_id"]),
        method=str(value.get("method", "POST")),
        path=str(value["path"]) if value.get("path") else None,
        error_code=str(value.get("error_code", "agent_context_preview_stale")),
        expected_count=1,
    )


def _external_exemptions(value: object) -> list[ExternalResourceExemption]:
    if not isinstance(value, list):
        return []
    result: list[ExternalResourceExemption] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            ExternalResourceExemption(
                url=str(item["url"]),
                event_kind=str(item["event_kind"]),
                no_product_impact_evidence=dict(item.get("no_product_impact_evidence") or {}),
            )
        )
    return result


def _navigation_exemptions(value: object) -> list[NavigationAbortExemption]:
    if not isinstance(value, list):
        return []
    result: list[NavigationAbortExemption] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            NavigationAbortExemption(
                navigation_id=str(item["navigation_id"]),
                cancellation_action_id=str(item["cancellation_action_id"]),
                final_url=str(item["final_url"]),
                usable_result_evidence=dict(item.get("usable_result_evidence") or {}),
            )
        )
    return result


def _apply_check_observations(
    report: dict[str, Any],
    evidence: Mapping[str, Any],
    *,
    run_id: str,
    observed_identity: ReleaseIdentity,
) -> None:
    supplied = evidence.get("check_observations")
    if not isinstance(supplied, Mapping):
        return
    identity = observed_identity.__dict__
    for name in MANDATORY_CHECKS:
        if name in _RECOMPUTED_CHECKS:
            continue
        item = supplied.get(name)
        if not isinstance(item, Mapping):
            continue
        item_run_id = item.get("run_id")
        scenario = item.get("scenario")
        assertions = item.get("assertions")
        artifact_identity = item.get("artifact_identity")
        reason = None
        if item_run_id != run_id:
            reason = "check observation run_id does not match verifier run_id"
        elif not isinstance(scenario, str) or not scenario.strip():
            reason = "check observation scenario is required"
        elif not isinstance(assertions, Mapping) or not assertions:
            reason = "check observation requires non-empty raw assertions"
        elif any(type(value) is not bool for value in assertions.values()):
            reason = "check observation assertions must be booleans"
        elif not all(assertions.values()):
            reason = "check observation contains a failed assertion"
        elif not isinstance(artifact_identity, Mapping):
            reason = "check observation artifact_identity is required"
        elif any(artifact_identity.get(field) != identity[field] for field in identity):
            reason = "check observation artifact identity does not match verified target"
        set_check(
            report,
            name,
            "FAIL" if reason else "PASS",
            evidence={
                "run_id": item_run_id,
                "scenario": scenario,
                "assertions": dict(assertions) if isinstance(assertions, Mapping) else None,
                "artifact_identity": dict(artifact_identity) if isinstance(artifact_identity, Mapping) else None,
            },
            reason=reason,
        )


def _apply_agent_run(report: dict[str, Any], evidence: Mapping[str, Any]) -> str | None:
    agent = evidence.get("agent_run")
    if not isinstance(agent, Mapping):
        return None
    check_name = "ask_agent_exact_context_and_provider_telemetry_pass"
    try:
        validated = validate_agent_run_response(
            int(agent.get("http_status", 0)),
            agent.get("payload") if isinstance(agent.get("payload"), Mapping) else None,
            expected_manifest_hash=str(agent["expected_manifest_hash"]) if agent.get("expected_manifest_hash") is not None else None,
            expected_architecture_version=int(agent["expected_architecture_version"]) if agent.get("expected_architecture_version") is not None else None,
            require_provider_usage=True,
        )
    except AgentRunEvidenceError as exc:
        set_check(report, check_name, "FAIL", reason=str(exc))
        block_unreached_checks(report, root_cause=str(exc))
        return str(exc)
    set_check(report, check_name, "PASS", evidence=validated)
    return None


def _apply_browser(report: dict[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_events = evidence.get("browser_events")
    if not isinstance(raw_events, list):
        return None
    events = [item for item in raw_events if isinstance(item, Mapping)]
    try:
        stale = _stale_expectation(evidence.get("stale_409_expectation"))
    except (KeyError, TypeError, ValueError) as exc:
        reason = sanitize_failure(exc)
        set_check(
            report,
            "stale_preview_fails_closed_before_provider_pass",
            "FAIL",
            reason=reason,
        )
        set_check(
            report,
            "browser_console_page_network_failures_recorded_pass",
            "FAIL",
            reason="browser verification policy input is invalid",
        )
        return {
            "status": "FAIL",
            "classified": [],
            "unexpected": [],
            "issues": [reason],
            "http_error_count": 0,
            "request_failed_count": 0,
        }
    browser = classify_browser_events(
        events,
        stale_expectation=stale,
        # Exemptions are verification policy, not observations. Evidence cannot
        # grant itself exceptions to mandatory browser failure classification.
        external_exemptions=(),
        navigation_exemptions=(),
    )
    set_check(
        report,
        "browser_console_page_network_failures_recorded_pass",
        "PASS" if browser["status"] == "PASS" else "FAIL",
        evidence={
            "http_error_count": browser["http_error_count"],
            "request_failed_count": browser["request_failed_count"],
            "issues": browser["issues"],
            "unexpected": browser["unexpected"],
        },
        reason="unexpected or unclassified browser failure evidence" if browser["status"] != "PASS" else None,
    )
    if stale is not None:
        stale_count = sum(1 for item in browser["classified"] if item.get("classification") == "EXPECTED_STALE_PREVIEW")
        stale_ok = stale_count == stale.expected_count and not any("stale-preview" in issue for issue in browser["issues"])
        set_check(
            report,
            "stale_preview_fails_closed_before_provider_pass",
            "PASS" if stale_ok else "FAIL",
            evidence={"exact_stale_count": stale_count, "expected_count": stale.expected_count},
            reason="stale-preview 409 did not match the exact owned action/request contract" if not stale_ok else None,
        )
    return browser


def build_report(
    *,
    release_unit: Mapping[str, Any],
    observed_identity_data: Mapping[str, Any],
    evidence: Mapping[str, Any],
    external_image_observation: Mapping[str, Any],
    run_id: str,
    test_layer: str,
    auth_mode: str,
    harness_root: str | Path | None = None,
) -> dict[str, Any]:
    expected: ReleaseIdentity | None = None
    observed: ReleaseIdentity | None = None
    report = new_attempt_report(run_id=run_id, test_layer=test_layer, auth_mode=auth_mode)
    cleanup = evidence.get("cleanup") if isinstance(evidence.get("cleanup"), Mapping) else {
        "status": "FAIL",
        "failures": [{"reason": "cleanup evidence missing"}],
        "deleted": [],
        "preserved": [],
        "absence_evidence": {},
    }
    if cleanup.get("run_id") != run_id:
        cleanup = {
            **dict(cleanup),
            "status": "FAIL",
            "failures": [
                *list(cleanup.get("failures") or []),
                {"reason": "cleanup evidence run_id does not match verifier run_id"},
            ],
        }
    browser: dict[str, Any] | None = None

    try:
        validate_auth_mode(auth_mode, evidence.get("auth_capability") if isinstance(evidence.get("auth_capability"), Mapping) else None)
        with StepCheckpoint(report, "release_identity_preflight"):
            expected = expected_identity_from_release_unit(release_unit)
            authoritative = authoritative_observed_identity(
                observed_identity_data,
                external_image_observation=external_image_observation,
                harness_root=harness_root,
            )
            observed = identity_from_mapping(authoritative, where="authoritative_observed_identity")
            identity_evidence = preflight_identity(expected, observed)
            mark_identity_pass(report, identity_evidence)
            report["identity"] = _identity_report(expected, observed, status="PASS")
        if evidence.get("fixture_creation_started_before_identity") is True:
            raise IdentityPreflightError("fixture creation started before identity preflight completed")

        with StepCheckpoint(report, "acceptance_evidence_classification"):
            assert observed is not None
            _apply_check_observations(
                report,
                evidence,
                run_id=run_id,
                observed_identity=observed,
            )
            provider_failure = _apply_agent_run(report, evidence)
            browser = _apply_browser(report, evidence)
            if provider_failure:
                report["root_cause"] = sanitize_failure(provider_failure)
    except Exception as exc:  # noqa: BLE001 - report must survive every verifier failure
        report["failure"] = sanitize_failure(exc)
        if isinstance(exc, IdentityPreflightError):
            report["identity"] = _identity_report(expected, observed, status="FAIL", failure=str(exc))
            report["fixture_admission"] = "BLOCKED"
        block_unreached_checks(report, root_cause=str(exc))

    report["verdict_owner"] = "qa.verify_archbro_release"
    return finalize_report(report, cleanup=cleanup, browser=browser)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify ArchBro release acceptance evidence without starting a runtime.")
    parser.add_argument("--release-unit", type=Path, required=True, help="Frozen manifests/RELEASE_UNIT.json")
    parser.add_argument("--observed-identity", type=Path, required=True, help="Observed release identity JSON from the target/runtime lane")
    parser.add_argument("--external-image-observation", type=Path, required=True, help="External Docker/OCI image observation JSON")
    parser.add_argument("--evidence", type=Path, required=True, help="Runtime acceptance evidence bundle JSON")
    parser.add_argument("--report", type=Path, required=True, help="Attempt report output path")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--test-layer", default="real_authenticated_browser")
    parser.add_argument("--auth-mode", choices=("storage_state", "workbench_attach"), default="storage_state")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report: dict[str, Any]
    try:
        report = build_report(
            release_unit=_load_json(args.release_unit),
            observed_identity_data=_load_json(args.observed_identity),
            evidence=_load_json(args.evidence),
            external_image_observation=_load_json(args.external_image_observation),
            run_id=args.run_id,
            test_layer=args.test_layer,
            auth_mode=args.auth_mode,
        )
    except Exception as exc:  # noqa: BLE001 - even malformed inputs get an attempt report
        report = new_attempt_report(run_id=args.run_id, test_layer=args.test_layer, auth_mode=args.auth_mode)
        report["failure"] = sanitize_failure(exc)
        report["identity"]["status"] = "FAIL"
        block_unreached_checks(report, root_cause=str(exc))
        report = finalize_report(
            report,
            cleanup={"status": "FAIL", "failures": [{"reason": "verifier input failure; cleanup evidence unavailable"}]},
        )
    finally:
        if "report" in locals():
            _write_json(args.report, report)
    if report["result"] == "PASS":
        return 0
    if report["result"] in {"UNAVAILABLE", "INCOMPLETE"}:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
