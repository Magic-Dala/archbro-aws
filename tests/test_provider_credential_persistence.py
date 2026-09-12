from __future__ import annotations

import base64
import json
import time
from dataclasses import replace

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
import pytest

from archbro.backend.api.provider_connections import (
    ProviderMcpRuntimeRegistry,
    build_provider_mcp_router,
)
from archbro.backend.core.authorization import TrustedPrincipal
from archbro.backend.mcp.provider_credentials import StoredProviderCredential
from archbro.backend.mcp.provider_gateway import ExternalMcpGateway
from archbro.backend.mcp.provider_oauth import McpOAuthManager
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.persistence.postgres import PostgresProjectRepository
from archbro.platform.persistence.provider_credentials import PostgresProviderCredentialStore
from archbro.platform.runtime.app import build_app
from conftest import requires_database


TEST_PROVIDER_CREDENTIAL_KEY = base64.urlsafe_b64encode(bytes(32)).decode("ascii")


class MemoryCredentialStore:
    persistent = True

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], StoredProviderCredential] = {}
        self.deletes: list[tuple[str, str]] = []
        self.fail_delete = False

    def providers_for_user(self, user_id: str) -> list[str]:
        return sorted(
            provider
            for owner, provider in self.rows
            if owner == user_id
        )

    def get(self, user_id: str, provider: str) -> StoredProviderCredential:
        try:
            return self.rows[(user_id, provider)]
        except KeyError:
            raise KeyError((user_id, provider)) from None

    def list_for_user(self, user_id: str) -> list[StoredProviderCredential]:
        return [self.get(user_id, provider) for provider in self.providers_for_user(user_id)]

    def upsert(self, user_id: str, credential: StoredProviderCredential) -> None:
        self.rows[(user_id, credential.provider)] = credential

    def delete(self, user_id: str, provider: str) -> None:
        self.deletes.append((user_id, provider))
        if self.fail_delete:
            raise RuntimeError("database unavailable")
        self.rows.pop((user_id, provider), None)


def _add_github(
    gateway: ExternalMcpGateway,
    *,
    access_token: str,
    refresh_token: str | None = "refresh-token",
    expires_in: int | None = 3600,
    persist: bool,
    replace_existing: bool = True,
) -> dict:
    return gateway.add_oauth_connection(
        provider="github",
        name="GitHub",
        url="https://api.githubcopilot.com/mcp/",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        token_url="https://github.com/login/oauth/access_token",
        client_id="github-client",
        client_secret="github-secret",
        persist=persist,
        replace_existing=replace_existing,
    )


def test_verified_oauth_connection_is_restored_for_same_user_only(monkeypatch):
    store = MemoryCredentialStore()
    gateway = ExternalMcpGateway(credential_owner="alice", credential_store=store)
    connection = _add_github(
        gateway,
        access_token="alice-access-token",
        persist=False,
        replace_existing=False,
    )
    monkeypatch.setattr(
        gateway,
        "_state_list_tools",
        lambda _state: [{"name": "get_file_contents", "annotations": {"readOnlyHint": True}}],
    )

    probed = gateway.probe(connection["id"])
    committed = gateway.commit_oauth_connection(connection["id"])

    assert probed["tool_count"] == 1
    assert committed["persistent"] is True
    stored = store.rows[("alice", "github")]
    assert stored.connection_id == connection["id"]
    assert stored.access_token == "alice-access-token"
    assert stored.tool_count == 1

    restarted = ProviderMcpRuntimeRegistry(store)
    restored_gateway, _ = restarted.runtime_for(TrustedPrincipal(user_id="alice"))
    restored = restored_gateway.list_connections()
    assert len(restored) == 1
    assert restored[0]["id"] == connection["id"]
    assert restored[0]["provider"] == "github"
    assert restored[0]["persistent"] is True
    assert restored[0]["restored"] is True
    assert "alice-access-token" not in json.dumps(restored)

    other_gateway, _ = restarted.runtime_for(TrustedPrincipal(user_id="bob"))
    assert other_gateway.list_connections() == []


