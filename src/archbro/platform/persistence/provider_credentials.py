from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import psycopg
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from psycopg.rows import dict_row

from archbro.backend.mcp.provider_credentials import (
    ProviderCredentialStore,
    StoredProviderCredential,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_credential_store_metadata (
    singleton_id SMALLINT PRIMARY KEY CHECK(singleton_id = 1),
    key_check TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_credentials (
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_user
    ON provider_credentials(user_id);
"""

_KEY_CHECK = b"archbro-provider-credential-store-key-check-v1"
_KEY_INIT_LOCK = 4706905503163011


class PostgresProviderCredentialStore(ProviderCredentialStore):
    """Principal-scoped OAuth credentials encrypted before entering PostgreSQL.

    ``ARCHBRO_PROVIDER_CREDENTIAL_KEY`` accepts one Fernet key or a comma-
    separated rotation set. The first key encrypts all new writes; remaining
    keys are read-only fallbacks. Reading an envelope or the key canary through
    a fallback key rewrites it with the primary key so an old key can be removed
    after every row has been exercised or explicitly migrated.
    """

    def __init__(self, dsn: str, encryption_key: str) -> None:
        self.dsn = dsn.strip()
        if not self.dsn:
            raise ValueError("provider credential store requires a PostgreSQL DSN")

        configured_keys = [
            value.strip()
            for value in str(encryption_key or "").split(",")
            if value.strip()
        ]
        if not configured_keys:
            raise ValueError(
                "ARCHBRO_PROVIDER_CREDENTIAL_KEY must contain at least one valid Fernet key"
            )
        try:
            fernets = [Fernet(value.encode("ascii")) for value in configured_keys]
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError(
                "ARCHBRO_PROVIDER_CREDENTIAL_KEY must contain valid Fernet keys"
            ) from exc
        self._primary = fernets[0]
        self._fernet = MultiFernet(fernets)

        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_KEY_INIT_LOCK,))
            conn.execute(_SCHEMA)
            row = conn.execute(
                "SELECT key_check FROM provider_credential_store_metadata WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                key_check = self._primary.encrypt(_KEY_CHECK).decode("ascii")
                conn.execute(
                    "INSERT INTO provider_credential_store_metadata(singleton_id, key_check) "
                    "VALUES (1, %s)",
                    (key_check,),
                )
            else:
                token = str(row["key_check"])
                try:
                    plaintext, used_fallback = self._decrypt_bytes(token)
                except (InvalidToken, UnicodeError, ValueError) as exc:
                    raise ValueError(
                        "ARCHBRO_PROVIDER_CREDENTIAL_KEY does not match the existing "
                        "provider credential store"
                    ) from exc
                if plaintext != _KEY_CHECK:
                    raise ValueError(
                        "provider credential store key check has an unsupported format"
                    )
                if used_fallback:
                    conn.execute(
                        "UPDATE provider_credential_store_metadata SET key_check=%s "
                        "WHERE singleton_id=1 AND key_check=%s",
                        (self._primary.encrypt(_KEY_CHECK).decode("ascii"), token),
                    )

    @property
    def persistent(self) -> bool:
        return True

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _require_identity(
        user_id: str,
        provider: str | None = None,
    ) -> tuple[str, str | None]:
        owner = user_id.strip()
        normalized_provider = provider.strip().lower() if provider is not None else None
        if not owner:
            raise ValueError("provider credential user_id is required")
        if provider is not None and not normalized_provider:
            raise ValueError("provider credential provider is required")
        return owner, normalized_provider

    def _decrypt_bytes(self, stored: str) -> tuple[bytes, bool]:
        encoded = stored.encode("ascii")
        try:
            return self._primary.decrypt(encoded), False
        except InvalidToken:
            return self._fernet.decrypt(encoded), True

    @staticmethod
    def _envelope(user_id: str, credential: StoredProviderCredential) -> dict:
        return {
            "user_id": user_id,
            "provider": credential.provider,
            "credential": credential.to_payload(),
        }

    def _encrypt(self, user_id: str, credential: StoredProviderCredential) -> str:
        encoded = json.dumps(
            self._envelope(user_id, credential),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return self._primary.encrypt(encoded).decode("ascii")

    def _decrypt(
        self,
        *,
        user_id: str,
        provider: str,
        ciphertext: str,
    ) -> tuple[StoredProviderCredential, bool]:
        try:
            plaintext, used_fallback = self._decrypt_bytes(ciphertext)
            envelope = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                "Stored provider authorization could not be decrypted. "
                "Restore the deployment credential key or reconnect the provider."
            ) from exc
        if not isinstance(envelope, dict):
            raise RuntimeError("Stored provider authorization has an invalid envelope")
        if envelope.get("user_id") != user_id or envelope.get("provider") != provider:
            raise RuntimeError("Stored provider authorization identity does not match its row")
        payload = envelope.get("credential")
        if not isinstance(payload, dict):
            raise RuntimeError("Stored provider authorization payload is invalid")
        try:
            credential = StoredProviderCredential.from_payload(payload)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Stored provider authorization payload is invalid") from exc
        if credential.provider != provider:
            raise RuntimeError("Stored provider authorization provider does not match its row")
        return credential, used_fallback

    def providers_for_user(self, user_id: str) -> list[str]:
        owner, _ = self._require_identity(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider FROM provider_credentials "
                "WHERE user_id=%s ORDER BY provider",
                (owner,),
            ).fetchall()
        return [str(row["provider"]) for row in rows]

    def get(self, user_id: str, provider: str) -> StoredProviderCredential:
        owner, normalized_provider = self._require_identity(user_id, provider)
        assert normalized_provider is not None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM provider_credentials "
                "WHERE user_id=%s AND provider=%s",
                (owner, normalized_provider),
            ).fetchone()
            if row is None:
                raise KeyError((owner, normalized_provider))
            ciphertext = str(row["ciphertext"])
            credential, used_fallback = self._decrypt(
                user_id=owner,
                provider=normalized_provider,
                ciphertext=ciphertext,
            )
            if used_fallback:
                conn.execute(
                    "UPDATE provider_credentials SET ciphertext=%s, updated_at=%s "
                    "WHERE user_id=%s AND provider=%s AND ciphertext=%s",
                    (
                        self._encrypt(owner, credential),
                        datetime.now(timezone.utc).isoformat(),
                        owner,
                        normalized_provider,
                        ciphertext,
                    ),
                )
        return credential

    def list_for_user(self, user_id: str) -> list[StoredProviderCredential]:
        return [
            self.get(user_id, provider)
            for provider in self.providers_for_user(user_id)
        ]

    def upsert(self, user_id: str, credential: StoredProviderCredential) -> None:
        owner, provider = self._require_identity(user_id, credential.provider)
        assert provider is not None
        if credential.provider != provider:
            credential = replace(credential, provider=provider)
        ciphertext = self._encrypt(owner, credential)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_credentials(user_id, provider, ciphertext, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(user_id, provider) DO UPDATE SET
                    ciphertext=EXCLUDED.ciphertext,
                    updated_at=EXCLUDED.updated_at
                """,
                (owner, provider, ciphertext, updated_at),
            )

    def delete(self, user_id: str, provider: str) -> None:
        owner, normalized_provider = self._require_identity(user_id, provider)
        assert normalized_provider is not None
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM provider_credentials WHERE user_id=%s AND provider=%s",
                (owner, normalized_provider),
            )
