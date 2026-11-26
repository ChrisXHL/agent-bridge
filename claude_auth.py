"""Backup Claude Code token (local patch).

Normal runs use the browser login stored by `claude auth login`. When that login
is missing, expired, or a run fails authentication, Claude runs fall back to a
one-year token from `claude setup-token`, saved locally with owner-only access.
The token is never returned by the API, logged, exported, or written into the DB.
"""
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

LOG = logging.getLogger("codex_bridge.claude_auth")
TOKEN_FILE = "claude-fallback-token.json"
TOKEN_LIFETIME = 365 * 86400
STATUS_TTL = 60
LOGIN_FAIL_HOLD = 600        # after an auth failure, prefer the token for this long
EXPIRY_MARGIN = 300          # treat a login expiring within 5 minutes as expired
_AUTH_ERROR_MARKERS = ("oauth token has expired", "invalid api key", "please run /login", "not logged in",
                       "authentication_error", "invalid bearer token", "api error: 401", "login expired",
                       "invalid authentication credentials", "oauth token revoked")

_lock = threading.Lock()
_state_dir = None
_status_cache = (0.0, None)
_login_failed_at = 0.0


def configure(state_dir):
    global _state_dir
    _state_dir = Path(state_dir) if state_dir else None


def state_dir():
    if _state_dir:
        return _state_dir
    env = os.getenv("BRIDGE_STATE_DIR")
    return Path(env) if env else Path.home() / "Library/Application Support/CodexBridgeV2"


def token_path():
    return state_dir() / TOKEN_FILE


def _read_token():
    try:
        data = json.loads(token_path().read_text())
    except (OSError, ValueError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return data if isinstance(token, str) and token.strip() else None


def save_token(token):
    token = (token or "").strip()
    if not token.startswith("sk-ant-") or len(token) < 40 or any(c.isspace() for c in token):
        raise ValueError("invalid_token")
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "saved_at": time.time()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    LOG.info("claude fallback token saved")


def clear_token():
    try:
        token_path().unlink()
        LOG.info("claude fallback token removed")
    except FileNotFoundError:
        pass


def _claude_bin():
    from claude_runtime import resolve_claude_binary
    return resolve_claude_binary()


def login_status(force=False):
    """Stored browser login as reported by `claude auth status` (token env stripped)."""
    global _status_cache
    with _lock:
        checked, cached = _status_cache
        if cached is not None and not force and time.time() - checked < STATUS_TTL:
            return cached
    from claude_runtime import base_env
    status = {"logged_in": False, "expires_at": None, "checked": False}
    try:
        p = subprocess.run([_claude_bin(), "auth", "status"], capture_output=True, text=True, timeout=20,
                           env=base_env())
        value = json.loads(p.stdout)
        expires = value.get("loginExpiresAt")
        expires_ts = None
        if isinstance(expires, str):
            from datetime import datetime
            try:
                expires_ts = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
            except ValueError:
                expires_ts = None
        status = {"logged_in": bool(value.get("loggedIn")), "expires_at": expires_ts, "checked": True,
                  "method": value.get("authMethod"), "plan": value.get("subscriptionType")}
    except Exception as exc:
        LOG.warning("claude auth status unavailable: %s", type(exc).__name__)
    with _lock:
        _status_cache = (time.time(), status)
    return status


def login_usable(status=None):
    status = status or login_status()
    if not status.get("checked"):
        return True          # unknown: keep the normal login, auth errors still trigger fallback
    if not status.get("logged_in"):
        return False
    expires = status.get("expires_at")
    return not expires or expires - time.time() > EXPIRY_MARGIN


def mark_login_failed():
    global _login_failed_at, _status_cache
    with _lock:
        _login_failed_at = time.time()
        _status_cache = (0.0, None)


def fallback_token():
    """The token to inject now, or None to use the browser login."""
    data = _read_token()
    if not data:
        return None
    if time.time() - _login_failed_at < LOGIN_FAIL_HOLD or not login_usable():
        return data["token"]
    return None


def is_auth_error(text):
    text = (text or "").lower()
    return any(m in text for m in _AUTH_ERROR_MARKERS)


def summary():
    data = _read_token()
    status = login_status()
    usable = login_usable(status)
    saved_at = data.get("saved_at") if data else None
    failed_recently = time.time() - _login_failed_at < LOGIN_FAIL_HOLD
    return {
        "login": {"logged_in": status.get("logged_in"), "checked": status.get("checked"),
                  "expires_at": status.get("expires_at"), "usable": usable and not failed_recently,
                  "plan": status.get("plan")},
        "token": {"configured": bool(data), "saved_at": saved_at,
                  "expires_at": (saved_at + TOKEN_LIFETIME) if saved_at else None},
        "using_token": bool(data) and (not usable or failed_recently),
    }
