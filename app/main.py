import os
import time

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app import banner, checks, jobs, scheduler, secrets_store, security, state, workflow
from app.security import auth, page_auth, same_origin

secrets_store.apply_env()  # stored credentials override the defaults from the stack
state.ensure_seed()
jobs.load_last()
scheduler.start_loop()
banner.start_up()
app = FastAPI(title="xvids sorter")
app.include_router(workflow.router)
UI = open(os.path.join(os.path.dirname(__file__), "ui.html"), encoding="utf-8").read()
LOGIN = open(os.path.join(os.path.dirname(__file__), "login.html"), encoding="utf-8").read()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"


def _set_cookie(resp, token: str):
    resp.set_cookie(security.COOKIE, token, max_age=security.SESSION_TTL, httponly=True, samesite="lax", secure=COOKIE_SECURE)


class Cred(BaseModel):
    username: str | None = None
    password: str


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    return checks.all_checks()


@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings():
    return secrets_store.summary()


@app.post("/api/settings/{kind}", dependencies=[Depends(auth), Depends(same_origin)])
def set_settings(kind: str, body: Cred):
    """Replace the stored login for the file server or StashDB. It is tested first, so a typo never replaces a working login."""
    if kind not in ("smb", "stash"):
        raise HTTPException(status_code=404)
    user = (body.username or secrets_store.username(kind)).strip()
    if not user or not body.password:
        raise HTTPException(status_code=400, detail="username and password are required")
    ok, detail = checks.test_login(kind, user, body.password)
    if not ok:
        raise HTTPException(status_code=400, detail=f"not saved - {detail}")
    secrets_store.save(kind, user, body.password)
    secrets_store.apply_env()
    return {"ok": True, "detail": f"saved - {detail}"}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
def index():
    return UI


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if security.is_authed(request):
        return RedirectResponse("/", status_code=303)
    return LOGIN.replace("__ERROR__", "")


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if security.throttled(ip):
        return HTMLResponse(LOGIN.replace("__ERROR__", "Too many attempts - wait a few minutes and try again."), status_code=429)
    if not security.check_password(password):
        security.record_fail(ip)
        time.sleep(0.3)  # slow down guessing a little
        return HTMLResponse(LOGIN.replace("__ERROR__", "Wrong password."), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    _set_cookie(resp, security.create_session())
    return resp


@app.post("/logout", dependencies=[Depends(same_origin)])
def logout(request: Request):
    security.destroy_session(request.cookies.get(security.COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(security.COOKIE)
    return resp
