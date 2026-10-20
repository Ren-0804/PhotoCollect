import base64
import json
from unittest.mock import patch

import authorization_service
from device_identity import ClientIdentity
from authorization_service import (
    AuthStatus,
    AuthorizationService,
    verify_ed25519,
)


def test_ed25519_rfc8032_vector_is_verified():
    public_key = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert verify_ed25519(public_key, b"", signature)


def test_auth_is_backward_compatible_until_server_is_configured(tmp_path):
    service = AuthorizationService(
        tmp_path / "config.json",
        api_url="",
        public_key_b64="",
        enabled=False,
    )
    decision = service.check("test")
    assert decision.status is AuthStatus.DISABLED
    assert decision.allowed is True


def test_invalid_local_lease_never_allows_offline_use(tmp_path):
    service = AuthorizationService(
        tmp_path / "config.json",
        api_url="https://license.invalid",
        public_key_b64=base64.urlsafe_b64encode(b"x" * 32).decode(),
        enabled=True,
    )
    service.state_path.write_text(
        json.dumps({"payload": "bad", "signature": "bad", "saved_at": 1}),
        encoding="utf-8",
    )
    identity = type("Identity", (), {"device_id": "d", "user_sid_hash": "s"})()
    decision = service._offline_decision(identity)
    assert decision.status is AuthStatus.INVALID
    assert decision.allowed is False


def test_auth_request_targets_vercel_api_route(tmp_path):
    service = AuthorizationService(
        tmp_path / "config.json",
        api_url="https://license.example.test",
        public_key_b64=base64.urlsafe_b64encode(b"x" * 32).decode(),
        enabled=True,
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    with patch.object(authorization_service.urllib.request, "urlopen", return_value=Response()) as urlopen:
        service._request("register", {"device_id": "test"})

    assert urlopen.call_args.args[0].full_url == "https://license.example.test/api/register"


def _cached_service(tmp_path, *, now=100_000, expires_in=24 * 60 * 60):
    identity = ClientIdentity("pc", "user", "s" * 64, "d" * 64)
    service = AuthorizationService(
        tmp_path / "config.json",
        api_url="https://license.example.test",
        public_key_b64=base64.urlsafe_b64encode(b"x" * 32).decode(),
        enabled=True,
        clock=lambda: now,
    )
    payload = {
        "device_id": identity.device_id,
        "user_sid_hash": identity.user_sid_hash,
        "status": "active",
        "issued_at": now,
        "expires_at": now + expires_in,
    }
    service.state_path.write_text(
        json.dumps({
            "payload": base64.urlsafe_b64encode(json.dumps(payload).encode()).decode(),
            "signature": base64.urlsafe_b64encode(b"s" * 64).decode(),
            "saved_at": now,
            "server_time": now,
        }),
        encoding="utf-8",
    )
    return service, identity


def test_authorization_checks_share_two_hour_window(tmp_path):
    service = AuthorizationService(tmp_path / "config.json", clock=lambda: 0)
    assert service.seconds_until_next_check() == 30
    service.clock = lambda: 31
    assert service.seconds_until_next_check() == 2 * 60 * 60 - 1
    service.clock = lambda: 2 * 60 * 60
    assert service.seconds_until_next_check() == 30


def test_signed_24_hour_lease_can_skip_startup_request(tmp_path):
    service, identity = _cached_service(tmp_path)
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ), patch.object(service, "_request", side_effect=AssertionError("unexpected network request")):
        decision = service.cached_decision(service.seconds_until_next_check())
    assert decision is not None
    assert decision.status is AuthStatus.ACTIVE_OFFLINE_GRACE
    assert decision.allowed


def test_old_short_lease_requires_online_refresh_at_startup(tmp_path):
    service, identity = _cached_service(tmp_path, expires_in=60 * 60)
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ):
        assert service.cached_decision(2 * 60 * 60) is None