def test_failed_reconnect_preserves_previous_runtime_and_stored_credential(monkeypatch):
    store = MemoryCredentialStore()
    gateway = ExternalMcpGateway(credential_owner="alice", credential_store=store)
    old = _add_github(gateway, access_token="old-access-token", persist=True)
    manager = McpOAuthManager(gateway)
    monkeypatch.setenv("ARCHBRO_GITHUB_OAUTH_CLIENT_ID", "github-client")
    monkeypatch.setenv("ARCHBRO_GITHUB_OAUTH_CLIENT_SECRET", "github-secret")
    redirect_uri = "https://archbro.example/mcp/oauth/github/callback"
    authorization_url = manager.start("github", redirect_uri)
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    monkeypatch.setattr(
        manager,
        "_exchange_token",
        lambda *_args, **_kwargs: {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        gateway,
        "_state_list_tools",
        lambda _state: (_ for _ in ()).throw(RuntimeError("provider rejected probe")),
    )

    with pytest.raises(RuntimeError, match="provider verification failed"):
        manager.complete(
            "github",
            state=state,
            code="authorization-code",
            redirect_uri=redirect_uri,
        )

    assert [item["id"] for item in gateway.list_connections()] == [old["id"]]
    assert store.rows[("alice", "github")].access_token == "old-access-token"


def test_remove_is_fail_closed_when_persistent_delete_fails():
    store = MemoryCredentialStore()
    gateway = ExternalMcpGateway(credential_owner="alice", credential_store=store)
    connection = _add_github(gateway, access_token="access-token", persist=True)
    store.fail_delete = True

    with pytest.raises(RuntimeError, match="database unavailable"):
        gateway.remove_connection(connection["id"])

    assert [item["id"] for item in gateway.list_connections()] == [connection["id"]]
    assert ("alice", "github") in store.rows


def test_oauth_refresh_updates_the_committed_credential(monkeypatch):
    store = MemoryCredentialStore()
    gateway = ExternalMcpGateway(credential_owner="alice", credential_store=store)
    connection = _add_github(
        gateway,
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        expires_in=1,
        persist=True,
    )
    state = gateway._get(connection["id"])
    assert state.oauth is not None
    state.oauth.expires_at = time.time() - 1

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "access_token": "refreshed-access-token",
                    "refresh_token": "refreshed-refresh-token",
                    "expires_in": 7200,
                }
            ).encode("utf-8")

    monkeypatch.setattr("archbro.backend.mcp.provider_gateway.urlopen", lambda *_a, **_k: Response())
    gateway._ensure_fresh_oauth(state)

    stored = store.rows[("alice", "github")]
    assert stored.access_token == "refreshed-access-token"
    assert stored.refresh_token == "refreshed-refresh-token"
    assert state.config.headers["Authorization"] == "Bearer refreshed-access-token"
    assert state.config.headers["X-MCP-Readonly"] == "true"


@requires_database
def test_postgres_store_encrypts_secrets_and_survives_a_new_instance(dsn):
    key = TEST_PROVIDER_CREDENTIAL_KEY
    store = PostgresProviderCredentialStore(dsn, key)
    credential = StoredProviderCredential(
        connection_id="mcp_persisted",
        provider="github",
        name="GitHub",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth",
        access_token="access-secret-value",
        refresh_token="refresh-secret-value",
        expires_at=12345.0,
        token_url="https://github.com/login/oauth/access_token",
        client_id="client-id",
        client_secret="client-secret-value",
        display_endpoint="GitHub remote MCP",
        tool_count=11,
    )
    store.upsert("alice", credential)

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT ciphertext FROM provider_credentials WHERE user_id=%s AND provider=%s",
            ("alice", "github"),
        ).fetchone()
    ciphertext = str(row[0])
    for secret in ("access-secret-value", "refresh-secret-value", "client-secret-value"):
        assert secret not in ciphertext

    restarted = PostgresProviderCredentialStore(dsn, key)
    assert restarted.list_for_user("alice") == [credential]
    assert restarted.list_for_user("bob") == []

    with pytest.raises(ValueError, match="does not match the existing provider credential store"):
        PostgresProviderCredentialStore(dsn, Fernet.generate_key().decode("ascii"))

    restarted.delete("alice", "github")
    assert restarted.list_for_user("alice") == []


