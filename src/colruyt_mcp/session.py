"""Browser-backed session: the user logs in by hand in a regular browser (Edge, Chrome or
Chromium, see browser.py); we only read cookies afterwards. Credentials are never read, typed
or stored.

Renewal of the Collect&Go session (which expires after ~30 min of inactivity) has two routes:
- `http_refresh`: replay the login redirect chain with httpx, using the Azure B2C single sign-on
  cookie saved at login. No browser. This is the normal route.
- `silent_refresh`: open the login page in the browser profile used at login (or, in attached
  mode, in the browser already running, see browser.py) and let the single sign-on do its work.
`auto_refresh` tries the first, then the second.
"""
import json
import logging
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import browser, config

log = logging.getLogger(__name__)

COLRUYT_DOMAINS = ("colruyt.be", "collectandgo.be", "colruytgroup.com", "myxtra.be", "dcsecosystem.be")
# Cookies replayed outside the browser: the two sites' sessions and the B2C single sign-on cookie
# (login.myxtra.be, ~30 days, absolute) that `http_refresh` needs.
SESSION_COOKIE = re.compile(
    r"^(clpbff_session|JSESSIONID|WC_.+|COL_.+|APP_C_LANGUAGE|colruytlanguage|cogo_user|x-ms-cpim-sso:.+)$")
SSO_COOKIE = "x-ms-cpim-sso:"
# Imperva anti-bot token. Since 2026-10-12 the Collect&Go API gateway (apip.collectandgo.be) answers
# 405 "Colruyt | AntiBot" to any call without it, so API calls replay it. The browser renews it every
# ~10 min; when the gateway rejects a stale one, `refresh_antibot` re-reads it from the attached
# browser. The login chain (HTML pages) does not need it and never sends it.
ANTIBOT_COOKIE = "reese84"


def _ours(domain: str) -> bool:
    return any(domain.lstrip(".").endswith(d) for d in COLRUYT_DOMAINS)


class CookieStore:
    """Cookies exported from the browser profile, persisted as JSON (Playwright cookie format)."""

    def __init__(self, path: Path = config.COOKIES_FILE):
        self.path = path

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, cookies: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keep = [c for c in cookies if _ours(c["domain"])]
        self.path.write_text(json.dumps(keep, indent=1), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def to_jar(self, everything: bool = False) -> httpx.Cookies:
        """Session cookies plus the anti-bot token (API calls), or everything but the anti-bot token
        (login chain)."""
        jar = httpx.Cookies()
        for c in self.load():
            if c["name"] == ANTIBOT_COOKIE:
                if everything:
                    continue
            elif not (everything or SESSION_COOKIE.match(c["name"])):
                continue
            jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        return jar

    def antibot_cookies(self) -> list[dict]:
        return [c for c in self.load() if c["name"] == ANTIBOT_COOKIE]

    def replace_antibot(self, cookies: list[dict]) -> int:
        """Replace the stored anti-bot tokens by those of `cookies` (a fresh browser export). Returns
        how many were stored."""
        fresh = [c for c in cookies if c["name"] == ANTIBOT_COOKIE and _ours(c["domain"])]
        if fresh:
            self.save([c for c in self.load() if c["name"] != ANTIBOT_COOKIE] + fresh)
        return len(fresh)

    def update_from_jar(self, jar: httpx.Cookies, everything: bool = False) -> None:
        """Merge cookies the servers rotated during API calls (or set during a login chain) back
        into the file."""
        current = {(c["name"], c["domain"], c.get("path", "/")): c for c in self.load()}
        changed = False
        for ck in jar.jar:
            if not (everything or SESSION_COOKIE.match(ck.name)):
                continue
            if not _ours(ck.domain or ""):
                continue
            key = (ck.name, ck.domain, ck.path)
            old = current.get(key)
            expires = ck.expires or -1
            if old is None or old["value"] != ck.value or old.get("expires", -1) != expires:
                current[key] = {
                    "name": ck.name, "value": ck.value, "domain": ck.domain, "path": ck.path,
                    "expires": expires, "httpOnly": bool(ck.has_nonstandard_attr("HttpOnly")),
                    "secure": ck.secure, "sameSite": "Lax",
                }
                changed = True
        if changed:
            self.save(list(current.values()))

    def summary(self) -> dict:
        cookies = self.load()
        names = {c["name"] for c in cookies}
        clp = next((c for c in cookies if c["name"] == "clpbff_session"), None)
        sso = next((c for c in cookies if c["name"].startswith(SSO_COOKIE)), None)

        def when(c):
            exp = c.get("expires", -1) if c else -1
            if not exp or exp <= 0:
                return None, None
            return time.strftime("%Y-%m-%d", time.localtime(exp)), max(0, int((exp - time.time()) // 86400))

        clp_exp, clp_days = when(clp)
        sso_exp, sso_days = when(sso)
        return {
            "file": str(self.path),
            "colruyt_session": clp is not None,
            # The colruyt.be session cookie is persistent (about 60 days): no browser needed until then.
            "colruyt_expires": clp_exp,
            "colruyt_days_left": clp_days,
            "cogo_session": "JSESSIONID" in names and any(n.startswith("WC_AUTHENTICATION_") for n in names),
            # B2C single sign-on ("stay signed in", about 30 days, not extended by use): while it is
            # valid, Collect&Go renews without a browser and without an SMS code.
            "sso_session": sso is not None,
            "sso_expires": sso_exp,
            "sso_days_left": sso_days,
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.path.stat().st_mtime))
            if self.path.exists() else None,
        }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=5)  # Browser.close already sent: usually exits by itself
    except subprocess.TimeoutExpired:
        proc.terminate()


