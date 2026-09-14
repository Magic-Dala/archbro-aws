from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.runtime.app import create_app
from archbro.platform.runtime.release_identity import (
    DEPENDENCY_MANIFEST_SCHEMA,
    SOURCE_MANIFEST_SCHEMA,
    build_runtime_identity,
    canonical_sha256,
    safe_runtime_config,
)
from qa.archbro_release_runtime import (
    ContractError,
    FIXED_ACCEPTANCE_ORIGIN,
    ROLE11_RELEASE_UNIT_SCHEMA,
    authorize_service_switch,
    acceptance_harness_sha256,
    build_command,
    compare_release_unit_identity,
    inspect_target,
    normalize_preflight,
    rollback_contract,
    runner_plan,
    validate_preflight,
    validate_target_origin,
)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._row if isinstance(self._row, list) else [self._row]


class _ProbeConnection:
    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        if "to_regclass" in query and "unnest" in query:
            relations = list(params[0])
            return _Result([
                {
                    "relation": relation,
                    "resolved": None if relation in self.missing else relation,
                }
                for relation in relations
            ])
        raise AssertionError(f"unexpected readiness query: {query}")


class _ProbeRepository:
    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()

    def _connect(self):
        return _ProbeConnection(self.missing)


def _runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")
    monkeypatch.setenv("ARCHBRO_PERSISTENCE", "postgres")
    monkeypatch.setenv("ARCHBRO_EDGE_GUARD", "off")


def _role11_expected_identity() -> dict[str, str]:
    return {
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "harness_hash": "d" * 64,
        "dependency_fingerprint": "e" * 64,
        "safe_config_fingerprint": "f" * 64,
        "target_origin": FIXED_ACCEPTANCE_ORIGIN,
    }


def _runtime_observation(expected=None):
    observed = dict(expected or _role11_expected_identity())
    observed["source"] = {
        "git_sha": observed["source_sha"], "git_tree": observed["source_tree"],
        "manifest_matches": True, "manifest_status": "verified",
        "manifest_sha256": "9" * 64, "observed_manifest_sha256": "9" * 64,
    }
    return observed


def _role11_release_unit() -> dict[str, object]:
    return {
        "schema": ROLE11_RELEASE_UNIT_SCHEMA,
        "expected_identity": _role11_expected_identity(),
    }


def test_safe_runtime_identity_never_contains_secret_values(tmp_path: Path) -> None:
    environ = {
        "ARCHBRO_ENV": "production",
        "ARCHBRO_AUTH_MODE": "firebase",
        "ARCHBRO_PERSISTENCE": "postgres",
        "FIREBASE_PROJECT_ID": "archbro-example",
        "ARCHBRO_FIREBASE_AUTH_DOMAIN": "archbro-example.firebaseapp.com",
        "ARCHBRO_FIREBASE_API_KEY": "browser-key-must-not-be-copied",
        "DATABASE_URL": "postgresql://<redacted>@db.example/archbro",
        "ARCHBRO_EDGE_TOKEN": "edge-secret",
        "ARCHBRO_MCP_SERVERS_JSON": '{"token":"mcp-secret"}',
    }

    config = safe_runtime_config(environ)
    serialized = json.dumps(config, sort_keys=True)

    assert "browser-key-must-not-be-copied" not in serialized
    assert "postgresql://" not in serialized
    assert "edge-secret" not in serialized
    assert "mcp-secret" not in serialized
    assert config["firebase"]["api_key_present"] is True
    assert config["connected_mcp_config_present"] is True


