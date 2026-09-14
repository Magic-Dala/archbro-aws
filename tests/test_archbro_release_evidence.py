from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from qa.archbro_release_evidence import (
    ASSERTION_MAP,
    AgentRunEvidenceError,
    ExternalResourceExemption,
    IdentityPreflightError,
    MANDATORY_CHECKS,
    NavigationAbortExemption,
    OwnedFixture,
    ReleaseIdentity,
    Stale409Expectation,
    classify_browser_events,
    cleanup_evidence_passes,
    cleanup_owned_fixtures,
    finalize_report,
    mark_identity_pass,
    new_attempt_report,
    preflight_and_create_fixture,
    preflight_identity,
    set_check,
    validate_agent_run_response,
    validate_auth_mode,
)
from qa.verify_archbro_release import build_report
from qa.archbro_release_runtime import acceptance_harness_sha256


ROOT = Path(__file__).resolve().parents[1]


def identity() -> ReleaseIdentity:
    return ReleaseIdentity(
        source_sha="a" * 40,
        source_tree="b" * 40,
        image_digest="sha256:" + "c" * 64,
        harness_hash=acceptance_harness_sha256(ROOT),
        dependency_fingerprint="e" * 64,
        safe_config_fingerprint="f" * 64,
        target_origin="https://archbro.example.test",
    )


def runtime_identity(value: ReleaseIdentity) -> dict[str, object]:
    dependency_hash = value.dependency_fingerprint
    return {
        **value.__dict__,
        "source": {
            "git_sha": value.source_sha,
            "git_tree": value.source_tree,
            "manifest_matches": True,
            "manifest_status": "verified",
            "manifest_sha256": "9" * 64,
            "observed_manifest_sha256": "9" * 64,
        },
        "runtime": {
            "dependency_manifest_matches": True,
            "dependency_manifest_status": "verified",
            "expected_dependency_manifest_sha256": dependency_hash,
            "observed_dependency_manifest_sha256": dependency_hash,
        },
        "harness": {"sha256": value.harness_hash},
        "image": {"manifest_digest": value.image_digest},
    }


def external_image(value: ReleaseIdentity) -> dict[str, str]:
    image_id = "sha256:" + "1" * 64
    return {
        "schema": "archbro.release.image_observation.v1",
        "image_reference": "registry.example/archbro@" + value.image_digest,
        "manifest_digest": value.image_digest,
        "image_id": image_id,
        "running_image_id": image_id,
    }


def release_unit(value: ReleaseIdentity | None = None) -> dict:
    value = value or identity()
    return {"schema": "archbro.release_unit.v1", "expected_identity": value.__dict__.copy()}


def all_pass_report() -> dict:
    expected = identity()
    report = new_attempt_report(
        run_id="run-11",
        test_layer="unit_contract",
        auth_mode="storage_state",
        expected_identity=expected,
        observed_identity=expected,
    )
    mark_identity_pass(report, preflight_identity(expected, expected))
    for name in MANDATORY_CHECKS:
        set_check(report, name, "PASS", evidence={"test_layer": "unit_contract"})
    return report


def check_observations(
    value: ReleaseIdentity,
    *,
    run_id: str,
    names: tuple[str, ...] | list[str] | None = None,
) -> dict[str, dict[str, object]]:
    selected = names or [
        name
        for name in MANDATORY_CHECKS
        if name not in {
            "ask_agent_exact_context_and_provider_telemetry_pass",
            "stale_preview_fails_closed_before_provider_pass",
            "browser_console_page_network_failures_recorded_pass",
        }
    ]
    return {
        name: {
            "run_id": run_id,
            "scenario": f"scenario:{name}",
            "assertions": {"observed": True},
            "artifact_identity": value.__dict__.copy(),
        }
        for name in selected
    }


