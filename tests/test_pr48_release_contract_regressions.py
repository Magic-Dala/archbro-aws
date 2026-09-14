from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from archbro.platform.runtime.release_identity import (
    DEPENDENCY_MANIFEST_SCHEMA, RELEASE_METADATA_SCHEMA, SOURCE_MANIFEST_SCHEMA,
    build_runtime_identity, database_readiness, file_sha256, safe_config_fingerprint,
    safe_runtime_config, verify_dependency_manifest, verify_source_manifest,
)
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.runtime.app import create_app
from qa.archbro_release_runtime import (
    ContractError,
    acceptance_harness_sha256,
    authoritative_observed_identity,
    compare_release_unit_identity,
    normalize_external_image_observation,
    observe_docker_image_identity,
)


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        relations = list(params[0]) if params else []
        rows = [
            {
                "relation": relation,
                "resolved": None if relation == "public.planner_checkpoints" else relation,
            }
            for relation in relations
        ]
        return SimpleNamespace(fetchall=lambda: rows)


def test_missing_planner_checkpoint_table_is_not_ready():
    result = database_readiness(SimpleNamespace(_connect=_Connection))
    assert result["status"] == "not_ready"
    assert "public.planner_checkpoints" in result["missing_relations"]


def test_acceptance_harness_hash_changes_when_any_trust_bearing_file_changes(tmp_path):
    root = tmp_path
    for relative in (
        "qa/verify_archbro_release.py",
        "qa/archbro_release_evidence.py",
        "qa/playwright_release_acceptance.py",
        "qa/archbro_release_runtime.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")
    first = acceptance_harness_sha256(root)
    (root / "qa/archbro_release_evidence.py").write_text("changed\n", encoding="utf-8")
    second = acceptance_harness_sha256(root)
    assert first != second


def test_external_image_observation_requires_digest_pinning_and_running_image_match():
    digest = "sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    observation = normalize_external_image_observation({
        "schema": "archbro.release.image_observation.v1",
        "image_reference": "registry.example/archbro@" + digest,
        "manifest_digest": digest,
        "image_id": image_id,
        "running_image_id": image_id,
    })
    assert observation["manifest_digest"] == digest
    with pytest.raises(ContractError, match="running image"):
        normalize_external_image_observation({
            **observation,
            "running_image_id": "sha256:" + "c" * 64,
        })


def test_external_image_observation_reads_image_and_running_container_identity():
    digest = "sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    calls: list[list[str]] = []

    def runner(args, **_kwargs):
        calls.append(args)
        stdout = image_id + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    observation = observe_docker_image_identity(
        image_reference="registry.example/archbro@" + digest,
        container="archbro-candidate",
        runner=runner,
    )
    assert observation["manifest_digest"] == digest
    assert observation["image_id"] == image_id
    assert observation["running_image_id"] == image_id
    assert calls == [
        ["docker", "image", "inspect", "--format", "{{.Id}}", "registry.example/archbro@" + digest],
        ["docker", "inspect", "--format", "{{.Image}}", "archbro-candidate"],
    ]


def test_dependency_manifest_must_match_actual_installed_distributions():
    manifest = {
        "schema": DEPENDENCY_MANIFEST_SCHEMA,
        "packages": [{"name": "fastapi", "version": "1.0"}],
    }
    matching = [SimpleNamespace(metadata={"Name": "fastapi"}, version="1.0")]
    changed = [SimpleNamespace(metadata={"Name": "fastapi"}, version="2.0")]
    assert verify_dependency_manifest(manifest, distributions=matching)["matches"] is True
    assert verify_dependency_manifest(manifest, distributions=changed)["matches"] is False


def test_runtime_dependency_mismatch_never_exports_release_fingerprint(tmp_path):
    app, _source, release, _manifest, _metadata, env = _release(tmp_path)
    changed = [SimpleNamespace(metadata={"Name": "unexpected"}, version="1.0")]
    identity = build_runtime_identity(
        release_dir=release,
        app_root=app,
        environ=env,
        distributions=changed,
    )
    assert identity["dependency_fingerprint"] is None
    assert identity["runtime"]["dependency_manifest_matches"] is False