@requires_database
def test_runtime_routes_restore_persisted_connection_and_remove_it_durably(dsn, monkeypatch):
    async def principal_provider(token: str) -> TrustedPrincipal:
        assert token == "alice"
        return TrustedPrincipal(user_id="alice", local_development=True)

    monkeypatch.setenv("ARCHBRO_ENV", "test")
    monkeypatch.setenv("ARCHBRO_AUTH_MODE", "local")
    key = TEST_PROVIDER_CREDENTIAL_KEY
    store = PostgresProviderCredentialStore(dsn, key)
    credential = StoredProviderCredential(
        connection_id="mcp_restored_route",
        provider="github",
        name="GitHub",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth",
        access_token="route-secret-token",
        refresh_token=None,
        expires_at=None,
        token_url="https://github.com/login/oauth/access_token",
        client_id="github-client",
        client_secret="github-secret",
        tool_count=11,
    )
    store.upsert("alice", credential)
    headers = {"Authorization": "Bearer alice"}

    first_app = build_app(
        PostgresProjectRepository(dsn),
        FakeModelProvider(),
        principal_provider=principal_provider,
        provider_credential_store=store,
    )
    with TestClient(first_app, base_url="http://127.0.0.1:8012") as client:
        listed = client.get("/mcp/connections", headers=headers)
        status = client.get("/mcp/oauth/github/status", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == "mcp_restored_route"
        assert listed.json()[0]["persistent"] is True
        assert listed.json()[0]["restored"] is True
        assert "route-secret-token" not in listed.text
        assert status.status_code == 200
        assert status.json()["connected"] is True
        assert status.json()["persistent"] is True
        assert status.json()["storage"] == "encrypted_postgres"
        assert status.json()["connection"]["id"] == "mcp_restored_route"

        removed = client.delete("/mcp/connections/mcp_restored_route", headers=headers)
        assert removed.status_code == 204

    restarted_store = PostgresProviderCredentialStore(dsn, key)
    second_app = build_app(
        PostgresProjectRepository(dsn),
        FakeModelProvider(),
        principal_provider=principal_provider,
        provider_credential_store=restarted_store,
    )
    with TestClient(second_app, base_url="http://127.0.0.1:8012") as client:
        assert client.get("/mcp/connections", headers=headers).json() == []


@requires_database
def test_concurrent_credential_store_initialization_uses_one_key_canary(dsn):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores=list(pool.map(lambda _:PostgresProviderCredentialStore(dsn,TEST_PROVIDER_CREDENTIAL_KEY),range(4)))
    assert len(stores)==4
    with psycopg.connect(dsn) as conn:
        assert conn.execute('SELECT COUNT(*) FROM provider_credential_store_metadata').fetchone()[0]==1


@requires_database
def test_separate_login_clients_share_only_their_own_persisted_connection(dsn,monkeypatch):
    monkeypatch.setenv('ARCHBRO_ENV','test'); monkeypatch.setenv('ARCHBRO_AUTH_MODE','local')
    async def principal_provider(token):
        return TrustedPrincipal(user_id=token,local_development=True)
    store=PostgresProviderCredentialStore(dsn,TEST_PROVIDER_CREDENTIAL_KEY)
    gateway=ExternalMcpGateway(credential_owner='alice',credential_store=store)
    connection=_add_github(gateway,access_token='fixture-alice',persist=True)
    assert 'fixture-alice' not in repr(store.list_for_user('alice'))
    for _ in range(2):
        # Different application/registry and HTTP session, same database/user.
        app=build_app(PostgresProjectRepository(dsn),FakeModelProvider(),principal_provider=principal_provider,
            provider_credential_store=PostgresProviderCredentialStore(dsn,TEST_PROVIDER_CREDENTIAL_KEY))
        with TestClient(app,base_url='http://127.0.0.1') as client:
            own=client.get('/mcp/connections',headers={'Authorization':'Bearer alice'})
            assert own.status_code==200 and own.json()[0]['id']==connection['id']
            assert own.json()[0]['persistent'] is True
            assert client.get('/mcp/connections',headers={'Authorization':'Bearer bob'}).json()==[]
            assert 'fixture-alice' not in own.text


@requires_database
def test_provider_credential_keys_rotate_online_and_rewrap_rows(dsn):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    credential = StoredProviderCredential(
        connection_id="mcp_rotation",
        provider="github",
        name="GitHub",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth",
        access_token='fixture-a1',
        refresh_token='fixture-r1',
        expires_at=time.time() + 3600,
        token_url="https://github.com/login/oauth/access_token",
        client_id="old-client",
        client_secret="",
        tool_count=11,
    )
    old_store = PostgresProviderCredentialStore(dsn, old_key)
    old_store.upsert("alice", credential)
    with psycopg.connect(dsn) as conn:
        old_ciphertext = conn.execute(
            "SELECT ciphertext FROM provider_credentials WHERE user_id=%s AND provider=%s",
            ("alice", "github"),
        ).fetchone()[0]
        old_canary = conn.execute(
            "SELECT key_check FROM provider_credential_store_metadata WHERE singleton_id=1"
        ).fetchone()[0]

    rotated = PostgresProviderCredentialStore(dsn, f"{new_key},{old_key}")
    assert rotated.get("alice", "github") == credential
    with psycopg.connect(dsn) as conn:
        new_ciphertext = conn.execute(
            "SELECT ciphertext FROM provider_credentials WHERE user_id=%s AND provider=%s",
            ("alice", "github"),
        ).fetchone()[0]
        new_canary = conn.execute(
            "SELECT key_check FROM provider_credential_store_metadata WHERE singleton_id=1"
        ).fetchone()[0]

    assert new_ciphertext != old_ciphertext
    assert new_canary != old_canary
    Fernet(new_key.encode("ascii")).decrypt(str(new_ciphertext).encode("ascii"))
    Fernet(new_key.encode("ascii")).decrypt(str(new_canary).encode("ascii"))
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode("ascii")).decrypt(str(new_ciphertext).encode("ascii"))

    new_only = PostgresProviderCredentialStore(dsn, new_key)
    assert new_only.get("alice", "github") == credential


