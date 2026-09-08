import hashlib
from pathlib import Path
import re
from typing import cast

import pytest
from fastapi.testclient import TestClient

from archbro.backend.core.authorization import TrustedPrincipal
from archbro.backend.core.repository import ProjectRepositoryPort
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.runtime.app import create_app


def _repository_not_used_by_runtime_config() -> ProjectRepositoryPort:
    return cast(ProjectRepositoryPort, object())


def test_firebase_popup_csp_includes_the_validated_custom_auth_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "firebase")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "archbro-test-project")
    monkeypatch.setenv("ARCHBRO_FIREBASE_API_KEY", "test-browser-key")
    monkeypatch.setenv(
        "ARCHBRO_FIREBASE_AUTH_DOMAIN",
        "login.example.archbro.invalid",
    )

    async def principal_provider(_: str) -> TrustedPrincipal:
        return TrustedPrincipal(user_id="firebase-uid-alice")

    client = TestClient(
        create_app(
            _repository_not_used_by_runtime_config(),
            FakeModelProvider(),
            principal_provider=principal_provider,
        )
    )
    policy = client.get("/runtime-config.js").headers["content-security-policy"]

    assert "script-src 'self' https://www.gstatic.com https://apis.google.com" in policy
    assert (
        "frame-src https://*.firebaseapp.com "
        "https://login.example.archbro.invalid"
    ) in policy


@pytest.mark.parametrize(
    "invalid_auth_domain",
    [
        "https://project.firebaseapp.com",
        "project.firebaseapp.com/path",
        "project.firebaseapp.com:443",
        "project.firebaseapp.com; script-src *",
    ],
)
def test_firebase_popup_csp_rejects_unsafe_auth_domains(
    monkeypatch: pytest.MonkeyPatch,
    invalid_auth_domain: str,
) -> None:
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "firebase")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "archbro-test-project")
    monkeypatch.setenv("ARCHBRO_FIREBASE_API_KEY", "test-browser-key")
    monkeypatch.setenv("ARCHBRO_FIREBASE_AUTH_DOMAIN", invalid_auth_domain)

    async def principal_provider(_: str) -> TrustedPrincipal:
        return TrustedPrincipal(user_id="firebase-uid-alice")

    with pytest.raises(ValueError, match="must be a hostname"):
        create_app(
            _repository_not_used_by_runtime_config(),
            FakeModelProvider(),
            principal_provider=principal_provider,
        )


def test_local_mode_does_not_trust_popup_only_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")

    client = TestClient(
        create_app(
            _repository_not_used_by_runtime_config(),
            FakeModelProvider(),
        )
    )
    policy = client.get("/runtime-config.js").headers["content-security-policy"]

    assert "https://apis.google.com" not in policy
    assert "frame-src 'none'" in policy


def test_static_assets_require_exact_content_version_for_immutable_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")

    client = TestClient(
        create_app(
            _repository_not_used_by_runtime_config(),
            FakeModelProvider(),
        )
    )
    asset_path = Path(__file__).resolve().parents[1] / "frontend" / "web" / "app.js"
    current_version = hashlib.sha256(asset_path.read_bytes()).hexdigest()[:16]
    index = client.get("/")
    versioned = client.get(f"/static/app.js?v={current_version}")
    wrong = client.get("/static/app.js?v=" + "0" * 64)
    unversioned = client.get("/static/app.js")

    assert f'/static/app.js?v={current_version}' in index.text
    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert wrong.status_code == 200
    assert wrong.headers["cache-control"] == "no-store, max-age=0"
    assert unversioned.status_code == 200
    assert unversioned.headers["cache-control"] == "no-store, max-age=0"


@pytest.mark.parametrize("asset", ["/static/app.js", "/runtime-config.js"])
def test_versioned_asset_authorization_errors_are_never_immutable(monkeypatch, asset):
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")
    monkeypatch.setenv("ARCHBRO_EDGE_GUARD", "required")
    monkeypatch.setenv("ARCHBRO_EDGE_TOKEN", "disposable-cache-test-token")
    with TestClient(create_app(_repository_not_used_by_runtime_config(), FakeModelProvider())) as client:
        index = client.get("/", headers={"X-ArchBro-Edge-Token": "disposable-cache-test-token"})
        assert index.status_code == 200
        match = re.search(re.escape(asset) + r'\?v=([0-9a-f]{16})', index.text)
        assert match is not None
        denied = client.get(match.group(0))
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store, max-age=0"


def test_runtime_config_exact_content_version_is_immutable_but_unversioned_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")
    client = TestClient(
        create_app(
            _repository_not_used_by_runtime_config(),
            FakeModelProvider(),
        )
    )

    index = client.get("/")
    match = re.search(r'src="(/runtime-config\.js\?v=([0-9a-f]{16}))"', index.text)
    assert match is not None
    versioned = client.get(match.group(1))
    unversioned = client.get("/runtime-config.js")
    wrong = client.get("/runtime-config.js?v=0000000000000000")

    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert unversioned.headers["cache-control"] == "no-store, max-age=0"
    assert wrong.headers["cache-control"] == "no-store, max-age=0"

    monkeypatch.setenv("ARCHBRO_MCP_SERVERS_JSON", '[{"id":"github"}]')
    changed_index = client.get("/")
    changed = re.search(r'/runtime-config\.js\?v=([0-9a-f]{16})', changed_index.text)
    assert changed is not None
    assert changed.group(1) != match.group(2)