def test_expiring_offline_grace_requires_online_refresh(tmp_path):
    service, identity = _cached_service(tmp_path)
    service.clock = lambda: 100_000 + 24 * 60 * 60 - 60 * 60
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ):
        assert service.cached_decision(2 * 60 * 60) is None


def test_server_503_uses_valid_signed_offline_lease(tmp_path):
    service, identity = _cached_service(tmp_path)
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ), patch.object(service, "_request", return_value={"_http_status": 503, "error": "service_unavailable"}):
        decision = service.check("test")
    assert decision.status is AuthStatus.ACTIVE_OFFLINE_GRACE
    assert decision.allowed


def test_online_admin_disable_overrides_cached_lease(tmp_path):
    service, identity = _cached_service(tmp_path)
    payload = {
        "device_id": identity.device_id,
        "user_sid_hash": identity.user_sid_hash,
        "status": "disabled",
        "issued_at": 100_000,
        "expires_at": 100_000 + 24 * 60 * 60,
    }
    response = {
        "payload": base64.urlsafe_b64encode(json.dumps(payload).encode()).decode(),
        "signature": base64.urlsafe_b64encode(b"s" * 64).decode(),
        "_http_status": 403,
    }
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ), patch.object(service, "_request", return_value=response):
        decision = service.check("test")
    assert decision.status is AuthStatus.DISABLED_BY_ADMIN
    assert not decision.allowed


# --- Added 2026-10-19: resilience, re-registration order, key rotation, frozen lockdown ---

def _bind(payload_status, identity, now=100_000, expires_in=24 * 60 * 60):
    payload = {
        "issuer": "PhotoCollect", "key_id": "k",
        "device_id": identity.device_id, "user_sid_hash": identity.user_sid_hash,
        "status": payload_status, "issued_at": now, "expires_at": now + expires_in,
    }
    return {
        "payload": base64.urlsafe_b64encode(json.dumps(payload).encode()).decode(),
        "signature": base64.urlsafe_b64encode(b"s" * 64).decode(),
    }


def test_network_error_keeps_working_installation_online_grace(tmp_path):
    service, identity = _cached_service(tmp_path)
    for network_error in ("timed out", "non_json_response", "[Errno 111] Connection refused"):
        with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
            authorization_service, "verify_ed25519", return_value=True
        ), patch.object(service, "_request", return_value={"_network_error": network_error}):
            decision = service.check("test")
        assert decision.status is AuthStatus.ACTIVE_OFFLINE_GRACE, network_error
        assert decision.allowed, network_error


def test_unsigned_error_response_does_not_close_working_installation(tmp_path):
    service, identity = _cached_service(tmp_path)
    for status in (400, 401, 429, 500):
        with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
            authorization_service, "verify_ed25519", return_value=True
        ), patch.object(service, "_request", return_value={"error": "x", "_http_status": status}):
            decision = service.check("test")
        assert decision.allowed, status
        assert decision.status is AuthStatus.ACTIVE_OFFLINE_GRACE, status


def test_failed_reregistration_preserves_local_lease(tmp_path):
    service, identity = _cached_service(tmp_path)

    def fake_request(endpoint, body):
        if endpoint == "heartbeat":
            return {"error": "client_not_registered", "_http_status": 404}
        return {"_network_error": "refused"}

    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ), patch.object(service, "_request", side_effect=fake_request):
        decision = service.check("test")
    assert decision.status is AuthStatus.ACTIVE_OFFLINE_GRACE
    assert decision.allowed
    assert service.state_path.is_file()


def test_successful_reregistration_after_404_rebinds(tmp_path):
    service, identity = _cached_service(tmp_path)

    def fake_request(endpoint, body):
        if endpoint == "heartbeat":
            return {"error": "client_not_registered", "_http_status": 404}
        return _bind("active", identity)

    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ), patch.object(service, "_request", side_effect=fake_request):
        decision = service.check("test")
    assert decision.status is AuthStatus.ACTIVE_ONLINE
    assert decision.allowed