# Login URL to open, and where the tab lands once single sign-on has logged the user in. The
# landing prefix is language-independent: Collect&Go ignores the language in redirectUrl and lands
# on /nl/home or /fr/home depending on the profile's language cookie.
REFRESH_TARGETS = {
    "colruyt": (config.CLP_LOGIN, "https://www.colruyt.be/"),
    "cogo": (config.COGO_LOGIN, "https://www.collectandgo.be/"),
}


def open_login_tabs(sites: tuple[str, ...]) -> tuple[int, list[str]]:
    """Attached browser only: open the login pages as new tabs. Returns (port, tab ids)."""
    port = browser.attached_port()
    if port is None:
        raise RuntimeError("Pas de navigateur attaché (COLRUYT_MCP_DEVTOOLS_PORT).")
    return port, [browser.open_tab(port, REFRESH_TARGETS[s][0]) for s in sites]


def collect_from_attached(store: CookieStore, port: int, tabs: list[str]) -> dict:
    """Attached browser only: read its cookies once, close the tabs we opened, keep the browser."""
    try:
        user_agent = browser.devtools_version(port).get("User-Agent")
        store.save(browser.read_cookies_and_close(port, close=False))
        browser.remember(None, user_agent)
    finally:
        for t in tabs:
            browser.close_tab(port, t)
    return store.summary() | {"browser": "attached"}


def refresh_antibot(store: CookieStore) -> int:
    """Attached browser only: re-read the Imperva token(s) the browser keeps fresh on its open
    collectandgo.be tab. Returns how many were stored (0 without an attached browser)."""
    port = browser.attached_port()
    if port is None:
        return 0
    return store.replace_antibot(browser.read_cookies_and_close(port, close=False))


def interactive_login(store: CookieStore, preference: str | None = None) -> dict:
    """CLI flow: open both login pages, the user logs in, presses Enter, cookies are saved."""
    urls = [config.CLP_LOGIN, config.COGO_LOGIN]
    if browser.attached_port():
        port, tabs = open_login_tabs(("colruyt", "cogo"))
        print(f"Les pages de connexion sont ouvertes dans le navigateur déjà lancé (port {port}).")
        if config.LOGIN_URL:
            print(f"Ouvrez {config.LOGIN_URL} pour voir ce navigateur.")
        print("Connectez-vous dans les deux onglets (cochez « rester connecté » si proposé).")
        input("Quand les deux sites affichent votre compte, appuyez sur Entrée… ")
        return collect_from_attached(store, port, tabs)
    b = browser.find_browser(preference)
    port = _free_port()
    proc = browser.launch(b, urls, port)
    try:
        user_agent = browser.devtools_version(port).get("User-Agent")
        print(f"{b.family.title()} est ouvert ({b.path}) sur les pages de connexion colruyt.be et Collect&Go.")
        print("Connectez-vous dans les deux onglets (cochez « rester connecté » si proposé).")
        input("Quand les deux sites affichent votre compte, appuyez sur Entrée… ")
        store.save(browser.read_cookies_and_close(port))
        browser.remember(b, user_agent)
    finally:
        _stop(proc)
    return store.summary() | {"browser": b.family}


