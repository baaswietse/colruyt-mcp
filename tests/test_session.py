"""Session renewal without a browser (HTTP login chain, mocked), cookie store, attached mode,
and the bearer-token wrapper of the HTTP transport. No network, no browser."""
import json
import time

import httpx
import pytest

from colruyt_mcp import browser, config, session
from colruyt_mcp.server import TokenCheck
from colruyt_mcp.session import CookieStore, http_refresh

B2C = "https://login.myxtra.be/dcsxtraprod.onmicrosoft.com/B2C_1A_signin/oauth2/v2.0/authorize"
COGO_HOME_OK = '<html><script>var WCParamJS = {"loggedIn": true, "WCAuthToken": \'x\'};</script></html>'
LOGIN_UI = '<html><script>var SETTINGS = {"api":"CombinedSigninAndSignup"};</script></html>'


def store_with(tmp_path, cookies):
    s = CookieStore(tmp_path / "cookies.json")
    s.save(cookies)
    return s


def cookie(name, domain, value="v", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
            "httpOnly": True, "secure": True, "sameSite": "None"}


def sso_chain(sso_valid: bool, block: bool = False):
    """Fake the four-hop Collect&Go login chain. Records which cookies reached each host."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        seen.setdefault(host, set()).update(request.headers.get("cookie", "").split("; "))
        if host == "www.collectandgo.be" and path == "/login":
            return httpx.Response(302, headers={"location": f"{B2C}?client_id=1&state=s"})
        if host == "login.myxtra.be":
            if block:
                return httpx.Response(405, text="<html>Incapsula</html>")
            if not sso_valid:
                return httpx.Response(200, text=LOGIN_UI)
            return httpx.Response(302, headers={
                "location": "https://www.collectandgo.be/ColExtraTokenUpdate?state=s&code=c",
                "set-cookie": "x-ms-cpim-sso:dcsxtraprod.onmicrosoft.com_0=new; Domain=.login.myxtra.be; Path=/"})
        if host == "www.collectandgo.be" and path == "/ColExtraTokenUpdate":
            return httpx.Response(302, headers=[
                ("location", "https://www.collectandgo.be/nl/home"),
                ("set-cookie", "JSESSIONID=j1; Domain=.collectandgo.be; Path=/; HttpOnly"),
                ("set-cookie", "WC_AUTHENTICATION_1823679=a1; Domain=.collectandgo.be; Path=/; HttpOnly"),
                ("set-cookie", "cogo_user=u; Domain=.collectandgo.be; Path=/; Max-Age=5270400"),
            ])
        if host == "www.collectandgo.be" and path == "/nl/home":
            return httpx.Response(200, text=COGO_HOME_OK)
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


REAL_CLIENT = httpx.Client


@pytest.fixture
def mock_client(monkeypatch):
    """Route session.httpx.Client through a MockTransport (can be installed several times)."""
    def install(transport):
        def fake(**kw):
            kw.pop("transport", None)
            return REAL_CLIENT(transport=transport, **kw)
        monkeypatch.setattr(session.httpx, "Client", fake)
    return install


def test_http_refresh_reconnects_with_sso_cookie(tmp_path, mock_client):
    store = store_with(tmp_path, [
        cookie("x-ms-cpim-sso:dcsxtraprod.onmicrosoft.com_0", ".login.myxtra.be", "old", time.time() + 86400 * 20),
        cookie("reese84", ".collectandgo.be", "imperva", time.time() + 86400),
        cookie("clpbff_session", ".colruyt.be", "c", time.time() + 86400 * 50),
    ])
    transport, seen = sso_chain(sso_valid=True)
    mock_client(transport)
    result = http_refresh(store, ("cogo",))
    assert result["reconnected"] == {"cogo": True} and result["needs_manual_login"] is False
    assert result["method"] == "http" and result["cogo_session"] is True
    # the anti-bot cookie is never replayed; the SSO cookie only goes to the B2C host
    assert not any(c.startswith("reese84=") for c in seen["www.collectandgo.be"])
    assert any(c.startswith("x-ms-cpim-sso:") for c in seen["login.myxtra.be"])
    assert not any(c.startswith("x-ms-cpim-sso:") for c in seen["www.collectandgo.be"])
    names = {c["name"]: c["value"] for c in store.load()}
    assert names["JSESSIONID"] == "j1" and names["WC_AUTHENTICATION_1823679"] == "a1"
    assert names["x-ms-cpim-sso:dcsxtraprod.onmicrosoft.com_0"] == "new"  # re-set by B2C, persisted
    assert names["reese84"] == "imperva"  # untouched


def test_http_refresh_reports_expired_sso(tmp_path, mock_client):
    store = store_with(tmp_path, [cookie("x-ms-cpim-sso:t_0", ".login.myxtra.be", "old")])
    transport, _ = sso_chain(sso_valid=False)
    mock_client(transport)
    result = http_refresh(store, ("cogo",))
    assert result["reconnected"] == {"cogo": False} and result["needs_manual_login"] is True
    assert "expiré" in result["detail"]["cogo"]
    assert result["cogo_session"] is False


def test_http_refresh_reports_bot_block(tmp_path, mock_client):
    store = store_with(tmp_path, [cookie("x-ms-cpim-sso:t_0", ".login.myxtra.be", "old")])
    transport, _ = sso_chain(sso_valid=True, block=True)
    mock_client(transport)
    result = http_refresh(store, ("cogo",))
    assert result["reconnected"] == {"cogo": False} and "anti-bot HTTP 405" in result["detail"]["cogo"]


def test_auto_refresh_falls_back_to_browser_only_when_needed(tmp_path, mock_client, monkeypatch):
    calls = []
    monkeypatch.setattr(session, "silent_refresh", lambda store, sites: calls.append(sites) or {
        "method": "browser", "reconnected": {"cogo": True}, "needs_manual_login": False})
    monkeypatch.setattr(browser, "attached_port", lambda: 9222)
    store = store_with(tmp_path, [cookie("x-ms-cpim-sso:t_0", ".login.myxtra.be", "old")])
    transport, _ = sso_chain(sso_valid=True)
    mock_client(transport)
    assert session.auto_refresh(store, ("cogo",))["method"] == "http" and calls == []
    transport, _ = sso_chain(sso_valid=False)
    mock_client(transport)
    result = session.auto_refresh(store, ("cogo",))
    assert result["method"] == "browser" and calls == [("cogo",)] and "expiré" in result["http_detail"]["cogo"]


def test_summary_reports_sso_expiry(tmp_path):
    store = store_with(tmp_path, [
        cookie("x-ms-cpim-sso:t_0", ".login.myxtra.be", "s", time.time() + 86400 * 12 + 60),
        cookie("JSESSIONID", ".collectandgo.be"), cookie("WC_AUTHENTICATION_1", ".collectandgo.be"),
    ])
    s = store.summary()
    assert s["sso_session"] is True and s["sso_days_left"] == 12 and s["cogo_session"] is True
    assert s["colruyt_session"] is False and s["sso_expires"] is not None


def test_jar_for_api_calls_excludes_sso_and_antibot(tmp_path):
    store = store_with(tmp_path, [
        cookie("x-ms-cpim-sso:t_0", ".login.myxtra.be"), cookie("reese84", ".colruyt.be"),
        cookie("clpbff_session", ".colruyt.be"), cookie("_fbp", ".colruyt.be"),
    ])
    assert {c.name for c in store.to_jar().jar} == {"clpbff_session", "x-ms-cpim-sso:t_0"}
    assert {c.name for c in store.to_jar(everything=True).jar} == {"clpbff_session", "x-ms-cpim-sso:t_0", "_fbp"}


def test_wait_for_tabs_accepts_any_language_landing():
    start, landing = session.REFRESH_TARGETS["cogo"]
    frames = iter([
        [("t", start)],
        [("t", "https://login.myxtra.be/x/authorize?p=1")],
        [("t", "https://www.collectandgo.be/nl/home")],
    ])
    monkeypatch_sleep = browser.time.sleep
    browser.time.sleep = lambda s: None
    try:
        landed = browser.wait_for_tabs(0, {"cogo": (start, landing)}, timeout_s=5, poll_s=0,
                                       tabs=lambda port: next(frames))
    finally:
        browser.time.sleep = monkeypatch_sleep
    assert landed == {"cogo": True}


def test_wait_for_tabs_login_url_is_not_a_landing():
    start, landing = session.REFRESH_TARGETS["cogo"]
    frames = iter([[("t", start)], [("t", start)]])
    landed = browser.wait_for_tabs(0, {"cogo": (start, landing)}, timeout_s=0, poll_s=0,
                                   tabs=lambda port: next(frames))
    assert landed == {"cogo": False}


def test_attached_port_from_env(monkeypatch):
    monkeypatch.delenv("COLRUYT_MCP_DEVTOOLS_PORT", raising=False)
    assert browser.attached_port() is None
    monkeypatch.setenv("COLRUYT_MCP_DEVTOOLS_PORT", "9222")
    assert browser.attached_port() == 9222


def test_remember_attached_keeps_auto_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_FILE", tmp_path / "browser.json")
    browser.remember(None, "UA/2")
    assert browser.saved_user_agent() == "UA/2" and browser.saved_browser() is None


async def test_token_check():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = TokenCheck(app, "s3cret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer s3cret"})).status_code == 200
