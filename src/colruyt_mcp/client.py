"""HTTP client for colruyt.be (Xtra BFF) and Collect&Go (HCL Commerce gateways).

Endpoints were mapped from the sites' own traffic while using them with a personal account.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import httpx

from . import config
from .browser import saved_user_agent
from .session import CookieStore, auto_refresh, collect_from_attached

log = logging.getLogger(__name__)

# The MCP must never place or pay an order: refuse these paths whatever the caller asks.
FORBIDDEN_PATH = re.compile(
    r"checkout|payment|PaymentRequest|validateOrderPaymentStatus|cancel_order|OrderCancel|order/amend",
    re.I,
)


class ColruytError(Exception):
    pass


class SessionExpired(ColruytError):
    def __init__(self, site: str):
        if site == "Collect&Go":
            hint = ("La reconnexion automatique a échoué ou a déjà été tentée il y a moins de 5 min. "
                    "Réessayez plus tard, ou lancez `colruyt-mcp login` dans un terminal "
                    "(connexion manuelle + SMS).")
        else:
            hint = ("Le cookie colruyt.be dure environ 60 jours : il a expiré ou la session a été fermée. "
                    "Essayez refresh_session(site=\"all\"), sinon lancez `colruyt-mcp login`.")
        super().__init__(f"Session {site} expirée ou absente. {hint}")
        self.site = site


class ForbiddenOperation(ColruytError):
    pass


class BotBlocked(ColruytError):
    def __init__(self, url: str, status: int):
        super().__init__(f"Requête bloquée par l'anti-bot (HTTP {status}) sur {url.split('?')[0]}. "
                         "Réessayez plus tard ; si cela persiste, relancez `colruyt-mcp login`.")


def parse_clp_api_key(page: str) -> str:
    raw = re.search(r'data-endpoints="([^"]+)"', page)
    if not raw:
        raise ColruytError("Configuration colruyt.be introuvable (data-endpoints).")
    key = re.search(r"X-CG-APIKey:\s*([\w-]+)", html.unescape(raw.group(1)))
    if not key:
        raise ColruytError("Clé d'API colruyt.be introuvable.")
    return key.group(1)


def parse_wcparam(page: str) -> dict:
    """Extract the few WCParamJS values we need from a Collect&Go HTML page."""
    def grab(name: str) -> str | None:
        m = re.search(rf'"{name}"\s*:\s*(\'([^\']*)\'|"([^"]*)"|(true|false|-?\d+))', page)
        if not m:
            return None
        return next(g for g in m.groups()[1:] if g is not None)

    order = re.search(r'"colShop"\s*:\s*\{.*?"orderId"\s*:\s*"(\d+)"', page, re.S)
    return {
        "loggedIn": grab("loggedIn") == "true",
        "WCAuthToken": grab("WCAuthToken"),
        "apiGatewayKey": grab("apiGatewayKey"),
        "storeId": grab("storeId") or "90004",
        "catalogId": grab("catalogId") or "10609",
        "orderId": order.group(1) if order else None,
    }


class _Base:
    def __init__(self, http: httpx.AsyncClient, store: CookieStore):
        self.http = http
        self.store = store

    async def request(self, method: str, url: str, site: str, **kw) -> httpx.Response:
        if FORBIDDEN_PATH.search(url):
            raise ForbiddenOperation(f"Opération interdite par le MCP : {url}")
        resp = await self.http.request(method, url, **kw)
        self.store.update_from_jar(self.http.cookies)
        if resp.status_code == 401:
            raise SessionExpired(site)
        if resp.status_code in (403, 405) and "json" not in resp.headers.get("content-type", ""):
            raise BotBlocked(url, resp.status_code)
        if resp.status_code >= 400:
            raise ColruytError(http_error_message(method, url, resp, kw.get("json")))
        return resp


def http_error_message(method: str, url: str, resp: httpx.Response, sent_json=None) -> str:
    """Readable error: status, response body (or that it is empty) and, for client errors, the payload
    we sent — the BFF often answers 400/422 with an empty body. Payloads never contain secrets
    (those travel in headers/cookies, which are not included)."""
    body = resp.text.strip()
    msg = (f"{method} {url.split('?')[0]} → HTTP {resp.status_code} "
           f"({resp.headers.get('content-type') or 'sans content-type'}) : "
           f"{body[:500] if body else '(corps de réponse vide)'}")
    if sent_json is not None and 400 <= resp.status_code < 500:
        msg += f" | payload envoyé : {json.dumps(sent_json, ensure_ascii=False)[:500]}"
    return msg


class ClpBff(_Base):
    """colruyt.be / Xtra: shopping list, favourites, purchase history, product search."""

    def __init__(self, http, store, lang: str = config.LANG):
        super().__init__(http, store)
        self.lang = lang
        self._key: str | None = None
        self._place_id: str | None = None

    async def key(self) -> str:
        if self._key is None:
            page = await self.http.get(config.CLP_HOME)
            self._key = parse_clp_api_key(page.text)
        return self._key

    async def _headers(self) -> dict:
        return {"X-CG-APIKey": await self.key(), "Accept": "application/json",
                "Origin": "https://www.colruyt.be", "Referer": "https://www.colruyt.be/"}

    async def get(self, url, **params):
        return (await self.request("GET", url, "colruyt.be", headers=await self._headers(), params=params)).json()

    async def send(self, method, url, body=None, **params):
        resp = await self.request(method, url, "colruyt.be", headers=await self._headers(), json=body, params=params)
        return resp.json() if resp.content else None

    # session & store
    async def auth_check(self) -> bool:
        try:
            await self.get(f"{config.CLP_BFF}/auth/check")
            return True
        except SessionExpired:
            return False

    async def place_id(self) -> str:
        """The BFF returns the preferred store as a branchId; prices need its placeId."""
        if self._place_id is None:
            branch = (await self.get(f"{config.CLP_BFF}/cg/preferredStore"))["value"]
            place = await self.get(f"{config.CLP_PLACES}/{self.lang}/places/getByBranchIdAndPlaceType",
                                   branchId=branch, placeTypeId=1)
            self._place_id = str(place["placeId"])
        return self._place_id

    # products
    async def search_products(self, term: str, size: int = 10, skip: int = 0, **filters) -> dict:
        params = {"searchTerm": term, "placeId": await self.place_id(), "size": size,
                  "sort": "relevancy desc", "isAvailable": "true", "skip": skip}
        params |= {k: "true" for k, v in filters.items() if v}
        return await self.get(f"{config.CLP_BFF_PROTECTED}/cg/{self.lang}/api/product-search-prs", **params)

    async def products_by_tech(self, tech_ids: list[str]) -> list[dict]:
        if not tech_ids:
            return []
        data = await self.get(f"{config.CLP_BFF_PROTECTED}/cg/{self.lang}/api/products",
                              placeId=await self.place_id(), dataGroup="ALL", isAvailable="true",
                              technicalArtNos=",".join(tech_ids))
        return data.get("products", [])

    # recipes
    async def favorite_recipe_ids(self) -> list[str]:
        return [r["recipeId"] for r in (await self.get(f"{config.CLP_BFF}/cg/get-favorite-recipes"))["data"]]

    # Xtra shopping list
    async def shopping_list(self) -> dict:
        items, offset, model = [], 0, None
        while True:
            page = await self.get(f"{config.CLP_BFF}/cg/latest-shopping-list", limit=100, offset=offset,
                                  sort="createdAt desc", placeId=await self.place_id(), lang=self.lang, prs="true")
            model = page.get("getListModel")
            batch = (page.get("getListItems") or {}).get("data") or []
            items += batch
            total = ((page.get("getListItems") or {}).get("pageInfo") or {}).get("total", len(items))
            if not batch or len(items) >= total:
                return {"list": model, "items": items}
            offset += len(batch)

    async def add_list_items(self, items: list[dict]) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        body = {"items": [{"id": str(uuid.uuid4()), "createdAt": now, "updatedAt": now, "completedAt": None,
                           **it} for it in items]}
        return await self.send("POST", f"{config.CLP_BFF}/cg/add-items-to-list", body)

    async def update_list_items(self, items: list[dict]) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return await self.send("PUT", f"{config.CLP_BFF}/cg/update-items-in-list",
                               {"items": [it | {"updatedAt": now} for it in items]})

    async def remove_list_item(self, item_id: str) -> None:
        await self.send("DELETE", f"{config.CLP_BFF}/cg/remove-item-from-list/{item_id}")

    # purchase history (technical lists)
    async def most_bought(self) -> list[dict]:
        return await self.get(f"{config.CLP_BFF}/cg/most-bought-products", lang=self.lang,
                              placeId=await self.place_id(), prs="true")

    async def last_ticket(self) -> list[dict]:
        return await self.get(f"{config.CLP_BFF}/cg/last-purchase-ticket", lang=self.lang,
                              placeId=await self.place_id(), prs="true")


class Cogo(_Base):
    """Collect&Go: recipes, products, cart, pickup point & slot, promotions."""

    def __init__(self, http, store, lang: str = config.LANG):
        super().__init__(http, store)
        self.lang = lang
        self.lang_id = config.COGO_LANG_ID[lang]
        self.params: dict | None = None
        # Async callback returning True when it renewed the session (set by ColruytClient).
        self.on_expired = None

    async def _load_params(self) -> dict:
        resp = await self.http.get(config.COGO_HOME)
        self.store.update_from_jar(self.http.cookies)
        if resp.status_code in (403, 405):
            raise BotBlocked(config.COGO_HOME, resp.status_code)
        p = parse_wcparam(resp.text)
        if resp.status_code == 401 or not p["loggedIn"] or not p["WCAuthToken"]:
            raise SessionExpired("Collect&Go")
        return p

    async def bootstrap(self, force: bool = False, auto_refresh: bool = True) -> dict:
        """Read WCParamJS. The Collect&Go server session expires after ~30 min of inactivity: when it
        has, renew it once through the browser (single sign-on, no SMS) and try again."""
        if self.params is None or force:
            try:
                self.params = await self._load_params()
            except SessionExpired:
                if not (auto_refresh and self.on_expired and await self.on_expired()):
                    raise
                self.params = await self._load_params()
        return self.params

    def gw(self, service: str, version: str) -> str:
        return f"{config.COGO_GATEWAY}.{service}/{version}/store/{self.params['storeId']}"

    async def call(self, method: str, service: str, version: str, path: str, body=None, params=None):
        p = await self.bootstrap()
        headers = {"X-CG-APIKey": p["apiGatewayKey"], "WCAuthToken": p["WCAuthToken"],
                   "Content-Type": "application/json", "Accept": "*/*",
                   "Origin": "https://www.collectandgo.be", "Referer": "https://www.collectandgo.be/"}
        url = self.gw(service, version) + path
        try:
            resp = await self.request(method, url, "Collect&Go", headers=headers, json=body, params=params)
        except SessionExpired:
            await self.bootstrap(force=True)  # token may have rotated; one retry
            headers["WCAuthToken"] = self.params["WCAuthToken"]
            resp = await self.request(method, url, "Collect&Go", headers=headers, json=body, params=params)
        return resp.json() if resp.content else None

    async def base_params(self, **extra) -> dict:
        await self.bootstrap()
        return {"catalogId": self.params["catalogId"], "langId": self.lang_id} | extra

    # recipes
    def _recipe_query(self, **extra) -> dict:
        return {"data": [{"languages": [self.lang], "page": 0,
                          "warehouses": {"must": [config.COGO_RECIPE_WAREHOUSE]},
                          "publishingStates": {"must": ["published"], "mustNot": []}} | extra]}

    async def favourite_recipe_ids(self) -> list[str]:
        data = await self.call("GET", "wcsrecipesvc", "v1", "/recipes/list-favourites")
        return [r["recipeId"] for r in data.get("data", [])]

    async def search_recipes(self, text: str | None = None, ids: list[str] | None = None, limit: int = 10) -> list:
        extra = {"limit": limit}
        if text:
            extra["search"] = text
        if ids:
            extra["ids"] = ids
        data = await self.call("POST", "wcsrecipesvc", "v1", "/recipes/search", self._recipe_query(**extra))
        return (data.get("recipeDetails") or {}).get("recipes") or []

    async def recipe_details(self, recipe_ids: list[str]) -> list[dict]:
        data = await self.call("GET", "wcsrecipesvc", "v1", f"/recipes/details/recipeIds/{','.join(recipe_ids)}",
                               params={"langId": self.lang_id})
        return (data.get("recipeProfile") or {}).get("recipeList") or []

    async def productize(self, recipe_id: str, servings: int, branch_id: str) -> dict:
        body = {"recipe": recipe_id, "targetYield": servings, "select": ["article.identifiers"],
                "warehouses": [[f"sellingPartner:{sp}", f"branchId:{branch_id}"] for sp in config.COGO_SELLING_PARTNERS]}
        return await self.call("POST", "wcsrecipesvc", "v1", "/recipes/productize", body)

    # products
    async def products_by_tech(self, tech_ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(tech_ids), 50):
            chunk = tech_ids[i:i + 50]
            params = [("id", t) for t in chunk] + list((await self.base_params(pageSize=str(len(chunk)), pageNumber="1")).items())
            data = await self.call("GET", "wcsproductsvc", "v1", "/productdata/byTechnicalIds", params=params)
            out += data.get("catalogEntryView", [])
        return out

    async def search_products(self, term: str = "*", facet: str | None = None, page: int = 1, size: int = 24) -> dict:
        params = (await self.base_params(searchSource="N", shopName="CLPBE", pageSize=size, pageNumber=page, orderBy="1"))
        if facet:
            params["facet"] = facet
        return await self.call("GET", "wcsproductviewsearchsvc", "v1", f"/productview/bySearchTerm/{term}",
                               params=params)

    # cart
    async def cart(self) -> dict:
        return await self.call("GET", "wcscartsvcv2", "v2", "/cart/@self", params={"langId": self.lang_id})

    async def basket(self) -> dict:
        return await self.call("GET", "wcsbasketcontextsvcv2", "v2", "/basketcontent/getbasketcontextdata",
                               params=(await self.base_params(source="web", withPromotions="true")))

    async def add_to_cart(self, order_items: list[dict]) -> dict:
        return await self.call("POST", "wcscartsvcv2", "v2", "/cart", {"orderItem": order_items},
                               params=(await self.base_params()))

    async def update_cart(self, order_id: str, order_items: list[dict]) -> dict:
        return await self.call("PUT", "wcscartsvcv2", "v2", "/cart/@self/update_order_item",
                               {"orderId": order_id, "orderItem": order_items, "orderExtendAttribute": []},
                               params={"langId": self.lang_id, "calculateBasket": "true"})

    # pickup point & slot
    async def places_near(self, latitude: float, longitude: float, radius_km: int) -> list[dict]:
        p = await self.bootstrap()
        resp = await self.request("GET", f"{config.COGO_PLACES}/{self.lang}/places/filter/cogo-colpnts", "Collect&Go",
                                  headers={"X-CG-APIKey": p["apiGatewayKey"], "Origin": "https://www.collectandgo.be"},
                                  params={"latitude": latitude, "longitude": longitude, "radius": radius_km})
        return resp.json().get("placeSearchDetails", [])

    async def pickup_point_details(self, branch_id: str) -> dict:
        p = await self.bootstrap()
        resp = await self.request("GET", f"{config.COGO_PLACES}/{self.lang}/places/getByBranchIdAndPlaceType",
                                  "Collect&Go", headers={"X-CG-APIKey": p["apiGatewayKey"]},
                                  params={"branchId": branch_id, "placeTypeId": 6})
        return resp.json()

    async def reserve_pickup_point(self, branch_id: str) -> dict:
        await self.bootstrap()
        return await self.call("POST", "wcshandoverpointsvc", "v1", "/handoverPoints/reserve",
                               params={"handoverPointId": branch_id, "handoverServiceId": config.HANDOVER_SERVICE,
                                       "catalogId": self.params["catalogId"], "HOPforcereserve": "false"})

    async def slots(self, branch_id: str, order_id: str, date_from: str, date_to: str) -> dict:
        return await self.call("GET", "wcshandoverwindowsvcv2", "v2", "/handoverwindows/getwindows",
                               params={"handoverServiceId": config.HANDOVER_SERVICE, "handoverDateFrom": date_from,
                                       "handoverDateTo": date_to, "ordersId": order_id,
                                       "handoverPointId": branch_id, "backupWindowRequest": "N"})

    async def validate_slot(self, slot: dict) -> dict:
        keys = ("handoverDate", "handoverFrom", "handoverTo", "sellingPartnerId", "handoverPointId",
                "handoverServiceId", "serviceFulfilmentTechArtId")
        return await self.call("POST", "wcshandoverwindowsvcv2", "v2", "/handoverwindows/validate",
                               {k: slot[k] for k in keys})

    async def reserve_slot(self, slot: dict) -> dict:
        return await self.call("POST", "wcshandoverwindowsvcv2", "v2", "/handoverwindows/reserve", slot,
                               params={"langId": self.lang_id, "checkoverrideRange": "false",
                                       "checkBasketPromotions": "false"})

    # purchase history
    async def last_order(self) -> dict:
        return await self.call("GET", "wcslistsvc", "v1", "/list/lastOrder", params=(await self.base_params(shopType="CLPBE")))

    async def top_products(self) -> dict:
        return await self.call("GET", "wcslistsvc", "v1", "/list/@self/type/TOP250",
                               params=(await self.base_params(shopType="CLPBE", noOfdays="All")))

    async def promotion_page(self, coupon_id: str, commercial_id: str, coupon_version: str = "SCP-S_0") -> str:
        await self.bootstrap()
        resp = await self.request("GET", "https://www.collectandgo.be/Promotions", "Collect&Go",
                                  params={"storeId": self.params["storeId"], "couponId": coupon_id,
                                          "couponVersion": coupon_version, "sellingPartner": "CLPBE",
                                          "commercialId": commercial_id,
                                          "promo": f"{coupon_id}:{coupon_version}:CLPBE:{commercial_id}"})
        return resp.text


REFRESH_COOLDOWN_S = 300  # at most one automatic browser refresh every 5 minutes


class ColruytClient:
    """colruyt.be (Xtra) never opens a browser: its cookie lasts about 60 days. Only Collect&Go, whose
    server session times out after ~30 min of inactivity, triggers an automatic browser refresh, and
    only when a Collect&Go call actually needs it."""

    def __init__(self, store: CookieStore | None = None, lang: str = config.LANG,
                 auto_refresh: bool | None = None, refresher=auto_refresh):
        self.store = store or CookieStore()
        self.http = httpx.AsyncClient(
            timeout=30, follow_redirects=False, cookies=self.store.to_jar(),
            headers={"User-Agent": saved_user_agent() or config.USER_AGENT, "Accept-Language": f"{lang}-BE,{lang};q=0.9"},
        )
        self.clp = ClpBff(self.http, self.store, lang)
        self.cogo = Cogo(self.http, self.store, lang)
        if auto_refresh is None:
            auto_refresh = os.environ.get("COLRUYT_MCP_AUTO_REFRESH", "1") != "0"
        self._refresher = refresher
        self._refresh_lock = asyncio.Lock()
        self._refresh_gen = 0
        self._last_refresh = -REFRESH_COOLDOWN_S
        self.last_refresh_result: dict | None = None
        if auto_refresh:
            self.cogo.on_expired = self.refresh_cogo

    def reload_cookies(self) -> None:
        self.http.cookies = self.store.to_jar()
        self.cogo.params = None

    async def refresh(self, sites: tuple[str, ...]) -> dict:
        """Refresh of `sites` with the default refresher (HTTP first, then browser; blocking work
        runs in a thread), then reload the cookies."""
        return await self.refresh_with(self._refresher, sites)

    async def refresh_with(self, refresher, sites: tuple[str, ...]) -> dict:
        result = await asyncio.to_thread(refresher, self.store, sites)
        self.reload_cookies()
        self._refresh_gen += 1
        self.last_refresh_result = result
        return result

    async def collect_attached(self, port: int, tabs: list[str]) -> dict:
        """After a manual login in the attached browser: read its cookies, then reload them."""
        result = await asyncio.to_thread(collect_from_attached, self.store, port, tabs)
        self.reload_cookies()
        self._refresh_gen += 1
        return result

    async def refresh_cogo(self) -> bool:
        """Automatic Collect&Go refresh. Concurrent callers share one attempt; True if a refresh ran
        (by this caller or while it was waiting) and reconnected."""
        gen = self._refresh_gen
        async with self._refresh_lock:
            if self._refresh_gen != gen:  # another caller refreshed while we waited: reuse its outcome
                return bool(((self.last_refresh_result or {}).get("reconnected") or {}).get("cogo"))
            if time.monotonic() - self._last_refresh < REFRESH_COOLDOWN_S:
                return False
            self._last_refresh = time.monotonic()
            log.info("Session Collect&Go expirée : reconnexion via la connexion unique")
            try:
                result = await self.refresh(("cogo",))
            except Exception as exc:  # no browser, profile in use, ...
                log.warning("Reconnexion Collect&Go impossible : %s", exc)
                return False
            return bool((result.get("reconnected") or {}).get("cogo"))

    async def aclose(self) -> None:
        await self.http.aclose()