def test_runtime_image_or_harness_declaration_cannot_override_external_authority(tmp_path):
    for relative in (
        "qa/verify_archbro_release.py",
        "qa/archbro_release_evidence.py",
        "qa/playwright_release_acceptance.py",
        "qa/archbro_release_runtime.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")
    harness_hash = acceptance_harness_sha256(tmp_path)
    digest = "sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    external = {
        "schema": "archbro.release.image_observation.v1",
        "image_reference": "registry.example/archbro@" + digest,
        "manifest_digest": digest,
        "image_id": image_id,
        "running_image_id": image_id,
    }
    runtime = {
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "dependency_fingerprint": "e" * 64,
        "safe_config_fingerprint": "f" * 64,
        "target_origin": "https://archbro.example.test",
        "source": {
            "git_sha": "a" * 40,
            "git_tree": "b" * 40,
            "manifest_matches": True,
            "manifest_status": "verified",
            "manifest_sha256": "9" * 64,
            "observed_manifest_sha256": "9" * 64,
        },
        "runtime": {
            "dependency_manifest_matches": True,
            "dependency_manifest_status": "verified",
            "expected_dependency_manifest_sha256": "e" * 64,
            "observed_dependency_manifest_sha256": "e" * 64,
        },
        "image": {"manifest_digest": digest},
        "harness": {"sha256": harness_hash},
    }
    observed = authoritative_observed_identity(
        runtime,
        external_image_observation=external,
        harness_root=tmp_path,
    )
    assert observed["image_digest"] == digest
    assert observed["harness_hash"] == harness_hash
    with pytest.raises(ContractError, match="runtime image declaration"):
        authoritative_observed_identity(
            {**runtime, "image": {"manifest_digest": "sha256:" + "c" * 64}},
            external_image_observation=external,
            harness_root=tmp_path,
        )
    with pytest.raises(ContractError, match="runtime harness declaration"):
        authoritative_observed_identity(
            {**runtime, "harness": {"sha256": "0" * 64}},
            external_image_observation=external,
            harness_root=tmp_path,
        )


def test_database_schema_readiness_uses_one_bounded_query():
    calls = []

    class Connection(_Connection):
        def execute(self, query, params=None):
            calls.append((query, params))
            relations = list(params[0])
            rows = [{"relation": relation, "resolved": relation} for relation in relations]
            return SimpleNamespace(fetchall=lambda: rows)

    result = database_readiness(SimpleNamespace(_connect=Connection))
    assert result["status"] == "ready"
    assert len(calls) == 1
    assert "unnest" in calls[0][0]


def test_public_readyz_does_not_touch_database_but_internal_readyz_does(monkeypatch):
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")
    monkeypatch.setenv("ARCHBRO_PERSISTENCE", "postgres")

    class Repository:
        def __init__(self):
            self.connect_calls = 0

        def _connect(self):
            self.connect_calls += 1
            return _Connection()

    repository = Repository()
    app = create_app(repository=repository, provider=FakeModelProvider())
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/readyz").status_code == 200
    assert repository.connect_calls == 0
    assert client.get("/internal/readyz").status_code == 503
    assert repository.connect_calls == 1


def test_internal_readyz_rejects_non_loopback_client(monkeypatch):
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")
    monkeypatch.setenv("ARCHBRO_PERSISTENCE", "postgres")
    app = create_app(repository=SimpleNamespace(_connect=lambda: _Connection()), provider=FakeModelProvider())

    async def scenario():
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/internal/readyz")
            assert response.status_code == 403

    import asyncio
    asyncio.run(scenario())


