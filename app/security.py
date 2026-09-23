"""Login: a session cookie set by a forms POST to /login, instead of the browser's own Basic-auth popup.

Sessions are an in-memory token -> expiry map (a restart just means everyone has to log in again, which is fine for
a home box). A small per-IP failure counter throttles password guessing now that there's no browser popup slowing
it down.
"""
import hmac
import os
import secrets
import threading
import time

from fastapi import HTTPException, Request

COOKIE = "sid"
SESSION_TTL = 30 * 24 * 3600  # 30 days
FAIL_WINDOW = 300  # 5 minutes
FAIL_LIMIT = 10

_lock = threading.Lock()
_sessions: dict[str, float] = {}
_fails: dict[str, list] = {}


def check_password(pw: str) -> bool:
    return hmac.compare_digest(pw or "", os.environ.get("UI_PASSWORD", "\0"))


def throttled(ip: str) -> bool:
    now = time.time()
    with _lock:
        hist = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = hist
        return len(hist) >= FAIL_LIMIT


def record_fail(ip: str):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL
    return token


def destroy_session(token: str | None):
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def _valid(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            del _sessions[token]
            return False
        return True


def is_authed(request: Request) -> bool:
    return _valid(request.cookies.get(COOKIE))


def auth(request: Request):
    """API routes: a bare 401 when there's no valid session cookie - the front end sends the browser to /login."""
    if not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="login required")


def page_auth(request: Request):
    """The main page itself: redirect straight to the login form instead of a bare 401."""
    if not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def same_origin(request: Request):
    """Browsers attach the session cookie to cross-site requests, so state changes must carry a header a foreign page cannot set."""
    if request.headers.get("x-requested-with") != "xvids":
        raise HTTPException(status_code=403, detail="missing X-Requested-With header")
