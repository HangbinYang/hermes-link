from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from hmac import compare_digest

from hermes_link.constants import PAIRING_CODE_ALPHABET, PAIRING_CODE_LENGTH
from hermes_link.i18n import t
from hermes_link.models import (
    AuthenticatedDevice,
    IssuedAccessToken,
    IssuedRefreshToken,
    LinkConfig,
    PairingSession,
    utc_now,
)
from hermes_link.network import is_loopback_host
from hermes_link.runtime import generate_id
from hermes_link.storage import LinkRepository


class SecurityError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        self.message = t(f"security.{code}")
        super().__init__(self.message)


def normalize_pairing_code(value: str) -> str:
    return value.upper().strip()


def generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def hash_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_access_token() -> tuple[str, str]:
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    return prefix, f"hlk_{prefix}_{secret}"


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_refresh_token() -> str:
    return f"hlkr_{secrets.token_urlsafe(32)}"


def has_required_scopes(granted: list[str], required: list[str]) -> bool:
    if "admin" in granted:
        return True
    granted_set = set(granted)
    return all(scope in granted_set for scope in required)


class SecurityManager:
    def __init__(self, repository: LinkRepository, config: LinkConfig):
        self.repository = repository
        self.config = config

    def create_pairing_session(self, *, scopes: list[str] | None = None, note: str | None = None) -> PairingSession:
        self.repository.expire_stale_pairings()
        if self.repository.count_pending_pairings() >= self.config.security.max_active_pairing_sessions:
            raise SecurityError("too_many_active_pairing_sessions")

        session = self.repository.create_pairing_session(
            session_id=generate_id("pair_"),
            code=generate_pairing_code(),
            expires_at=utc_now() + timedelta(minutes=self.config.security.pairing_code_ttl_minutes),
            scopes=scopes or list(self.config.security.default_device_scopes),
            note=note,
        )
        self.repository.append_audit_event(
            "pairing.session.created",
            actor_type="local_cli",
            detail={"session_id": session.session_id, "scopes": session.scopes},
        )
        return session

    def claim_pairing_session(
        self,
        *,
        session_id: str,
        code: str,
        device_label: str,
        device_platform: str,
    ) -> tuple[PairingSession, AuthenticatedDevice, IssuedAccessToken, IssuedRefreshToken]:
        token_prefix, token = issue_access_token()
        token_id = generate_id("tok_")
        device_id = generate_id("dev_")
        token_hash = hash_access_token(token)
        refresh_token = issue_refresh_token()
        refresh_token_id = generate_id("rtok_")
        refresh_token_hash = hash_refresh_token(refresh_token)

        try:
            device, token_record, refresh_token_record = self.repository.claim_pairing_session(
                session_id=session_id,
                normalized_code=normalize_pairing_code(code),
                device_id=device_id,
                device_label=device_label,
                device_platform=device_platform,
                token_id=token_id,
                token_prefix=token_prefix,
                token_hash=token_hash,
                token_expires_at=utc_now() + timedelta(minutes=self.config.security.access_token_ttl_minutes),
                refresh_token_id=refresh_token_id,
                refresh_token_hash=refresh_token_hash,
                refresh_token_expires_at=utc_now() + timedelta(days=self.config.security.refresh_token_ttl_days),
            )
        except ValueError as exc:
            raise SecurityError(str(exc)) from exc

        session = self.repository.get_pairing_session(session_id)
        if session is None:
            raise SecurityError("pairing_session_missing_after_claim")

        self.repository.append_audit_event(
            "pairing.session.claimed",
            actor_type="remote_device",
            actor_id=device_id,
            detail={"session_id": session_id, "device_label": device_label, "device_platform": device_platform},
        )

        issued = IssuedAccessToken(
            token_id=token_record.token_id,
            token=token,
            token_prefix=token_record.token_prefix,
            device_id=device.device_id,
            scopes=token_record.scopes,
            created_at=token_record.created_at,
            expires_at=token_record.expires_at,
        )
        authenticated = AuthenticatedDevice(
            device=device,
            token_id=token_record.token_id,
            token_expires_at=token_record.expires_at,
            refresh_token_id=refresh_token_record.refresh_token_id,
            refresh_token_expires_at=refresh_token_record.expires_at,
        )
        issued_refresh = IssuedRefreshToken(
            refresh_token_id=refresh_token_record.refresh_token_id,
            token=refresh_token,
            device_id=device.device_id,
            created_at=refresh_token_record.created_at,
            expires_at=refresh_token_record.expires_at,
        )
        return session, authenticated, issued, issued_refresh

    def authenticate_bearer(self, bearer_token: str, *, required_scopes: list[str] | None = None) -> AuthenticatedDevice:
        validated = self.repository.validate_access_token(token_hash=hash_access_token(bearer_token))
        if validated is None:
            raise SecurityError("access_token_invalid")
        device, token = validated
        if required_scopes and not has_required_scopes(token.scopes, required_scopes):
            raise SecurityError("access_token_scope_denied")
        refresh_token = self.repository.get_active_refresh_token_for_device(device.device_id)
        return AuthenticatedDevice(
            device=device,
            token_id=token.token_id,
            token_expires_at=token.expires_at,
            refresh_token_id=refresh_token.refresh_token_id if refresh_token else None,
            refresh_token_expires_at=refresh_token.expires_at if refresh_token else None,
        )

    def authenticate_refresh_token(self, refresh_token: str, *, required_scopes: list[str] | None = None) -> AuthenticatedDevice:
        validated = self.repository.validate_refresh_token(token_hash=hash_refresh_token(refresh_token))
        if validated is None:
            raise SecurityError("refresh_token_invalid")
        device, token = validated
        if required_scopes and not has_required_scopes(device.scopes, required_scopes):
            raise SecurityError("access_token_scope_denied")
        return AuthenticatedDevice(
            device=device,
            token_id=None,
            token_expires_at=None,
            refresh_token_id=token.refresh_token_id,
            refresh_token_expires_at=token.expires_at,
        )

    def rotate_access_token(self, authenticated: AuthenticatedDevice) -> IssuedAccessToken:
        if authenticated.token_id is None:
            raise SecurityError("access_token_invalid")
        active_refresh_token = self.repository.get_active_refresh_token_for_device(authenticated.device.device_id)
        if active_refresh_token is None:
            raise SecurityError("device_session_not_renewable")
        token_prefix, token = issue_access_token()
        token_id = generate_id("tok_")
        rotated = self.repository.rotate_access_token(
            current_token_id=authenticated.token_id,
            new_token_id=token_id,
            token_prefix=token_prefix,
            token_hash=hash_access_token(token),
            expires_at=utc_now() + timedelta(minutes=self.config.security.access_token_ttl_minutes),
        )
        if rotated is None:
            raise SecurityError("access_token_invalid")

        device, token_record = rotated
        self.repository.append_audit_event(
            "access_token.rotated",
            actor_type="remote_device",
            actor_id=device.device_id,
            detail={"replaced_token_id": authenticated.token_id, "new_token_id": token_record.token_id},
        )
        return IssuedAccessToken(
            token_id=token_record.token_id,
            token=token,
            token_prefix=token_record.token_prefix,
            device_id=device.device_id,
            scopes=token_record.scopes,
            created_at=token_record.created_at,
            expires_at=token_record.expires_at,
        )

    def refresh_device_session(
        self,
        *,
        authenticated: AuthenticatedDevice | None = None,
        refresh_token: str | None = None,
    ) -> tuple[AuthenticatedDevice, IssuedAccessToken, IssuedRefreshToken]:
        refresh_authenticated: AuthenticatedDevice | None = None
        if refresh_token:
            refresh_authenticated = self.authenticate_refresh_token(refresh_token)
        if (
            authenticated is not None
            and refresh_authenticated is not None
            and authenticated.device.device_id != refresh_authenticated.device.device_id
        ):
            raise SecurityError("session_device_mismatch")

        subject = authenticated or refresh_authenticated
        if subject is None:
            raise SecurityError("refresh_token_missing")

        token_prefix, access_token = issue_access_token()
        access_token_id = generate_id("tok_")
        refresh_token_value = issue_refresh_token()
        refresh_token_id = generate_id("rtok_")
        rotated = self.repository.rotate_device_session(
            device_id=subject.device.device_id,
            current_token_id=authenticated.token_id if authenticated else None,
            current_refresh_token_id=refresh_authenticated.refresh_token_id if refresh_authenticated else None,
            new_access_token_id=access_token_id,
            new_access_token_prefix=token_prefix,
            new_access_token_hash=hash_access_token(access_token),
            new_access_token_expires_at=utc_now() + timedelta(minutes=self.config.security.access_token_ttl_minutes),
            new_refresh_token_id=refresh_token_id,
            new_refresh_token_hash=hash_refresh_token(refresh_token_value),
            new_refresh_token_expires_at=utc_now() + timedelta(days=self.config.security.refresh_token_ttl_days),
        )
        if rotated is None:
            raise SecurityError("refresh_token_invalid" if refresh_authenticated else "access_token_invalid")

        device, access_record, refresh_record = rotated
        self.repository.append_audit_event(
            "device.session.refreshed",
            actor_type="remote_device",
            actor_id=device.device_id,
            detail={
                "replaced_token_id": authenticated.token_id if authenticated else None,
                "replaced_refresh_token_id": refresh_authenticated.refresh_token_id if refresh_authenticated else None,
                "new_token_id": access_record.token_id,
                "new_refresh_token_id": refresh_record.refresh_token_id,
            },
        )
        next_authenticated = AuthenticatedDevice(
            device=device,
            token_id=access_record.token_id,
            token_expires_at=access_record.expires_at,
            refresh_token_id=refresh_record.refresh_token_id,
            refresh_token_expires_at=refresh_record.expires_at,
        )
        issued_access = IssuedAccessToken(
            token_id=access_record.token_id,
            token=access_token,
            token_prefix=access_record.token_prefix,
            device_id=device.device_id,
            scopes=access_record.scopes,
            created_at=access_record.created_at,
            expires_at=access_record.expires_at,
        )
        issued_refresh = IssuedRefreshToken(
            refresh_token_id=refresh_record.refresh_token_id,
            token=refresh_token_value,
            device_id=device.device_id,
            created_at=refresh_record.created_at,
            expires_at=refresh_record.expires_at,
        )
        return next_authenticated, issued_access, issued_refresh

    def revoke_device_session(self, authenticated: AuthenticatedDevice) -> bool:
        before = self.repository.device_has_active_session(authenticated.device.device_id)
        self.repository.revoke_device_sessions(authenticated.device.device_id)
        if before:
            self.repository.append_audit_event(
                "device.session.revoked",
                actor_type="remote_device",
                actor_id=authenticated.device.device_id,
                detail={
                    "token_id": authenticated.token_id,
                    "refresh_token_id": authenticated.refresh_token_id,
                },
            )
        return before

    def authenticate_internal_device(
        self,
        *,
        service_secret: str,
        device_id: str,
        client_host: str | None,
        required_scopes: list[str] | None = None,
    ) -> AuthenticatedDevice:
        if not client_host or not is_loopback_host(client_host):
            raise SecurityError("internal_request_not_loopback")
        if not compare_digest(service_secret, self.config.service_secret):
            raise SecurityError("internal_service_secret_invalid")
        device = self.repository.get_active_device(device_id)
        if device is None:
            raise SecurityError("internal_device_not_active")
        if not self.repository.device_has_active_session(device_id):
            raise SecurityError("internal_device_session_missing")
        if required_scopes and not has_required_scopes(device.scopes, required_scopes):
            raise SecurityError("access_token_scope_denied")
        return AuthenticatedDevice(device=device, token_id=None, token_expires_at=None)