@requires_database
def test_one_corrupt_provider_grant_does_not_hide_other_providers(dsn):
    store = PostgresProviderCredentialStore(dsn, TEST_PROVIDER_CREDENTIAL_KEY)
    github = StoredProviderCredential(
        connection_id="mcp_corrupt_github",
        provider="github",
        name="GitHub",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth",
        access_token='fixture-a2',
        refresh_token='fixture-r2',
        expires_at=None,
        token_url="https://github.com/login/oauth/access_token",
        client_id="github-client",
        client_secret="github-secret",
    )
    slack = StoredProviderCredential(
        connection_id="mcp_valid_slack",
        provider="slack",
        name="Slack",
        url="https://mcp.slack.com/mcp",
        auth_type="oauth",
        access_token='fixture-a3',
        refresh_token='fixture-r3',
        expires_at=None,
        token_url="https://slack.com/api/oauth.v2.access",
        client_id="slack-client",
        client_secret="slack-secret",
    )
    store.upsert("alice", github)
    store.upsert("alice", slack)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE provider_credentials SET ciphertext=%s "
            "WHERE user_id=%s AND provider=%s",
            ("not-a-valid-fernet-envelope", "alice", "github"),
        )

    registry = ProviderMcpRuntimeRegistry(store)
    gateway, _ = registry.runtime_for(TrustedPrincipal(user_id="alice"))
    assert [item["provider"] for item in gateway.list_connections()] == ["slack"]
    assert registry.restore_error("alice", "github") == (
        "The saved authorization could not be restored. Reconnect this provider."
    )
    assert registry.restore_error("alice", "slack") is None
    serialized = json.dumps(gateway.list_connections())
    assert "slack-access-secret" not in serialized
    assert "github-access-secret" not in serialized


def test_concurrent_remove_during_restore_is_absent_not_a_reconnect_error():
    class VanishingCredentialStore(MemoryCredentialStore):
        def providers_for_user(self, user_id: str) -> list[str]:
            assert user_id == "alice"
            return ["github"]

        def get(self, user_id: str, provider: str) -> StoredProviderCredential:
            raise KeyError((user_id, provider))

    registry = ProviderMcpRuntimeRegistry(VanishingCredentialStore())
    gateway, _ = registry.runtime_for(TrustedPrincipal(user_id="alice"))
    assert gateway.list_connections() == []
    assert registry.restore_error("alice", "github") is None


