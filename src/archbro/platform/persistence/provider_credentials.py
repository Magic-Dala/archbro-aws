from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
from cryptography.fernet import Fernet, InvalidToken
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


class PostgresProviderCredentialStore(ProviderCredentialStore):
    """Principal-scoped OAuth credentials encrypted before entering PostgreSQL."""

    def __init__(self, dsn: str, encryption_key: str) -> None:
        self.dsn = dsn.strip()
        if not self.dsn:
            raise ValueError("provider credential store requires a PostgreSQL DSN")
        try:
            self._fernet = Fernet(encryption_key.strip().encode("ascii"))
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError(
                "ARCHBRO_PROVIDER_CREDENTIAL_KEY must be a valid Fernet key"
            ) from exc
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (4706905503163011,))
            conn.execute(_SCHEMA)
            row = conn.execute(
                "SELECT key_check FROM provider_credential_store_metadata WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                key_check = self._fernet.encrypt(
                    b"archbro-provider-credential-store-key-check-v1"
                ).decode("ascii")
                conn.execute(
                    "INSERT INTO provider_credential_store_metadata(singleton_id, key_check) VALUES (1, %s)",
                    (key_check,),
                )
            else:
                try:
                    plaintext = self._fernet.decrypt(str(row["key_check"]).encode("ascii"))
                except (InvalidToken, UnicodeError, ValueError) as exc:
                    raise ValueError(
                        "ARCHBRO_PROVIDER_CREDENTIAL_KEY does not match the existing provider credential store"
                    ) from exc
                if plaintext != b"archbro-provider-credential-store-key-check-v1":
                    raise ValueError(
                        "provider credential store key check has an unsupported format"
                    )

    @property
    def persistent(self) -> bool:
        return True

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _require_identity(user_id: str, provider: str | None = None) -> tuple[str, str | None]:
        owner = user_id.strip()
        normalized_provider = provider.strip() if provider is not None else None
        if not owner:
            raise ValueError("provider credential user_id is required")
        if provider is not None and not normalized_provider:
            raise ValueError("provider credential provider is required")
        return owner, normalized_provider

    def _encrypt(self, user_id: str, credential: StoredProviderCredential) -> str:
        envelope = {
            "user_id": user_id,
            "provider": credential.provider,
            "credential": credential.to_payload(),
        }
        encoded = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return self._fernet.encrypt(encoded).decode("ascii")

    def _decrypt(
        self,
        *,
        user_id: str,
        provider: str,
        ciphertext: str,
    ) -> StoredProviderCredential:
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
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
        return credential

    def list_for_user(self, user_id: str) -> list[StoredProviderCredential]:
        owner, _ = self._require_identity(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, ciphertext FROM provider_credentials "
                "WHERE user_id=%s ORDER BY provider",
                (owner,),
            ).fetchall()
        return [
            self._decrypt(
                user_id=owner,
                provider=str(row["provider"]),
                ciphertext=str(row["ciphertext"]),
            )
            for row in rows
        ]

    def upsert(self, user_id: str, credential: StoredProviderCredential) -> None:
        owner, provider = self._require_identity(user_id, credential.provider)
        assert provider is not None
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