def complete_evidence(value: ReleaseIdentity, *, run_id: str) -> dict[str, object]:
    return {
        "check_observations": check_observations(value, run_id=run_id),
        "agent_run": {
            "http_status": 200,
            "payload": {
                "result": "SUCCESS",
                "provider": "real-provider",
                "model": "real-model",
                "context_telemetry": {"manifest_hash": "mh-1", "architecture_version": 7},
                "provider_usage": {"input_tokens": 10, "output_tokens": 2},
            },
            "expected_manifest_hash": "mh-1",
            "expected_architecture_version": 7,
        },
        "browser_events": [
            {
                "kind": "http_response",
                "status": 409,
                "method": "POST",
                "path": "/projects/project-owned/events",
                "project_id": "project-owned",
                "error_code": "agent_context_preview_stale",
                "action_id": "action-1",
                "request_id": "request-1",
            }
        ],
        "stale_409_expectation": {
            "project_id": "project-owned",
            "action_id": "action-1",
            "request_id": "request-1",
        },
        "cleanup": {"status": "PASS", "run_id": run_id, "deleted": [], "preserved": [], "absence_evidence": {}, "failures": []},
    }


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("source_sha", "b" * 40),
        ("image_digest", "sha256:wrong-image"),
        ("harness_hash", "sha256:wrong-harness"),
    ],
)
def test_identity_mismatch_rejects_before_fixture_creation(field: str, wrong: str) -> None:
    expected = identity()
    observed = replace(expected, **{field: wrong})
    created: list[str] = []

    with pytest.raises(IdentityPreflightError, match=field):
        preflight_and_create_fixture(expected, observed, lambda: created.append("fixture"))

    assert created == []


def test_http_200_with_agent_error_is_failure_with_closest_cause() -> None:
    with pytest.raises(AgentRunEvidenceError, match="provider quota exhausted"):
        validate_agent_run_response(
            200,
            {
                "result": "ERROR",
                "error": "ProviderError: provider quota exhausted",
                "summary": "fallback summary",
                "context_telemetry": None,
                "provider_usage": None,
            },
        )


def test_agent_success_requires_exact_context_and_observed_provider_usage() -> None:
    evidence = validate_agent_run_response(
        200,
        {
            "result": "SUCCESS",
            "provider": "real-provider",
            "model": "real-model",
            "context_telemetry": {"manifest_hash": "mh-1", "architecture_version": 7},
            "provider_usage": {"input_tokens": 321, "output_tokens": 45},
        },
        expected_manifest_hash="mh-1",
        expected_architecture_version=7,
    )
    assert evidence["agent_result"] == "SUCCESS"
    assert evidence["provider_usage"] == {"input_tokens": 321, "output_tokens": 45}


def test_non_stale_409_is_not_exempt_even_on_same_events_path() -> None:
    expected = Stale409Expectation(project_id="project-owned", action_id="action-1", request_id="request-1")
    result = classify_browser_events(
        [
            {
                "kind": "http_response",
                "status": 409,
                "method": "POST",
                "path": "/projects/project-owned/events",
                "project_id": "project-owned",
                "error_code": "stale_architecture_version",
                "action_id": "action-1",
                "request_id": "request-1",
            }
        ],
        stale_expectation=expected,
    )
    assert result["status"] == "FAIL"
    assert result["unexpected"][0]["classification"] == "UNEXPECTED_HTTP_STATUS"
    assert result["http_error_count"] == 1


def test_exact_test_owned_stale_409_is_exempt_once() -> None:
    expected = Stale409Expectation(project_id="project-owned", action_id="action-1", request_id="request-1")
    result = classify_browser_events(
        [
            {
                "kind": "http_response",
                "status": 409,
                "method": "POST",
                "path": "/projects/project-owned/events",
                "project_id": "project-owned",
                "error_code": "agent_context_preview_stale",
                "action_id": "action-1",
                "request_id": "request-1",
            }
        ],
        stale_expectation=expected,
    )
    assert result["status"] == "PASS"
    assert result["classified"][0]["classification"] == "EXPECTED_STALE_PREVIEW"
    assert result["unexpected"] == []


def test_navigation_err_aborted_without_complete_linkage_is_not_exempt() -> None:
    exemption = NavigationAbortExemption(
        navigation_id="nav-1",
        cancellation_action_id="cancel-1",
        final_url="https://archbro.example.test/projects/p1",
        usable_result_evidence={"workspace_visible": True, "target_project_loaded": True},
    )
    result = classify_browser_events(
        [
            {
                "kind": "request_failed",
                "url": "https://archbro.example.test/projects/p1",
                "error_text": "net::ERR_ABORTED",
                "navigation_id": "nav-1",
                "cancellation_action_id": "cancel-1",
            }
        ],
        navigation_exemptions=[exemption],
    )
    assert result["status"] == "FAIL"
    assert result["unexpected"][0]["classification"] == "UNEXPECTED_REQUEST_FAILED"
    assert result["request_failed_count"] == 1


