import pytest

from hermes_link.runtime import bootstrap_runtime, set_runtime_home
from hermes_link.security import SecurityError, SecurityManager
from hermes_link.storage import LinkRepository


def test_pairing_session_claim_issues_access_token(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)

    session = security.create_pairing_session()
    claimed_session, authenticated, issued, refresh_issued = security.claim_pairing_session(
        session_id=session.session_id,
        code=session.code,
        device_label="iPhone",
        device_platform="ios",
    )

    assert claimed_session.status == "claimed"
    assert authenticated.device.label == "iPhone"
    assert issued.token.startswith("hlk_")
    assert refresh_issued.token.startswith("hlkr_")
    assert repository.count_active_devices() == 1
    assert "admin" in authenticated.device.scopes
    assert "devices:manage" in authenticated.device.scopes


def test_refresh_token_can_rotate_device_session(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)

    session = security.create_pairing_session()
    _, _, first_access, first_refresh = security.claim_pairing_session(
        session_id=session.session_id,
        code=session.code,
        device_label="iPhone",
        device_platform="ios",
    )

    authenticated, next_access, next_refresh = security.refresh_device_session(refresh_token=first_refresh.token)

    assert authenticated.device.device_id == first_access.device_id
    assert next_access.token != first_access.token
    assert next_refresh.token != first_refresh.token


def test_access_token_rotation_requires_an_active_refresh_session(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)

    session = security.create_pairing_session()
    _, authenticated, _, refresh_issued = security.claim_pairing_session(
        session_id=session.session_id,
        code=session.code,
        device_label="iPhone",
        device_platform="ios",
    )
    repository.revoke_refresh_token(refresh_issued.refresh_token_id)

    with pytest.raises(SecurityError) as exc_info:
        security.rotate_access_token(authenticated)

    assert exc_info.value.code == "device_session_not_renewable"