def _release(tmp_path):
    app = tmp_path / "app"
    source = app / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()
    manifest = {"schema": SOURCE_MANIFEST_SCHEMA, "files": [
        {"path": "src/example.py", "sha256": file_sha256(source)}
    ]}
    metadata = {"schema": RELEASE_METADATA_SCHEMA, "source": {
        "git_sha": "a" * 40, "git_tree": "b" * 40,
    }, "harness": {"sha256": "d" * 64}}
    for name, value in (("source-manifest.json", manifest), ("release.json", metadata),
                        ("dependencies.json", {"schema": DEPENDENCY_MANIFEST_SCHEMA, "packages": []})):
        (release / name).write_text(json.dumps(value), encoding="utf-8")
    env = {"ARCHBRO_IMAGE_MANIFEST_DIGEST": "sha256:" + "c" * 64,
           "ARCHBRO_ACCEPTANCE_TARGET_ORIGIN": "http://127.0.0.1:8013"}
    return app, source, release, manifest, metadata, env


@pytest.mark.parametrize("failure", [
    "tampered", "missing", "empty", "duplicate", "unlisted",
    "wrong_metadata_schema", "invalid_git_sha",
])
def test_unverified_source_never_exports_observed_git_identity(tmp_path, failure):
    app, source, release, manifest, metadata, env = _release(tmp_path)
    if failure == "tampered":
        source.write_text("VALUE = 2\n", encoding="utf-8")
    elif failure == "missing":
        source.unlink()
    elif failure == "empty":
        manifest["files"] = []
    elif failure == "duplicate":
        manifest["files"] *= 2
    elif failure == "unlisted":
        source.with_name("unexpected.py").write_text("VALUE = 3\n", encoding="utf-8")
    elif failure == "wrong_metadata_schema":
        metadata["schema"] = "unrecognized"
    elif failure == "invalid_git_sha":
        metadata["source"]["git_sha"] = "a" * 41
    (release / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (release / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
    identity = build_runtime_identity(release_dir=release, app_root=app, environ=env, distributions=[])
    assert identity["source_sha"] is None
    assert identity["source_tree"] is None
    assert identity["source"]["git_sha"] is None


def test_empty_manifest_is_not_verification(tmp_path):
    result = verify_source_manifest({"schema": SOURCE_MANIFEST_SCHEMA, "files": []}, app_root=tmp_path)
    assert result["matches"] is not True


@pytest.mark.parametrize("path", ["../outside.py", "..\\outside.py", "C:/outside.py",
                                   "/outside.py", "src/../example.py", "src/example.py:stream"])
def test_manifest_rejects_noncanonical_or_escaping_paths(tmp_path, path):
    result = verify_source_manifest({"schema": SOURCE_MANIFEST_SCHEMA, "files": [
        {"path": path, "sha256": "a" * 64}
    ]}, app_root=tmp_path)
    assert result["matches"] is not True


def test_file_read_error_fails_closed_without_exposing_message(tmp_path, monkeypatch):
    app, source, release, manifest, metadata, env = _release(tmp_path)
    def denied(path):
        raise PermissionError("private-path-must-not-leak")
    monkeypatch.setattr("archbro.platform.runtime.release_identity.file_sha256", denied)
    identity = build_runtime_identity(release_dir=release, app_root=app, environ=env, distributions=[])
    assert identity["source_sha"] is None
    assert "private-path-must-not-leak" not in json.dumps(identity)


@pytest.mark.parametrize("key", [
    "GEMINI_GOAL_MODEL", "GEMINI_ROUTINE_MODEL", "GEMINI_SYSTEM_MAP_MODEL",
    "GEMINI_FALLBACK_MODEL", "GEMINI_FALLBACK_MODELS", "GEMINI_GOAL_FALLBACK_MODELS",
    "GEMINI_ROUTINE_FALLBACK_MODELS", "GEMINI_BOOTSTRAP_FALLBACK_MODELS",
    "GEMINI_GOAL_MODEL_TIMEOUT_SECONDS", "GEMINI_ROUTINE_MODEL_TIMEOUT_SECONDS",
    "GEMINI_ARCHITECTURE_MODEL_TIMEOUT_SECONDS", "GEMINI_ARCHITECTURE_PHASE_TIMEOUT_SECONDS",
    "GEMINI_ARCHITECTURE_TOTAL_TIMEOUT_SECONDS", "GEMINI_ARCHITECTURE_MAX_OUTPUT_TOKENS",
    "GEMINI_ARCHITECTURE_THINKING_LEVEL",
    "GEMINI_SYSTEM_MAP_MAX_OUTPUT_TOKENS", "GEMINI_SCOPE_MAX_OUTPUT_TOKENS",
    "GEMINI_RECONCILE_MAX_OUTPUT_TOKENS", "GEMINI_SYSTEM_MAP_THINKING_LEVEL",
    "GEMINI_SCOPE_THINKING_LEVEL", "GEMINI_RECONCILE_THINKING_LEVEL",
    "GEMINI_ARCHITECTURE_RETRY_ATTEMPTS",
    "GEMINI_RETRY_INITIAL_DELAY_SECONDS", "GEMINI_RETRY_MAX_DELAY_SECONDS",
    "GEMINI_RETRY_EXP_BASE", "GEMINI_RETRY_JITTER",
    "GEMINI_ARCHITECTURE_MAX_CONCURRENCY", "GEMINI_ARCHITECTURE_QUEUE_TIMEOUT_SECONDS",
    "GEMINI_HTTP_TIMEOUT_MS",
    "GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS",
])
def test_behavior_changing_safe_settings_invalidate_fingerprint(key):
    assert safe_config_fingerprint({key: "first"}) != safe_config_fingerprint({key: "second"})


def test_secret_rotation_does_not_enter_public_fingerprint():
    secrets = ("DATABASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ARCHBRO_EDGE_TOKEN")
    first = {key: "private-first" for key in secrets}
    second = {key: "private-second" for key in secrets}
    assert safe_config_fingerprint(first) == safe_config_fingerprint(second)
    assert "private-first" not in json.dumps(safe_runtime_config(first))


@pytest.mark.parametrize("failure", ["missing_proof", "false_match", "string_match",
                                       "wrong_status", "different_hash", "different_git_sha"])
def test_matching_flat_labels_cannot_override_source_provenance(tmp_path, failure):
    app, source, release, manifest, metadata, env = _release(tmp_path)
    identity = build_runtime_identity(release_dir=release, app_root=app, environ=env, distributions=[])
    fields = ("source_sha", "source_tree", "image_digest", "harness_hash",
              "dependency_fingerprint", "safe_config_fingerprint", "target_origin")
    unit = {"schema": "archbro.release_unit.v1",
            "expected_identity": {key: identity[key] for key in fields}}
    assert compare_release_unit_identity(unit, identity)["status"] == "PASS"
    if failure == "missing_proof":
        identity.pop("source")
    elif failure == "false_match":
        identity["source"]["manifest_matches"] = False
    elif failure == "string_match":
        identity["source"]["manifest_matches"] = "true"
    elif failure == "wrong_status":
        identity["source"]["manifest_status"] = "unknown"
    elif failure == "different_hash":
        identity["source"]["observed_manifest_sha256"] = "0" * 64
    elif failure == "different_git_sha":
        identity["source"]["git_sha"] = "0" * 40
    result = compare_release_unit_identity(unit, identity)
    assert result["status"] == "FAIL"
    assert result["fixture_admission"] == "BLOCKED"


def test_browser_acceptance_runner_never_self_declares_release_pass_before_verifier():
    root = Path(__file__).resolve().parents[1]
    source = (root / "qa" / "playwright_release_acceptance.py").read_text(encoding="utf-8")

    assert "RELEASE_ACCEPTANCE_PASS" not in source
    assert 'report["result"] = "PASS"' not in source
    assert "cleanup_owned_fixtures(" in source
    assert "RELEASE_ACCEPTANCE_EVIDENCE_READY" in source
    assert "run qa/verify_archbro_release.py for the sole PASS decision" in source