def test_runtime_identity_proves_baked_file_manifest_and_preserves_unknown_image(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    source = app_root / "src" / "archbro" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    release_dir = tmp_path / "release"
    release_dir.mkdir()

    source_manifest = {
        "schema": SOURCE_MANIFEST_SCHEMA,
        "files": [
            {
                "path": "src/archbro/example.py",
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        ],
    }
    dependencies = {
        "schema": DEPENDENCY_MANIFEST_SCHEMA,
        "packages": [{"name": "fastapi", "version": "0.test"}],
    }
    (release_dir / "source-manifest.json").write_text(
        json.dumps(source_manifest), encoding="utf-8"
    )
    (release_dir / "dependencies.json").write_text(
        json.dumps(dependencies), encoding="utf-8"
    )
    (release_dir / "release.json").write_text(
        json.dumps(
            {
                "schema": "archbro.release.metadata.v1",
                "source": {
                    "git_sha": "a" * 40,
                    "git_tree": "b" * 40,
                },
                "base": {
                    "reference": "python:3.13-slim@sha256:" + "c" * 64,
                    "manifest_digest": "sha256:" + "c" * 64,
                },
                "harness": {"sha256": "d" * 64},
            }
        ),
        encoding="utf-8",
    )

    identity = build_runtime_identity(
        release_dir=release_dir,
        app_root=app_root,
        environ={"ARCHBRO_AUTH_MODE": "local"},
        distributions=[SimpleNamespace(metadata={"Name": "fastapi"}, version="0.test")],
    )

    assert identity["source"]["manifest_matches"] is True
    assert identity["source"]["git_sha"] == "a" * 40
    assert identity["runtime"]["dependency_manifest_sha256"] == canonical_sha256(
        dependencies
    )
    assert identity["image"]["id"] is None
    assert identity["image"]["manifest_digest"] is None
    # Role-11 observed identity stays top-level and missing external
    # observations remain unknown instead of being copied from metadata.
    assert identity["source_sha"] == "a" * 40
    assert identity["source_tree"] == "b" * 40
    assert identity["harness_hash"] == "d" * 64
    assert identity["dependency_fingerprint"] == canonical_sha256(dependencies)
    assert identity["image_digest"] is None
    assert identity["target_origin"] is None

    source.write_text("VALUE = 2\n", encoding="utf-8")
    changed = build_runtime_identity(
        release_dir=release_dir,
        app_root=app_root,
        environ={"ARCHBRO_AUTH_MODE": "local"},
        distributions=[SimpleNamespace(metadata={"Name": "fastapi"}, version="0.test")],
    )
    assert changed["source"]["manifest_matches"] is False
    assert changed["source_sha"] is None
    assert changed["source_tree"] is None


def test_runtime_identity_emits_exact_role11_observed_fields_when_measured(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    source = app_root / "src" / "archbro" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    release_dir = tmp_path / "release"
    release_dir.mkdir()

    source_sha = "a" * 40
    source_tree = "b" * 40
    harness_hash = "d" * 64
    image_digest = "sha256:" + "c" * 64
    source_manifest = {
        "schema": SOURCE_MANIFEST_SCHEMA,
        "files": [
            {
                "path": "src/archbro/example.py",
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        ],
    }
    dependencies = {
        "schema": DEPENDENCY_MANIFEST_SCHEMA,
        "packages": [{"name": "fastapi", "version": "0.test"}],
    }
    for name, value in (
        ("source-manifest.json", source_manifest),
        ("dependencies.json", dependencies),
        (
            "release.json",
            {
                "schema": "archbro.release.metadata.v1",
                "source": {"git_sha": source_sha, "git_tree": source_tree},
                "base": {
                    "reference": "python:3.13-slim@sha256:" + "9" * 64,
                    "manifest_digest": "sha256:" + "9" * 64,
                },
                "harness": {"sha256": harness_hash},
            },
        ),
    ):
        (release_dir / name).write_text(json.dumps(value), encoding="utf-8")

    environ = {
        "ARCHBRO_AUTH_MODE": "local",
        "ARCHBRO_IMAGE_MANIFEST_DIGEST": image_digest,
        "ARCHBRO_ACCEPTANCE_TARGET_ORIGIN": FIXED_ACCEPTANCE_ORIGIN,
    }
    identity = build_runtime_identity(
        release_dir=release_dir,
        app_root=app_root,
        environ=environ,
        distributions=[SimpleNamespace(metadata={"Name": "fastapi"}, version="0.test")],
    )

    assert identity["source_sha"] == source_sha
    assert identity["source_tree"] == source_tree
    assert identity["image_digest"] == image_digest
    assert identity["harness_hash"] == harness_hash
    assert identity["dependency_fingerprint"] == canonical_sha256(dependencies)
    assert identity["safe_config_fingerprint"]
    assert identity["target_origin"] == FIXED_ACCEPTANCE_ORIGIN


def test_readyz_is_independent_from_healthz_and_never_calls_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_env(monkeypatch)

    class _ExplodingProvider:
        def generate(self, *args, **kwargs):
            raise AssertionError("readiness must never call a model provider")

    app = create_app(
        repository=_ProbeRepository(),
        provider=_ExplodingProvider(),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ready"
    assert payload["provider_probe"] == "not_run"


@pytest.mark.parametrize("missing_relation", ["public.projects", "public.planner_checkpoints"])
def test_readyz_fails_closed_when_database_schema_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    missing_relation: str,
) -> None:
    _runtime_env(monkeypatch)
    app = create_app(
        repository=_ProbeRepository({missing_relation}),
        provider=FakeModelProvider(),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    assert client.get("/healthz").status_code == 200
    readiness = client.get("/internal/readyz")
    assert readiness.status_code == 503
    assert readiness.json()["checks"]["database"]["schema"] == "missing"
    assert missing_relation in readiness.json()["checks"]["database"][
        "missing_relations"
    ]


def test_probe_endpoints_are_non_secret_and_bypass_edge_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_env(monkeypatch)
    monkeypatch.setenv("ARCHBRO_EDGE_GUARD", "required")
    monkeypatch.setenv("ARCHBRO_EDGE_TOKEN", "edge-secret-that-must-not-leak")
    app = create_app(
        repository=_ProbeRepository(),
        provider=FakeModelProvider(),
    )
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    identity = client.get("/runtime-identity")
    assert identity.status_code == 200
    assert "edge-secret-that-must-not-leak" not in identity.text
    assert client.get("/").status_code == 403


def test_preflight_preserves_unknown_values_instead_of_substituting_expected() -> None:
    normalized = normalize_preflight(
        {
            "schema": "archbro.release.preflight.v1",
            "source": {"git_sha": None},
            "target": {"origin": FIXED_ACCEPTANCE_ORIGIN},
        }
    )

    assert normalized["source"]["git_sha"] is None
    assert normalized["image"]["id"] is None
    assert normalized["base"]["manifest_digest"] is None
    assert runner_plan(normalized)["service_switch_default"] == "refused"


def test_role11_release_unit_preflight_keeps_expectation_separate_from_observation() -> None:
    release_unit = _role11_release_unit()
    normalized = normalize_preflight(release_unit)

    assert normalized["contract_source"] == ROLE11_RELEASE_UNIT_SCHEMA
    assert normalized["expected_identity"] == _role11_expected_identity()
    # Never convert an expected source/image value into an observed role-12 value.
    assert normalized["source"]["git_sha"] is None
    assert normalized["image"]["manifest_digest"] is None
    assert normalized["target"]["origin"] is None


def test_role11_identity_comparison_blocks_unknown_or_mismatch_without_substitution() -> None:
    release_unit = _role11_release_unit()
    observed = _runtime_observation()

    passed = compare_release_unit_identity(release_unit, observed)
    assert passed["status"] == "PASS"
    assert passed["fixture_admission"] == "ALLOWED"

    missing = dict(observed)
    missing["image_digest"] = None
    blocked_unknown = compare_release_unit_identity(release_unit, missing)
    assert blocked_unknown["status"] == "FAIL"
    assert blocked_unknown["fixture_admission"] == "BLOCKED"
    assert blocked_unknown["unknown_observed"] == ["image_digest"]
    assert blocked_unknown["observed"]["image_digest"] is None

    mismatched = dict(observed)
    mismatched["dependency_fingerprint"] = "0" * 64
    blocked_mismatch = compare_release_unit_identity(release_unit, mismatched)
    assert blocked_mismatch["status"] == "FAIL"
    assert "dependency_fingerprint" in blocked_mismatch["mismatches"]


def test_preflight_rejects_secret_bearing_fields() -> None:
    with pytest.raises(ContractError, match="forbidden secret-bearing field"):
        normalize_preflight(
            {
                "schema": "archbro.release.preflight.v1",
                "database_url": "postgresql://secret",
            }
        )


def test_build_requires_resolved_base_digest_and_is_not_a_service_switch() -> None:
    with pytest.raises(ContractError, match="pinned with @sha256"):
        build_command(
            base_image="python:3.13-slim",
            source_sha="a" * 40,
            source_tree="b" * 40,
            harness_sha256="c" * 64,
            image_tag="archbro:candidate",
        )

    harness_hash = acceptance_harness_sha256()
    command = build_command(
        base_image="python:3.13-slim@sha256:" + "d" * 64,
        source_sha="a" * 40,
        source_tree="b" * 40,
        harness_sha256=harness_hash,
        image_tag="archbro:candidate",
    )
    assert command[:2] == ["docker", "build"]
    assert "compose" not in command
    assert "run" not in command
    harness_index = command.index("--build-arg", command.index("--build-arg") + 1)
    assert any(item == f"ARCHBRO_HARNESS_SHA256={harness_hash}" for item in command)


def test_rollback_stays_dry_run_until_explicit_15d_gates() -> None:
    snapshot = {
        "owned_route_identity": "archbro-jim-owned-route",
        "image_id": "sha256:" + "a" * 64,
        "image_manifest_digest": "sha256:" + "b" * 64,
        "safe_config_fingerprint": "c" * 64,
    }
    gates = {
        "explicit_user_launch": True,
        "acceptance_14v_pass": True,
        "threaden_promotion_admitted": True,
        "exact_cas_ready": True,
    }

    assert authorize_service_switch(phase="12", gates=gates) is False
    assert rollback_contract(snapshot)["authorized_to_execute"] is False
    authorized = rollback_contract(snapshot, phase="15D", gates=gates)
    assert authorized["authorized_to_execute"] is True
    assert authorized["git_history_rewrite"] is False


def test_candidate_dockerfile_has_no_floating_default_base() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "release"
        / "Dockerfile.candidate"
    ).read_text(encoding="utf-8")

    assert "ARG BASE_IMAGE=" not in dockerfile
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "source-manifest.json" in dockerfile
    assert "dependencies.json" in dockerfile
    assert "ARCHBRO_SOURCE_SHA" in dockerfile


def test_public_origin_is_frozen_without_becoming_an_observation() -> None:
    unit = _role11_release_unit()
    unit["expected_identity"]["target_origin"] = "https://archbro-jim.magicdala.com"
    normalized = normalize_preflight(unit)
    assert normalized["target"]["origin"] is None
    assert runner_plan(unit)["fixed_target"]["origin"] == "https://archbro-jim.magicdala.com"
    assert runner_plan(unit)["dependencies"]["candidate_app_port"] is None
    assert validate_preflight(unit)["valid"] is True
    observed = _runtime_observation(unit["expected_identity"])
    assert compare_release_unit_identity(unit, observed)["status"] == "PASS"
    observed["target_origin"] = "http://127.0.0.1:8014"
    assert compare_release_unit_identity(unit, observed)["status"] == "FAIL"


@pytest.mark.parametrize("origin", [
    "https://example.com/path", "https://example.com/", "https://example.com?x=1",
    "https://example.com#fragment", "https://user:secret@example.com",
    "http://example.com", "file:///tmp/app", "https://example.com:99999",
    "https://example.com\\path", "https://example.com\n",
    None, 8013,
])
def test_release_origin_rejects_non_origin_values(origin: str) -> None:
    with pytest.raises(ContractError):
        validate_target_origin(origin)


def test_inspection_rejects_wrong_frozen_origin_before_network(monkeypatch) -> None:
    def unexpected_request(*args, **kwargs):
        pytest.fail("A mismatched release target must not make a request")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_request)
    with pytest.raises(ContractError, match="frozen release unit"):
        inspect_target("https://archbro-jim.magicdala.com", release_unit=_role11_release_unit())


@pytest.mark.parametrize("observed_origin", ["https://archbro-jim.magicdala.com", None, FIXED_ACCEPTANCE_ORIGIN])
def test_inspection_checks_actual_public_identity(monkeypatch, observed_origin) -> None:
    origin = "https://archbro-jim.magicdala.com"
    unit = _role11_release_unit()
    unit["expected_identity"]["target_origin"] = origin
    identity = _runtime_observation(unit["expected_identity"])
    identity["target_origin"] = observed_origin
    requested = []

    def response(request, **kwargs):
        requested.append(request.full_url)
        body = identity if request.full_url.endswith("/runtime-identity") else {"status": "ready"}
        result = BytesIO(json.dumps(body).encode())
        result.status = 200
        result.geturl = lambda: request.full_url
        return result

    monkeypatch.setattr("urllib.request.urlopen", response)
    result = inspect_target(origin, release_unit=unit)
    assert requested == [origin + "/runtime-identity", origin + "/readyz"]
    assert result["matches"] is (observed_origin == origin)
    assert result["identity"]["target_origin"] == observed_origin


def test_inspection_rejects_cross_origin_redirect(monkeypatch) -> None:
    response = BytesIO(b"{}")
    response.status = 200
    response.geturl = lambda: "https://other.example/runtime-identity"
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: response)
    with pytest.raises(ContractError, match="different origin"):
        inspect_target("https://archbro-jim.magicdala.com")


def test_direct_promotion_admission_preserves_14v_and_exact_cas_gates() -> None:
    gates = {"explicit_user_launch": True, "acceptance_14v_pass": True,
             "promotion_admitted": True, "exact_cas_ready": True}
    assert authorize_service_switch(phase="15D", gates=gates) is True
    for key in gates:
        assert authorize_service_switch(phase="15D", gates={**gates, key: False}) is False
    assert authorize_service_switch(phase="14V", gates=gates) is False
    assert authorize_service_switch(phase="15D", gates={
        **gates, "promotion_admitted": False, "threaden_promotion_admitted": True,
    }) is False
