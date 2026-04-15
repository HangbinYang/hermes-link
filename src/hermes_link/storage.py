from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from hermes_link.models import (
    AccessTokenRecord,
    AuditEvent,
    DeviceRecord,
    PairingSession,
    RefreshTokenRecord,
    utc_now,
)


class LinkRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pairing_sessions (
                    session_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    note TEXT,
                    claimed_at TEXT,
                    claimed_device_id TEXT
                );

                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS access_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );

                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    refresh_token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );

                CREATE INDEX IF NOT EXISTS idx_refresh_tokens_device_active
                ON refresh_tokens (device_id, revoked_at, expires_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT,
                    detail_json TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def _row_to_pairing_session(self, row: sqlite3.Row) -> PairingSession:
        return PairingSession(
            session_id=row["session_id"],
            code=row["code"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            status=row["status"],
            scopes=json.loads(row["scopes_json"]),
            note=row["note"],
            claimed_at=row["claimed_at"],
            claimed_device_id=row["claimed_device_id"],
        )

    def _row_to_device(self, row: sqlite3.Row) -> DeviceRecord:
        return DeviceRecord(
            device_id=row["device_id"],
            label=row["label"],
            platform=row["platform"],
            scopes=json.loads(row["scopes_json"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            status=row["status"],
        )

    def _row_to_refresh_token(self, row: sqlite3.Row) -> RefreshTokenRecord:
        return RefreshTokenRecord(
            refresh_token_id=row["refresh_token_id"],
            token_hash=row["token_hash"],
            device_id=row["device_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def append_audit_event(
        self,
        event_type: str,
        *,
        actor_type: str,
        actor_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (event_type, occurred_at, actor_type, actor_id, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    utc_now().isoformat(),
                    actor_type,
                    actor_id,
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def create_pairing_session(
        self,
        *,
        session_id: str,
        code: str,
        expires_at: datetime,
        scopes: list[str],
        note: str | None = None,
    ) -> PairingSession:
        created_at = utc_now().isoformat()
        payload = (
            session_id,
            code,
            created_at,
            expires_at.isoformat(),
            "pending",
            json.dumps(scopes),
            note,
            None,
            None,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pairing_sessions (
                    session_id, code, created_at, expires_at, status, scopes_json, note, claimed_at, claimed_device_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            conn.commit()
        return PairingSession(
            session_id=session_id,
            code=code,
            created_at=created_at,
            expires_at=expires_at.isoformat(),
            status="pending",
            scopes=scopes,
            note=note,
        )

    def list_pairing_sessions(self, *, include_non_pending: bool = False) -> list[PairingSession]:
        query = "SELECT * FROM pairing_sessions"
        params: tuple = ()
        if not include_non_pending:
            query += " WHERE status = ?"
            params = ("pending",)
        query += " ORDER BY created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_pairing_session(row) for row in rows]

    def get_pairing_session(self, session_id: str) -> PairingSession | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._row_to_pairing_session(row) if row else None

    def expire_stale_pairings(self) -> int:
        now = utc_now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pairing_sessions
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at < ?
                """,
                (now,),
            )
            conn.commit()
            return cursor.rowcount

    def count_pending_pairings(self) -> int:
        self.expire_stale_pairings()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM pairing_sessions WHERE status = 'pending'"
            ).fetchone()
        return int(row["count"])

    def cancel_pairing_session(self, session_id: str) -> PairingSession | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "pending":
                conn.execute(
                    "UPDATE pairing_sessions SET status = 'cancelled' WHERE session_id = ?",
                    (session_id,),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM pairing_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        return self._row_to_pairing_session(row) if row else None

    def create_device(self, *, device_id: str, label: str, platform: str, scopes: list[str]) -> DeviceRecord:
        created_at = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO devices (device_id, label, platform, scopes_json, created_at, last_seen_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'active')
                """,
                (device_id, label, platform, json.dumps(scopes), created_at, created_at),
            )
            conn.commit()
        return DeviceRecord(
            device_id=device_id,
            label=label,
            platform=platform,
            scopes=scopes,
            created_at=created_at,
            last_seen_at=created_at,
            status="active",
        )

    def create_access_token(
        self,
        *,
        token_id: str,
        token_prefix: str,
        token_hash: str,
        device_id: str,
        scopes: list[str],
        expires_at: datetime,
    ) -> AccessTokenRecord:
        created_at = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO access_tokens (
                    token_id, token_prefix, token_hash, device_id, scopes_json, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    token_id,
                    token_prefix,
                    token_hash,
                    device_id,
                    json.dumps(scopes),
                    created_at,
                    expires_at.isoformat(),
                ),
            )
            conn.commit()
        return AccessTokenRecord(
            token_id=token_id,
            token_prefix=token_prefix,
            token_hash=token_hash,
            device_id=device_id,
            scopes=scopes,
            created_at=created_at,
            expires_at=expires_at.isoformat(),
            revoked_at=None,
        )

    def claim_pairing_session(
        self,
        *,
        session_id: str,
        normalized_code: str,
        device_id: str,
        device_label: str,
        device_platform: str,
        token_id: str,
        token_prefix: str,
        token_hash: str,
        token_expires_at: datetime,
        refresh_token_id: str,
        refresh_token_hash: str,
        refresh_token_expires_at: datetime,
    ) -> tuple[DeviceRecord, AccessTokenRecord, RefreshTokenRecord]:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError("pairing_session_not_found")
            if row["status"] != "pending":
                raise ValueError("pairing_session_not_pending")
            if row["expires_at"] < now.isoformat():
                conn.execute(
                    "UPDATE pairing_sessions SET status = 'expired' WHERE session_id = ?",
                    (session_id,),
                )
                conn.commit()
                raise ValueError("pairing_session_expired")
            if row["code"] != normalized_code:
                raise ValueError("pairing_code_invalid")

            scopes = json.loads(row["scopes_json"])
            conn.execute(
                """
                INSERT INTO devices (device_id, label, platform, scopes_json, created_at, last_seen_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    device_id,
                    device_label,
                    device_platform,
                    json.dumps(scopes),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO access_tokens (
                    token_id, token_prefix, token_hash, device_id, scopes_json, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    token_id,
                    token_prefix,
                    token_hash,
                    device_id,
                    json.dumps(scopes),
                    now.isoformat(),
                    token_expires_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO refresh_tokens (
                    refresh_token_id, token_hash, device_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    refresh_token_id,
                    refresh_token_hash,
                    device_id,
                    now.isoformat(),
                    refresh_token_expires_at.isoformat(),
                ),
            )
            conn.execute(
                """
                UPDATE pairing_sessions
                SET status = 'claimed', claimed_at = ?, claimed_device_id = ?
                WHERE session_id = ?
                """,
                (now.isoformat(), device_id, session_id),
            )
            conn.commit()

        return (
            DeviceRecord(
                device_id=device_id,
                label=device_label,
                platform=device_platform,
                scopes=scopes,
                created_at=now.isoformat(),
                last_seen_at=now.isoformat(),
                status="active",
            ),
            AccessTokenRecord(
                token_id=token_id,
                token_prefix=token_prefix,
                token_hash=token_hash,
                device_id=device_id,
                scopes=scopes,
                created_at=now.isoformat(),
                expires_at=token_expires_at.isoformat(),
                revoked_at=None,
            ),
            RefreshTokenRecord(
                refresh_token_id=refresh_token_id,
                token_hash=refresh_token_hash,
                device_id=device_id,
                created_at=now.isoformat(),
                expires_at=refresh_token_expires_at.isoformat(),
                revoked_at=None,
            ),
        )

    def validate_access_token(self, *, token_hash: str) -> tuple[DeviceRecord, AccessTokenRecord] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    t.token_id, t.token_prefix, t.token_hash, t.device_id, t.scopes_json AS token_scopes_json,
                    t.created_at AS token_created_at, t.expires_at, t.revoked_at,
                    d.label, d.platform, d.scopes_json AS device_scopes_json, d.created_at,
                    d.last_seen_at, d.status
                FROM access_tokens t
                JOIN devices d ON d.device_id = t.device_id
                WHERE t.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()

            if row is None:
                return None
            if row["revoked_at"] is not None:
                return None
            if row["status"] != "active":
                return None
            if row["expires_at"] < utc_now().isoformat():
                return None

            device = DeviceRecord(
                device_id=row["device_id"],
                label=row["label"],
                platform=row["platform"],
                scopes=json.loads(row["device_scopes_json"]),
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
                status=row["status"],
            )
            token = AccessTokenRecord(
                token_id=row["token_id"],
                token_prefix=row["token_prefix"],
                token_hash=row["token_hash"],
                device_id=row["device_id"],
                scopes=json.loads(row["token_scopes_json"]),
                created_at=row["token_created_at"],
                expires_at=row["expires_at"],
                revoked_at=row["revoked_at"],
            )
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (utc_now().isoformat(), device.device_id),
            )
            conn.commit()
            return device, token

    def rotate_access_token(
        self,
        *,
        current_token_id: str,
        new_token_id: str,
        token_prefix: str,
        token_hash: str,
        expires_at: datetime,
    ) -> tuple[DeviceRecord, AccessTokenRecord] | None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    t.token_id,
                    t.device_id,
                    t.scopes_json AS token_scopes_json,
                    d.label,
                    d.platform,
                    d.scopes_json AS device_scopes_json,
                    d.created_at,
                    d.last_seen_at,
                    d.status
                FROM access_tokens t
                JOIN devices d ON d.device_id = t.device_id
                WHERE t.token_id = ? AND t.revoked_at IS NULL
                """,
                (current_token_id,),
            ).fetchone()
            if row is None or row["status"] != "active":
                return None

            conn.execute(
                """
                INSERT INTO access_tokens (
                    token_id, token_prefix, token_hash, device_id, scopes_json, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    new_token_id,
                    token_prefix,
                    token_hash,
                    row["device_id"],
                    row["token_scopes_json"],
                    now,
                    expires_at.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE access_tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (now, current_token_id),
            )
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (now, row["device_id"]),
            )
            conn.commit()

        device = DeviceRecord(
            device_id=row["device_id"],
            label=row["label"],
            platform=row["platform"],
            scopes=json.loads(row["device_scopes_json"]),
            created_at=row["created_at"],
            last_seen_at=now,
            status=row["status"],
        )
        token = AccessTokenRecord(
            token_id=new_token_id,
            token_prefix=token_prefix,
            token_hash=token_hash,
            device_id=row["device_id"],
            scopes=json.loads(row["token_scopes_json"]),
            created_at=now,
            expires_at=expires_at.isoformat(),
            revoked_at=None,
        )
        return device, token

    def validate_refresh_token(self, *, token_hash: str) -> tuple[DeviceRecord, RefreshTokenRecord] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    r.refresh_token_id,
                    r.token_hash,
                    r.device_id,
                    r.created_at AS refresh_created_at,
                    r.expires_at AS refresh_expires_at,
                    r.revoked_at,
                    d.label,
                    d.platform,
                    d.scopes_json AS device_scopes_json,
                    d.created_at,
                    d.last_seen_at,
                    d.status
                FROM refresh_tokens r
                JOIN devices d ON d.device_id = r.device_id
                WHERE r.token_hash = ?
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()

            if row is None:
                return None
            if row["revoked_at"] is not None:
                return None
            if row["status"] != "active":
                return None
            if row["refresh_expires_at"] < utc_now().isoformat():
                return None

            device = DeviceRecord(
                device_id=row["device_id"],
                label=row["label"],
                platform=row["platform"],
                scopes=json.loads(row["device_scopes_json"]),
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
                status=row["status"],
            )
            refresh_token = RefreshTokenRecord(
                refresh_token_id=row["refresh_token_id"],
                token_hash=row["token_hash"],
                device_id=row["device_id"],
                created_at=row["refresh_created_at"],
                expires_at=row["refresh_expires_at"],
                revoked_at=row["revoked_at"],
            )
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (utc_now().isoformat(), device.device_id),
            )
            conn.commit()
            return device, refresh_token

    def rotate_device_session(
        self,
        *,
        device_id: str,
        current_token_id: str | None,
        current_refresh_token_id: str | None,
        new_access_token_id: str,
        new_access_token_prefix: str,
        new_access_token_hash: str,
        new_access_token_expires_at: datetime,
        new_refresh_token_id: str,
        new_refresh_token_hash: str,
        new_refresh_token_expires_at: datetime,
    ) -> tuple[DeviceRecord, AccessTokenRecord, RefreshTokenRecord] | None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT device_id, label, platform, scopes_json, created_at, last_seen_at, status
                FROM devices
                WHERE device_id = ? AND status = 'active'
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if row is None:
                return None

            if current_token_id:
                current_access = conn.execute(
                    """
                    SELECT token_id
                    FROM access_tokens
                    WHERE token_id = ? AND device_id = ? AND revoked_at IS NULL
                    LIMIT 1
                    """,
                    (current_token_id, device_id),
                ).fetchone()
                if current_access is None:
                    return None

            if current_refresh_token_id:
                current_refresh = conn.execute(
                    """
                    SELECT refresh_token_id, expires_at
                    FROM refresh_tokens
                    WHERE refresh_token_id = ? AND device_id = ? AND revoked_at IS NULL
                    LIMIT 1
                    """,
                    (current_refresh_token_id, device_id),
                ).fetchone()
                if current_refresh is None or current_refresh["expires_at"] < now:
                    return None

            conn.execute(
                "UPDATE access_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            conn.execute(
                """
                INSERT INTO access_tokens (
                    token_id, token_prefix, token_hash, device_id, scopes_json, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    new_access_token_id,
                    new_access_token_prefix,
                    new_access_token_hash,
                    device_id,
                    row["scopes_json"],
                    now,
                    new_access_token_expires_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO refresh_tokens (
                    refresh_token_id, token_hash, device_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    new_refresh_token_id,
                    new_refresh_token_hash,
                    device_id,
                    now,
                    new_refresh_token_expires_at.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (now, device_id),
            )
            conn.commit()

        device = DeviceRecord(
            device_id=row["device_id"],
            label=row["label"],
            platform=row["platform"],
            scopes=json.loads(row["scopes_json"]),
            created_at=row["created_at"],
            last_seen_at=now,
            status=row["status"],
        )
        access_token = AccessTokenRecord(
            token_id=new_access_token_id,
            token_prefix=new_access_token_prefix,
            token_hash=new_access_token_hash,
            device_id=device_id,
            scopes=json.loads(row["scopes_json"]),
            created_at=now,
            expires_at=new_access_token_expires_at.isoformat(),
            revoked_at=None,
        )
        refresh_token = RefreshTokenRecord(
            refresh_token_id=new_refresh_token_id,
            token_hash=new_refresh_token_hash,
            device_id=device_id,
            created_at=now,
            expires_at=new_refresh_token_expires_at.isoformat(),
            revoked_at=None,
        )
        return device, access_token, refresh_token

    def revoke_access_token(self, token_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE access_tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (utc_now().isoformat(), token_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_devices(self) -> list[DeviceRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY created_at DESC").fetchall()
        return [self._row_to_device(row) for row in rows]

    def get_active_device(self, device_id: str) -> DeviceRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id = ? AND status = 'active'",
                (device_id,),
            ).fetchone()
        return self._row_to_device(row) if row else None

    def get_active_refresh_token_for_device(self, device_id: str) -> RefreshTokenRecord | None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    r.refresh_token_id,
                    r.token_hash,
                    r.device_id,
                    r.created_at,
                    r.expires_at,
                    r.revoked_at
                FROM refresh_tokens r
                JOIN devices d ON d.device_id = r.device_id
                WHERE r.device_id = ?
                  AND r.revoked_at IS NULL
                  AND r.expires_at >= ?
                  AND d.status = 'active'
                ORDER BY r.created_at DESC
                LIMIT 1
                """,
                (device_id, now),
            ).fetchone()
        return self._row_to_refresh_token(row) if row else None

    def device_has_active_session(self, device_id: str) -> bool:
        now = utc_now().isoformat()
        with self.connect() as conn:
            access_row = conn.execute(
                """
                SELECT 1
                FROM access_tokens
                WHERE device_id = ? AND revoked_at IS NULL AND expires_at >= ?
                LIMIT 1
                """,
                (device_id, now),
            ).fetchone()
            if access_row is not None:
                return True
            refresh_row = conn.execute(
                """
                SELECT 1
                FROM refresh_tokens
                WHERE device_id = ? AND revoked_at IS NULL AND expires_at >= ?
                LIMIT 1
                """,
                (device_id, now),
            ).fetchone()
        return refresh_row is not None

    def count_active_devices(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM devices WHERE status = 'active'"
            ).fetchone()
        return int(row["count"])

    def revoke_device(self, device_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE devices SET status = 'revoked' WHERE device_id = ? AND status = 'active'",
                (device_id,),
            )
            conn.execute(
                "UPDATE access_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (utc_now().isoformat(), device_id),
            )
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (utc_now().isoformat(), device_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def revoke_all_devices(self) -> int:
        now = utc_now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE devices SET status = 'revoked' WHERE status = 'active'"
            )
            conn.execute(
                "UPDATE access_tokens SET revoked_at = ? WHERE revoked_at IS NULL",
                (now,),
            )
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE revoked_at IS NULL",
                (now,),
            )
            conn.commit()
            return cursor.rowcount

    def revoke_refresh_token(self, refresh_token_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE refresh_token_id = ? AND revoked_at IS NULL",
                (utc_now().isoformat(), refresh_token_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def revoke_device_sessions(self, device_id: str) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                "UPDATE access_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            conn.commit()

    def cancel_all_pending_pairings(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE pairing_sessions SET status = 'cancelled' WHERE status = 'pending'"
            )
            conn.commit()
            return cursor.rowcount

    def list_audit_events(self, *, limit: int = 20) -> list[AuditEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, event_type, occurred_at, actor_type, actor_id, detail_json
                FROM audit_events
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            AuditEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                actor_type=row["actor_type"],
                actor_id=row["actor_id"],
                detail=json.loads(row["detail_json"]),
            )
            for row in rows
        ]