def silent_refresh(store: CookieStore, sites: tuple[str, ...] = ("cogo",), timeout_s: float = 40) -> dict:
    """Best effort: open the login page of `sites` in the browser profile used at login (or as new
    tabs of the attached browser); the B2C single sign-on cookie (if still valid) logs in without
    user action. Nothing is attached to the pages while they load: progress is read from the local
    DevTools tab list, then cookies are read once."""
    targets = {s: REFRESH_TARGETS[s] for s in sites}
    if browser.attached_port():
        port, tabs = open_login_tabs(sites)
        landed = browser.wait_for_tabs(port, targets, timeout_s)
        result = collect_from_attached(store, port, tabs)
        return result | {"method": "browser", "reconnected": landed, "needs_manual_login": not all(landed.values())}
    b = browser.find_browser()
    port = _free_port()
    proc = browser.launch(b, [REFRESH_TARGETS[s][0] for s in sites], port, minimized=True)
    try:
        user_agent = browser.devtools_version(port).get("User-Agent")
        landed = browser.wait_for_tabs(port, targets, timeout_s)
        store.save(browser.read_cookies_and_close(port))
        browser.remember(b, user_agent)
    finally:
        _stop(proc)
    return store.summary() | {"method": "browser", "browser": b.family, "reconnected": landed,
                              "needs_manual_login": not all(landed.values())}


# ------------------------------------------------------------------ HTTP refresh (no browser)

LOGIN_UI = re.compile(r"CombinedSigninAndSignup|SelfAsserted|SA_FIELDS")
MAX_HOPS = 15


def _landing_ok(site: str, url: str, text: str, jar: httpx.Cookies) -> bool:
    host = urlsplit(url).netloc
    if site == "cogo":
        return host == "www.collectandgo.be" and bool(re.search(r'"loggedIn"\s*:\s*true', text))
    return host == "www.colruyt.be" and any(c.name == "clpbff_session" for c in jar.jar)


def follow_login(client: httpx.Client, site: str, start: str) -> tuple[bool, str]:
    """Follow the login redirect chain of `site`. Returns (logged in, short reason). Only hosts and
    paths are ever reported: query strings carry codes and signed state."""
    url = start
    for _ in range(MAX_HOPS):
        r = client.get(url)
        where = f"{urlsplit(url).netloc}{urlsplit(url).path}"
        if r.status_code in (403, 405):
            return False, f"anti-bot HTTP {r.status_code} sur {where}"
        if 300 <= r.status_code < 400 and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} sur {where}"
        if LOGIN_UI.search(r.text):
            return False, "la connexion unique a expiré : formulaire de connexion affiché"
        if _landing_ok(site, url, r.text, client.cookies):
            return True, f"connecté ({where})"
        return False, f"page inattendue : {where}"
    return False, "trop de redirections"


def http_refresh(store: CookieStore, sites: tuple[str, ...] = ("cogo",), timeout_s: float = 30) -> dict:
    """Renew `sites` with plain HTTP requests, using the saved B2C single sign-on cookie. Same
    result shape as `silent_refresh`. Never opens a browser."""
    headers = {"User-Agent": browser.saved_user_agent() or config.USER_AGENT,
               "Accept-Language": f"{config.LANG}-BE,{config.LANG};q=0.9",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    landed, detail = {}, {}
    with httpx.Client(cookies=store.to_jar(everything=True), follow_redirects=False, timeout=timeout_s,
                      headers=headers) as client:
        for site in sites:
            try:
                landed[site], detail[site] = follow_login(client, site, REFRESH_TARGETS[site][0])
            except httpx.HTTPError as exc:
                landed[site], detail[site] = False, f"erreur réseau : {type(exc).__name__}"
            log.info("Reconnexion HTTP %s : %s", site, detail[site])
        store.update_from_jar(client.cookies, everything=True)
    return store.summary() | {"method": "http", "reconnected": landed, "detail": detail,
                              "needs_manual_login": not all(landed.values())}


def auto_refresh(store: CookieStore, sites: tuple[str, ...] = ("cogo",)) -> dict:
    """HTTP first; if that does not reconnect everything, try the browser route when one is
    available (attached browser, or a browser installed for a previous login)."""
    result = http_refresh(store, sites)
    if not result["needs_manual_login"]:
        return result
    try:
        if browser.attached_port() is None:
            browser.find_browser()  # raises when no browser is installed
        fallback = silent_refresh(store, sites)
    except Exception as exc:  # no browser, profile in use, ...
        log.warning("Reconnexion par navigateur impossible : %s", exc)
        return result
    fallback["http_detail"] = result["detail"]
    return fallback