def test_exact_external_resource_needs_no_product_impact_proof() -> None:
    event = {
        "kind": "request_failed",
        "url": "https://static.cloudflareinsights.com/beacon.min.js",
        "error_text": "blocked by client",
    }
    denied = classify_browser_events(
        [event],
        external_exemptions=[
            ExternalResourceExemption(
                url=event["url"],
                event_kind="request_failed",
                no_product_impact_evidence={},
            )
        ],
    )
    allowed = classify_browser_events(
        [event],
        external_exemptions=[
            ExternalResourceExemption(
                url=event["url"],
                event_kind="request_failed",
                no_product_impact_evidence={"app_ready": True, "required_api_success": True},
            )
        ],
    )
    assert denied["status"] == "FAIL"
    assert allowed["status"] == "PASS"
    assert allowed["classified"][0]["classification"] == "EXPECTED_EXTERNAL_NONPRODUCT"


def test_missing_any_mandatory_check_cannot_pass() -> None:
    report = all_pass_report()
    missing = MANDATORY_CHECKS[-1]
    report["checks"][missing] = {"status": "NOT_REACHED"}
    final = finalize_report(report, cleanup={"status": "PASS", "deleted": [], "preserved": [], "absence_evidence": {}, "failures": []})
    assert final["result"] == "INCOMPLETE"
    assert final["missing"] == [missing]
    assert len(final["required"]) == 18
    assert tuple(ASSERTION_MAP) == MANDATORY_CHECKS


def test_delete_failure_is_terminal_and_prevents_pass() -> None:
    fixture = OwnedFixture(project_id="owned", owner_run_id="run-11", created_this_run=True)
    cleanup = cleanup_owned_fixtures(
        [fixture],
        run_id="run-11",
        delete_project=lambda _project_id: 500,
        project_exists=lambda _project_id: True,
    )
    final = finalize_report(all_pass_report(), cleanup=cleanup)
    assert cleanup["status"] == "FAIL"
    assert cleanup["failures"][0]["delete_status"] == 500
    assert final["result"] == "FAIL"


def test_cleanup_never_deletes_other_or_preexisting_project() -> None:
    calls: list[str] = []
    fixtures = [
        OwnedFixture(project_id="owned", owner_run_id="run-11", created_this_run=True),
        OwnedFixture(project_id="other-run", owner_run_id="run-12", created_this_run=True),
        OwnedFixture(project_id="pre-existing", owner_run_id="run-11", created_this_run=False, pre_existing=True),
    ]
    cleanup = cleanup_owned_fixtures(
        fixtures,
        run_id="run-11",
        delete_project=lambda project_id: calls.append(project_id) or 204,
        project_exists=lambda project_id: project_id != "other-run" and project_id != "pre-existing" and False,
    )
    assert calls == ["owned"]
    assert cleanup["deleted"] == ["owned"]
    assert cleanup["preserved"] == ["other-run", "pre-existing"]
    assert cleanup["status"] == "PASS"


def test_provider_failure_is_one_product_failure_and_later_checks_not_reached() -> None:
    base = identity()
    observed = check_observations(base, run_id="run-11", names=list(MANDATORY_CHECKS[:6]))
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence={
            "check_observations": observed,
            "agent_run": {
                "http_status": 200,
                "payload": {"result": "ERROR", "error": "ProviderError: upstream unavailable"},
            },
            "cleanup": {"status": "PASS", "run_id": "run-11", "deleted": [], "preserved": [], "absence_evidence": {}, "failures": []},
        },
        run_id="run-11",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )
    failed = [name for name, item in report["checks"].items() if item["status"] == "FAIL"]
    assert failed == ["ask_agent_exact_context_and_provider_telemetry_pass"]
    assert report["checks"]["explore_exact_context_pass"]["status"] == "NOT_REACHED"
    assert "upstream unavailable" in report["root_cause"]


def test_identity_report_separates_expected_and_observed_fields() -> None:
    base = identity()
    evidence = complete_evidence(base, run_id="run-11")
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence=evidence,
        run_id="run-11",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )
    assert report["identity"]["expected_source_sha"] == base.source_sha
    assert report["identity"]["observed_source_sha"] == base.source_sha
    assert report["test_layer"] == "unit_contract"
    assert report["result"] == "PASS"
    assert report["verdict_owner"] == "qa.verify_archbro_release"


def test_workbench_attach_requires_measured_supported_capability() -> None:
    with pytest.raises(Exception, match="measured supported capability"):
        validate_auth_mode("workbench_attach", {"supported": False, "tested": True})