def test_lease_signed_by_rotated_key_is_accepted(tmp_path):
    key_old = base64.urlsafe_b64encode(b"a" * 32).decode()
    key_new = base64.urlsafe_b64encode(b"b" * 32).decode()
    identity = ClientIdentity("pc", "user", "s" * 64, "d" * 64)
    service = AuthorizationService(
        tmp_path / "config.json", api_url="https://license.example.test",
        public_keys_b64=[key_old, key_new], enabled=True, clock=lambda: 100_000,
    )
    only_new = lambda pk, msg, sig: pk == b"b" * 32
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", side_effect=only_new
    ), patch.object(service, "_request", return_value=_bind("active", identity)):
        decision = service.check("test")
    assert decision.status is AuthStatus.ACTIVE_ONLINE
    assert decision.allowed


def test_lease_signed_by_untrusted_key_is_rejected(tmp_path):
    key_old = base64.urlsafe_b64encode(b"a" * 32).decode()
    identity = ClientIdentity("pc", "user", "s" * 64, "d" * 64)
    service = AuthorizationService(
        tmp_path / "config.json", api_url="https://license.example.test",
        public_keys_b64=[key_old], enabled=True, clock=lambda: 100_000,
    )
    only_new = lambda pk, msg, sig: pk == b"b" * 32
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", side_effect=only_new
    ), patch.object(service, "_request", return_value=_bind("active", identity)):
        decision = service.check("test")
    assert not decision.allowed


class _BodyResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_non_utf8_success_body_is_a_network_error_not_a_crash(tmp_path):
    # A captive portal or company proxy answering 200 with a GBK HTML page.
    service, identity = _cached_service(tmp_path)
    portal = "<html><body>请先登录上网认证</body></html>".encode("gbk")
    with patch.object(authorization_service.urllib.request, "urlopen", return_value=_BodyResponse(portal)):
        assert service._request("heartbeat", {}) == {"_network_error": "non_json_response"}
        with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
            authorization_service, "verify_ed25519", return_value=True
        ):
            decision = service.check("test")
    assert decision.status is AuthStatus.ACTIVE_OFFLINE_GRACE
    assert decision.allowed


def test_lease_save_failure_keeps_signed_online_decision(tmp_path):
    service, identity = _cached_service(tmp_path)
    before = service.state_path.read_text(encoding="utf-8")
    with patch.object(authorization_service, "collect_identity", return_value=identity), patch.object(
        authorization_service, "verify_ed25519", return_value=True
    ), patch.object(service, "_request", return_value=_bind("active", identity)), patch.object(
        authorization_service.os, "replace", side_effect=PermissionError(32, "file in use")
    ):
        decision = service.check("test")
    assert decision.status is AuthStatus.ACTIVE_ONLINE
    assert decision.allowed
    assert service.state_path.read_text(encoding="utf-8") == before
    assert not service.state_path.with_suffix(".tmp").exists()


def test_frozen_executable_ignores_environment_overrides():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent
    while not (root / "authorization_config.py").exists() and root != root.parent:
        root = root.parent
    code = (
        "import sys; sys.frozen = True;"
        "import authorization_config as c;"
        "print(c.AUTH_API_URL);"
        "print(int(c.AUTH_ENABLED));"
        "print(','.join(c.AUTH_PUBLIC_KEYS_B64))"
    )
    env = dict(
        os.environ,
        PHOTOCOLLECT_AUTH_API_URL="https://attacker.example",
        PHOTOCOLLECT_AUTH_PUBLIC_KEY="AAAA",
        PHOTOCOLLECT_AUTH_ENABLED="0",
    )
    out = subprocess.check_output([sys.executable, "-c", code], cwd=str(root), text=True, env=env).splitlines()
    assert out[0] == "https://license-api-steel.vercel.app"
    assert out[1] == "1"
    assert out[2] == "1IHKz3c3n2QkKY6j/3Tf/yAUTythA7aiTCvLqTyFpDk="
