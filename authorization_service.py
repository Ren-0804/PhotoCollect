"""Signed silent authorization client.

This module has no Qt dependency and is safe to run in a QRunnable.  It uses a
small Ed25519 verifier so the packaged client does not need another native
crypto wheel.  Only the server's public key(s) are present on the client.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from authorization_config import (
    AUTH_API_URL,
    AUTH_CHECK_INTERVAL_SECONDS,
    AUTH_ENABLED,
    AUTH_LEASE_SECONDS,
    AUTH_PUBLIC_KEYS_B64,
    AUTH_REQUEST_TIMEOUT_SECONDS,
)
from device_identity import ClientIdentity, collect_identity

log = logging.getLogger(__name__)

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)
_BY = (4 * pow(5, _Q - 2, _Q)) % _Q
_IDENTITY = (0, 1)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = (x * _I) % _Q
    if x & 1:
        x = _Q - x
    return x


_BX = _xrecover(_BY)
_BASE = (_BX, _BY)


def _on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    # Twisted Edwards curve: -x^2 + y^2 = 1 + d x^2 y^2 (mod q).
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _Q == 0


def _edwards_add(P: tuple[int, int], Q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    denominator_x = pow(1 + _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    denominator_y = pow(1 - _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    return (
        ((x1 * y2 + x2 * y1) * denominator_x) % _Q,
        ((y1 * y2 + x1 * x2) * denominator_y) % _Q,
    )


def _scalarmult(P: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = _IDENTITY
    addend = P
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _decode_point(encoded: bytes) -> tuple[int, int]:
    if len(encoded) != 32:
        raise ValueError("Ed25519 点长度错误")
    value = int.from_bytes(encoded, "little")
    y = value & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("Ed25519 点坐标越界")
    x = _xrecover(y)
    if (x & 1) != ((value >> 255) & 1):
        x = _Q - x
    point = (x, y)
    # Reject anything that is not actually a point on the curve, then reject the
    # small-order subgroup.
    if not _on_curve(point):
        raise ValueError("Ed25519 点不在曲线上")
    if _scalarmult(point, 8) == _IDENTITY:
        raise ValueError("Ed25519 点无效")
    return point


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature using only Python's standard library."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        R = _decode_point(signature[:32])
        A = _decode_point(public_key)
        S = int.from_bytes(signature[32:], "little")
        if S >= _L:
            return False
        digest = __import__("hashlib").sha512(signature[:32] + public_key + message).digest()
        h = int.from_bytes(digest, "little") % _L
        return _scalarmult(_BASE, S) == _edwards_add(R, _scalarmult(A, h))
    except (ValueError, OverflowError):
        return False


def _b64decode(value: str) -> bytes:
    value = str(value or "")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class AuthStatus(str, Enum):
    DISABLED = "disabled"
    ACTIVE_ONLINE = "active_online"
    ACTIVE_OFFLINE_GRACE = "active_offline_grace"
    REQUIRES_ONLINE = "requires_online"
    DISABLED_BY_ADMIN = "disabled_by_admin"
    BANNED = "banned"
    INVALID = "invalid"


@dataclass(frozen=True)
class AuthorizationDecision:
    status: AuthStatus
    allowed: bool
    message: str = ""
    payload: dict[str, Any] | None = None


class AuthorizationService:
    """Check and persist a signed authorization lease."""

    def __init__(
        self,
        config_path: Path,
        *,
        api_url: str = AUTH_API_URL,
        public_key_b64: str | None = None,
        public_keys_b64: list[str] | None = None,
        enabled: bool = AUTH_ENABLED,
        clock=time.time,
    ):
        self.config_path = Path(config_path)
        self.state_path = self.config_path.with_name("auth_state.json")
        self.api_url = str(api_url or "").rstrip("/")
        # Accept both the historical single-key argument and the new list, and
        # fall back to the configured keys when neither is given.
        keys: list[str] = []
        for candidate in [public_key_b64, *(public_keys_b64 or [])]:
            text = str(candidate or "").strip()
            if text and text not in keys:
                keys.append(text)
        if not keys and public_key_b64 is None and public_keys_b64 is None:
            keys = list(AUTH_PUBLIC_KEYS_B64)
        self.public_keys_b64 = keys
        self.enabled = bool(enabled and self.api_url and self.public_keys_b64)
        self.clock = clock
        self.last_decision = AuthorizationDecision(AuthStatus.DISABLED, True)

    # Backwards-compatible alias: older call sites read ``public_key_b64``.
    @property
    def public_key_b64(self) -> str:
        return self.public_keys_b64[0] if self.public_keys_b64 else ""

    @staticmethod
    def check_interval_seconds() -> int:
        return AUTH_CHECK_INTERVAL_SECONDS

    def seconds_until_next_check(self) -> int:
        # A shared window lets Neon suspend between checks even when clients
        # start at different times. Fifteen simultaneous requests are small.
        period = self.check_interval_seconds()
        now = int(self.clock())
        offset = 30
        next_at = ((now - offset) // period + 1) * period + offset
        return max(1, next_at - now)

    def cached_decision(self, min_valid_seconds: int = 0) -> AuthorizationDecision | None:
        """Use a signed local lease at startup when it outlasts the next check."""
        if not self.enabled:
            return AuthorizationDecision(AuthStatus.DISABLED, True)
        identity = collect_identity(self.config_path.parent)
        decision = self._offline_decision(identity)
        if not decision.allowed or not decision.payload:
            return None
        expires_at = int(decision.payload["expires_at"])
        state = self._read_state()
        if not state:
            return None
        offline_until = int(state.get("saved_at", 0)) + AUTH_LEASE_SECONDS
        valid_until = min(expires_at, offline_until)
        if valid_until <= int(self.clock()) + max(0, min_valid_seconds) + 60:
            return None
        self.last_decision = decision
        return decision

    def _read_state(self) -> dict[str, Any] | None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _write_state(self, response: dict[str, Any], payload: dict[str, Any]) -> None:
        state = {
            "payload": response["payload"],
            "signature": response["signature"],
            "key_id": response.get("key_id", ""),
            "saved_at": int(self.clock()),
            "server_time": int(payload.get("issued_at", self.clock())),
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            # Saving the lease is best effort. A file held by antivirus or an
            # indexer must not turn a valid signed decision into an exception,
            # which the GUI treats as "not authorized". This check's decision
            # still comes from the signed response.
            log.warning("Could not save authorization lease: %s", exc)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _decode_signed(self, response: dict[str, Any], identity: ClientIdentity) -> dict[str, Any]:
        payload_b64 = str(response.get("payload", ""))
        signature_b64 = str(response.get("signature", ""))
        payload_bytes = _b64decode(payload_b64)
        signature = _b64decode(signature_b64)
        verified = False
        for key_b64 in self.public_keys_b64:
            try:
                if verify_ed25519(_b64decode(key_b64), payload_bytes, signature):
                    verified = True
                    break
            except (ValueError, base64.binascii.Error):
                continue
        if not verified:
            raise ValueError("授权签名验证失败")
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("授权内容格式错误")
        if payload.get("device_id") != identity.device_id or payload.get("user_sid_hash") != identity.user_sid_hash:
            raise ValueError("授权设备身份不匹配")
        status = str(payload.get("status", ""))
        if status not in {"active", "disabled", "banned"}:
            raise ValueError("授权状态未知")
        return payload

    def _lease_decision(self, payload: dict[str, Any], *, online: bool) -> AuthorizationDecision:
        status = str(payload.get("status"))
        if status == "disabled":
            return AuthorizationDecision(AuthStatus.DISABLED_BY_ADMIN, False, "软件已过期，请联系管理员。", payload)
        if status == "banned":
            return AuthorizationDecision(AuthStatus.BANNED, False, "软件已过期，请联系管理员。", payload)
        try:
            expires_at = int(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            return AuthorizationDecision(AuthStatus.INVALID, False, "授权期限无效。", payload)
        now = int(self.clock())
        if now > expires_at:
            return AuthorizationDecision(AuthStatus.REQUIRES_ONLINE, False, "授权已过期，请连接网络后重试。", payload)
        return AuthorizationDecision(
            AuthStatus.ACTIVE_ONLINE if online else AuthStatus.ACTIVE_OFFLINE_GRACE,
            True,
            payload=payload,
        )

    def _offline_decision(self, identity: ClientIdentity) -> AuthorizationDecision:
        state = self._read_state()
        if not state:
            return AuthorizationDecision(AuthStatus.REQUIRES_ONLINE, False, "首次使用需要连接网络。")
        try:
            saved_at = int(state.get("saved_at", 0))
            server_time = int(state.get("server_time", 0))
            now = int(self.clock())
            if server_time and now < server_time - 300:
                return AuthorizationDecision(AuthStatus.INVALID, False, "系统时间异常，请校准后重试。")
            response = {"payload": state["payload"], "signature": state["signature"], "key_id": state.get("key_id", "")}
            payload = self._decode_signed(response, identity)
            if now - saved_at > AUTH_LEASE_SECONDS:
                return AuthorizationDecision(AuthStatus.REQUIRES_ONLINE, False, "离线授权已过期，请连接网络。", payload)
            return self._lease_decision(payload, online=False)
        except (KeyError, ValueError, TypeError, base64.binascii.Error) as exc:
            log.info("Offline authorization unavailable: %s", exc)
            return AuthorizationDecision(AuthStatus.INVALID, False, "本地授权无效，请连接网络重试。")

    def _request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        # Vercel exposes the serverless handlers below ``/api``.  Keep the
        # configured base URL friendly for operators (the production value is
        # just the site origin), while also accepting an explicitly configured
        # ``.../api`` URL for staging and local tests.
        api_base = self.api_url.rstrip("/")
        if not api_base.lower().endswith("/api"):
            api_base = f"{api_base}/api"
        request = urllib.request.Request(
            f"{api_base}/{endpoint.lstrip('/')}",
            data=_canonical_json(body),
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "PhotoCollect/authorization"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=AUTH_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # A real HTTP status with a (possibly signed) body.
            try:
                parsed = json.loads(exc.read().decode("utf-8"))
            except (OSError, ValueError):
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            parsed["_http_status"] = exc.code
            return parsed
        except (urllib.error.URLError, socket.timeout, TimeoutError, http.client.HTTPException, ConnectionError, OSError) as exc:
            # Any transport-level problem (DNS, refused, reset, read timeout,
            # TLS, captive portal that drops the socket). The caller must treat
            # this as "server unreachable" and fall back to the saved lease,
            # never as an authorization failure.
            log.info("Authorization service unreachable: %s", exc)
            return {"_network_error": str(exc) or exc.__class__.__name__}
        try:
            # Decode here, not inside the transport try: UnicodeDecodeError is a
            # ValueError and must land in this branch.
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            # 2xx with a body that is not UTF-8 JSON (e.g. a proxy or
            # captive-portal HTML page, often GBK-encoded). Not a valid lease and
            # not an authorization failure.
            return {"_network_error": "non_json_response"}
        return parsed if isinstance(parsed, dict) else {"_network_error": "unexpected_response"}

    def check(self, app_version: str) -> AuthorizationDecision:
        if not self.enabled:
            self.last_decision = AuthorizationDecision(AuthStatus.DISABLED, True)
            return self.last_decision
        identity = collect_identity(self.config_path.parent)
        body = identity.as_request(app_version)
        endpoint = "heartbeat" if self._read_state() else "register"
        response = self._request(endpoint, body)

        # Server unreachable or an unusable (non-JSON) response: keep running on
        # the saved signed lease within its offline grace. This is what keeps a
        # working installation alive through a flaky network, a slow cold start,
        # a company proxy, or a rate-limited edge response.
        if response.get("_network_error"):
            self.last_decision = self._offline_decision(identity)
            return self.last_decision

        try:
            payload = self._decode_signed(response, identity)
        except (ValueError, TypeError, KeyError, base64.binascii.Error):
            http_status = response.get("_http_status")
            if isinstance(http_status, int) and http_status == 404 and endpoint == "heartbeat":
                # A server-side database reset or device purge should not strand
                # a valid installation. Re-enrol, but keep the existing lease
                # until the new registration actually succeeds, so a failed
                # re-registration falls back to offline grace instead of closing.
                register_response = self._request("register", body)
                if not register_response.get("_network_error"):
                    try:
                        register_payload = self._decode_signed(register_response, identity)
                        self._write_state(register_response, register_payload)
                        self.last_decision = self._lease_decision(register_payload, online=True)
                        return self.last_decision
                    except (ValueError, TypeError, KeyError, base64.binascii.Error):
                        pass
                self.last_decision = self._offline_decision(identity)
                return self.last_decision
            # Any other unsigned, unparseable, or unexpected server reply (400,
            # 401, 403-without-signature, 5xx, malformed body) must not close a
            # working installation: fall back to the saved lease.
            self.last_decision = self._offline_decision(identity)
            return self.last_decision

        # A validly signed lease is authoritative, including an admin
        # disable/ban, which blocks immediately.
        self._write_state(response, payload)
        self.last_decision = self._lease_decision(payload, online=True)
        return self.last_decision

    def ensure_allowed(self, app_version: str) -> AuthorizationDecision:
        decision = self.check(app_version)
        if not decision.allowed:
            raise PermissionError(decision.message or "软件已过期，请联系管理员。")
        return decision
