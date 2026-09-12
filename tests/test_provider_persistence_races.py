"""No external OAuth: exercise concurrent account requests with recording fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import pytest
from archbro.backend.mcp.provider_gateway import ExternalMcpGateway
from test_provider_credential_persistence import MemoryCredentialStore, _add_github


def fixture_gateway(monkeypatch):
    store=MemoryCredentialStore()
    gateway=ExternalMcpGateway(credential_owner='alice',credential_store=store)
    conn=_add_github(gateway,access_token='fixture-expired',persist=True)
    state=gateway._get(conn['id']);state.oauth.expires_at=time.time()-1
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def read(self):
            return json.dumps({'access_token':'fixture-refreshed','refresh_token':'fixture-rotated','expires_in':3600}).encode()
    def request(*_a,**_k):
        calls.append(1);time.sleep(.03);return Response()
    monkeypatch.setattr('archbro.backend.mcp.provider_gateway.urlopen',request)
    return gateway,store,state,calls


def test_two_clients_refresh_expired_credential_only_once(monkeypatch):
    gateway,store,state,calls=fixture_gateway(monkeypatch)
    barrier=threading.Barrier(2)
    def refresh(_):
        barrier.wait();gateway._ensure_fresh_oauth(state)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(refresh,range(2)))
    assert len(calls)==1
    assert store.rows['alice','github'].access_token=='fixture-refreshed'


def test_failed_refresh_persistence_is_retried_before_using_new_token(monkeypatch):
    gateway,store,state,calls=fixture_gateway(monkeypatch)
    original=store.upsert
    failures=[True]
    def upsert(owner,credential):
        if failures and failures.pop():
            raise RuntimeError('fixture persistence unavailable')
        original(owner,credential)
    monkeypatch.setattr(store,'upsert',upsert)
    with pytest.raises(RuntimeError,match='persistence unavailable'):
        gateway._ensure_fresh_oauth(state)
    gateway._ensure_fresh_oauth(state)
    assert len(calls)==1
    assert store.rows['alice','github'].access_token=='fixture-refreshed'


def test_removed_connection_cannot_be_recreated_by_a_late_probe_save(monkeypatch):
    gateway,store,state,calls=fixture_gateway(monkeypatch)
    gateway.remove_connection(state.id)
    with pytest.raises((RuntimeError,KeyError),match='connection'):
        gateway._persist_state(state)
    assert store.list_for_user('alice')==[]