def _declared_pass_checks() -> dict[str, dict[str, object]]:
    return {
        name: {"status": "PASS", "evidence": {"claimed": True}}
        for name in MANDATORY_CHECKS
    }


def _pass_cleanup(**overrides) -> dict[str, object]:
    return {
        "status": "PASS",
        "deleted": [],
        "preserved": [],
        "absence_evidence": {},
        "failures": [],
        **overrides,
    }


def test_declared_pass_checks_without_raw_evidence_cannot_accept_release() -> None:
    base = identity()
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence={"checks": _declared_pass_checks(), "cleanup": _pass_cleanup()},
        run_id="run-raw-evidence-required",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )

    assert report["result"] != "PASS"


def test_verifier_exception_after_declared_checks_cannot_be_reduced_to_pass() -> None:
    base = identity()
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence={
            "checks": _declared_pass_checks(),
            # Raw event is intentionally malformed so classification itself raises
            # after identity preflight and the acceptance-classification step begins.
            "browser_events": [{"kind": "http_response", "status": "not-an-integer"}],
            "cleanup": _pass_cleanup(),
        },
        run_id="run-verifier-failure",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )

    assert report["result"] != "PASS"
    assert report.get("failure")
    assert any(step.get("status") == "FAIL" for step in report["steps"])


def test_evidence_cannot_disable_mandatory_provider_usage() -> None:
    base = identity()
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence={
            "checks": _declared_pass_checks(),
            "agent_run": {
                "http_status": 200,
                "payload": {
                    "result": "SUCCESS",
                    "provider": "real-provider",
                    "model": "real-model",
                    "context_telemetry": {"manifest_hash": "mh", "architecture_version": 7},
                    "provider_usage": None,
                },
                "expected_manifest_hash": "mh",
                "expected_architecture_version": 7,
                "require_provider_usage": False,
            },
            "cleanup": _pass_cleanup(),
        },
        run_id="run-provider-policy",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )

    assert report["result"] != "PASS"
    assert report["checks"]["ask_agent_exact_context_and_provider_telemetry_pass"]["status"] == "FAIL"


def test_evidence_cannot_set_stale_expectation_count_to_zero() -> None:
    base = identity()
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence={
            "checks": _declared_pass_checks(),
            "browser_events": [],
            "stale_409_expectation": {
                "project_id": "project-owned",
                "action_id": "action-1",
                "request_id": "request-1",
                "expected_count": 0,
            },
            "cleanup": _pass_cleanup(),
        },
        run_id="run-stale-policy",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )

    assert report["result"] != "PASS"
    assert report["checks"]["stale_preview_fails_closed_before_provider_pass"]["status"] == "FAIL"


def test_cleanup_claimed_pass_with_failures_cannot_accept_release() -> None:
    final = finalize_report(
        all_pass_report(),
        cleanup=_pass_cleanup(failures=[{"project_id": "owned", "delete_status": 500}]),
    )

    assert final["result"] != "PASS"
    assert cleanup_evidence_passes(_pass_cleanup()) is True
    assert cleanup_evidence_passes(
        _pass_cleanup(failures=[{"project_id": "owned", "delete_status": 500}])
    ) is False


def test_nested_source_manifest_mismatch_cannot_be_hidden_by_matching_flat_identity() -> None:
    base = identity()
    observed = runtime_identity(base)
    observed["source"] = {
        "manifest_matches": False,
        "manifest_status": "mismatch",
        "manifest_sha256": "1" * 64,
        "observed_manifest_sha256": "2" * 64,
        "git_sha": base.source_sha,
        "git_tree": base.source_tree,
    }
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=observed,
        external_image_observation=external_image(base),
        evidence={"checks": _declared_pass_checks(), "cleanup": _pass_cleanup()},
        run_id="run-nested-source-mismatch",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )

    assert report["result"] != "PASS"
    assert report["identity"]["status"] == "FAIL"


def test_cleanup_from_different_run_cannot_be_replayed_into_pass() -> None:
    base = identity()
    evidence = complete_evidence(base, run_id="run-current")
    evidence["cleanup"]["run_id"] = "run-old"
    report = build_report(
        release_unit=release_unit(base),
        observed_identity_data=runtime_identity(base),
        external_image_observation=external_image(base),
        evidence=evidence,
        run_id="run-current",
        test_layer="unit_contract",
        auth_mode="storage_state",
    )
    assert report["result"] != "PASS"
    assert report["cleanup"]["status"] == "FAIL"
