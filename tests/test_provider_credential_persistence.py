from __future__ import annotations

import base64
import json
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import psycopg
import pytest

from archbro.backend.api.provider_connections import ProviderMcpRuntimeRegistry
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

    def list_for_user(self, user_id: str) -> list[StoredProviderCredential]:
        return [
            credential
            for (owner, _), credential in sorted(self.rows.items())
            if owner == user_id
        ]

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