def test_expired_unrefreshable_grant_requires_reconnect_instead_of_showing_connected():
    store = MemoryCredentialStore()
    store.rows[("alice", "github")] = StoredProviderCredential(
        connection_id="mcp_expired",
        provider="github",
        name="GitHub",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth",
        access_token='fixture-a4',
        refresh_token=None,
        expires_at=time.time() - 120,
        token_url="https://github.com/login/oauth/access_token",
        client_id="github-client",
        client_secret="github-secret",
    )
    registry = ProviderMcpRuntimeRegistry(store)
    gateway, _ = registry.runtime_for(TrustedPrincipal(user_id="alice"))
    assert gateway.list_connections() == []
    assert registry.restore_error("alice", "github") == (
        "The saved authorization could not be restored. Reconnect this provider."
    )


def test_restore_error_status_is_safe_durable_and_never_enables_legacy_fallback(
    monkeypatch,
):
    store = MemoryCredentialStore()
    seed_gateway = ExternalMcpGateway(
        credential_owner="alice",
        credential_store=store,
    )
    fixture_kwargs = {
        "access_" + "token": "status-fixture",
        "persist": True,
    }
    _add_github(seed_gateway, **fixture_kwargs)
    stored = store.rows[("alice", "github")]
    store.rows[("alice", "github")] = replace(
        stored,
        expires_at=time.time() - 120,
        refresh_token=None,
    )
    monkeypatch.setenv("ARCHBRO_ENV", "production")
    monkeypatch.setenv("ARCHBRO_GITHUB_OAUTH_CLIENT_ID", "current-client")
    monkeypatch.setenv("ARCHBRO_GITHUB_OAUTH_CLIENT_SECRET", "current-secret")
    monkeypatch.setenv("ARCHBRO_ALLOW_EPHEMERAL_PROVIDER_OAUTH", "false")
    monkeypatch.setenv("ARCHBRO_OAUTH_REDIRECT_BASE_URL", "https://archbro.example")

    registry = ProviderMcpRuntimeRegistry(store)

    async def principal(_request):
        return TrustedPrincipal(user_id="alice")

    app = FastAPI()
    app.include_router(build_provider_mcp_router(principal, registry))
    with TestClient(app, base_url="https://archbro.example") as client:
        response = client.get("/mcp/oauth/github/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["persistent"] is True
    assert payload["durable_authorization"] is False
    assert payload["legacy_fallback_allowed"] is False
    assert payload["restore_error"] == (
        "The saved authorization could not be restored. Reconnect this provider."
    )
    assert "status-fixture" not in response.text
    assert "current-secret" not in response.text


def test_restore_uses_current_deployment_oauth_identity_and_stops_persisting_client_secret(
    monkeypatch,
):
    store = MemoryCredentialStore()
    store.rows[("alice", "github")] = StoredProviderCredential(
        connection_id="mcp_rotated_client",
        provider="github",
        name="Old GitHub label",
        url="https://old.example/mcp",
        auth_type="oauth",
        access_token='fixture-a5',
        refresh_token='fixture-r5',
        expires_at=time.time() + 3600,
        token_url="https://github.com/login/oauth/access_token",
        client_id="retired-client",
        client_secret="retired-secret",
    )
    monkeypatch.setenv("ARCHBRO_GITHUB_OAUTH_CLIENT_ID", "current-client")
    monkeypatch.setenv("ARCHBRO_GITHUB_OAUTH_CLIENT_SECRET", "current-secret")

    registry = ProviderMcpRuntimeRegistry(store)
    gateway, _ = registry.runtime_for(TrustedPrincipal(user_id="alice"))
    connection = gateway.list_connections()[0]
    state = gateway._get(connection["id"])
    assert state.oauth is not None
    assert state.oauth.client_id == "current-client"
    assert state.oauth.client_secret == "current-secret"
    assert state.config.url == "https://api.githubcopilot.com/mcp/"

    gateway._persist_state(state)
    persisted = store.rows[("alice", "github")]
    assert persisted.client_id == "current-client"
    assert persisted.client_secret == ""
    assert "current-secret" not in repr(persisted)
