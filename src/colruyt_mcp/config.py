"""Paths and constants. Everything personal lives outside the repo, in COLRUYT_MCP_HOME."""
import os
from pathlib import Path

HOME = Path(os.environ.get("COLRUYT_MCP_HOME", Path.home() / ".colruyt-mcp"))
COOKIES_FILE = HOME / "cookies.json"
BROWSER_FILE = HOME / "browser.json"  # browser used at login + its real User-Agent

LANG = os.environ.get("COLRUYT_MCP_LANG", "fr")
COGO_LANG_ID = {"fr": "-2", "nl": "-1001"}

# Server mode (e.g. in a container with a browser that is always running):
# - COLRUYT_MCP_DEVTOOLS_PORT: local DevTools port of that browser; login/refresh open tabs in it
#   instead of launching a browser ("attached" mode).
# - COLRUYT_MCP_LOGIN_URL: where the user can see that browser (web desktop), returned by
#   `request_login` so the assistant can send it to the user.
# - COLRUYT_MCP_TOKEN or COLRUYT_MCP_TOKEN_FILE: bearer token of the HTTP transport (`serve --http`).
LOGIN_URL = os.environ.get("COLRUYT_MCP_LOGIN_URL")

# Fallback only: the client replays the User-Agent of the browser used at login (browser.json),
# so that API calls look like the session's own browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
)

# colruyt.be / Xtra
CLP_HOME = "https://www.colruyt.be/fr"
CLP_BFF = "https://apix.colruyt.be/gateway/emec.colruyt.bffsvc"
CLP_BFF_PROTECTED = "https://apip.colruyt.be/gateway/emec.colruyt.protected.bffsvc"
CLP_PLACES = "https://apix.colruyt.be/gateway/ictmgmt.emarkecom.cgplacesretrsvcv4/v4"
CLP_LOGIN = "https://auth.colruyt.be/login?redirectUrl=https%3A%2F%2Fwww.colruyt.be%2Ffr"

# Collect&Go (HCL Commerce)
COGO_HOME = "https://www.collectandgo.be/fr/home"
COGO_LOGIN = "https://www.collectandgo.be/login?redirectUrl=https%3A%2F%2Fwww.collectandgo.be%2Ffr%2Fhome"
COGO_GATEWAY = "https://apip.collectandgo.be/gateway/ecomfoodb2c.eshop"
COGO_PLACES = "https://apix.colruytgroup.com/gateway/ictmgmt.emarkecom.cgplacesretrsvcv4/v4"
COGO_RECIPE_WAREHOUSE = "C00165B0-5EB5-11EA-B696-9FD37BA957BD"
COGO_SELLING_PARTNERS = ["CLPBE", "CLPLU", "BIOBE", "BIOLU"]
HANDOVER_SERVICE = "COLLECTNEXTDAY"
