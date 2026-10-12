"""Offline tests: parsing, order-safety guard, cart unit logic. No network."""
import httpx
import pytest
import respx

from colruyt_mcp.client import (BotBlocked, ClpBff, Cogo, ColruytClient, ForbiddenOperation, SessionExpired,
                                parse_clp_api_key, parse_wcparam)
from colruyt_mcp.logic import order_item, orderable_units, summarize_promo
from colruyt_mcp.session import CookieStore


def attrs(**kv):
    return [{"name": k, "values": [{"value": v}]} for k, v in kv.items()]


def test_parse_clp_api_key():
    page = '<div data-endpoints="[{&#34;headers&#34;:[&#34;X-CG-APIKey: abc-123&#34;]}]"></div>'
    assert parse_clp_api_key(page) == "abc-123"


def test_parse_wcparam():
    page = """var WCParamJS = {
        "xtraAvailable": true,
        "colShop": {"basket":{"basketItems":[]},"orderId":"123","shopName":"CLPBE"},
        "storeId": '90004', "catalogId": '10609', "langId": '-2',
        "loggedIn": true, "WCAuthToken": '42%2Csecret', "apiGatewayKey": 'k-1'};"""
    p = parse_wcparam(page)
    assert p == {"loggedIn": True, "WCAuthToken": "42%2Csecret", "apiGatewayKey": "k-1",
                 "storeId": "90004", "catalogId": "10609", "orderId": "123"}


def test_parse_wcparam_logged_out():
    assert parse_wcparam('"loggedIn": false, "WCAuthToken": \'\'')["loggedIn"] is False


@pytest.mark.parametrize("path", [
    "/cart/@self/checkout", "/cart/@self/initiatePaymentRequest", "/cart/1/cancel_order", "/OrderCancel?x=1",
])
async def test_forbidden_paths(tmp_path, path):
    async with httpx.AsyncClient() as http:
        cogo = Cogo(http, CookieStore(tmp_path / "c.json"))
        with pytest.raises(ForbiddenOperation):
            await cogo.request("POST", "https://apip.collectandgo.be/gateway/x/v2/store/90004" + path, "Collect&Go")


@respx.mock
async def test_401_raises_session_expired(tmp_path):
    respx.get("https://www.colruyt.be/fr").respond(
        200, text='<i data-endpoints="X-CG-APIKey: k"></i>')
    respx.get(url__startswith="https://apix.colruyt.be/gateway/emec.colruyt.bffsvc/cg/get-favorite-recipes").respond(401)
    async with httpx.AsyncClient() as http:
        clp = ClpBff(http, CookieStore(tmp_path / "c.json"))
        with pytest.raises(SessionExpired):
            await clp.favorite_recipe_ids()


def test_units_piece_and_pack():
    entry = {"name": "eau", "singleSKUCatalogEntryID": "11",
             "attributes": attrs(WcsIsOrderableByPiece="Y", WcsPackSize="6", IsWeightArticle="N")}
    assert orderable_units(entry) == ["P", "PA"]
    assert order_item(entry, 2) == {"productId": "11", "quantity": "2", "xitem_field2": "P|1|st"}
    assert order_item(entry, 2, "PA") == {"productId": "11", "quantity": "12", "xitem_field2": "PA|6|st"}


def test_units_weight_article_by_piece():
    entry = {"name": "bananes", "singleSKUCatalogEntryID": "13",
             "attributes": attrs(IsWeightArticle="Y", WcsIsOrderableByPiece="Y", WeightconversionFactor="1000.0")}
    assert orderable_units(entry) == ["P"]
    assert order_item(entry, 3)["xitem_field2"] == "P|1|kg"


def test_unit_not_allowed():
    entry = {"name": "x", "uniqueID": "1", "attributes": attrs(WcsIsOrderableByPiece="Y")}
    with pytest.raises(Exception):
        order_item(entry, 1, "W")


def test_summarize_service_fee_promo():
    p = summarize_promo({"promoType": "71", "strapLine": "Pas de frais de service", "staffelFrom": "25",
                         "staffelUnit": "A", "commercialId": "7653", "startDate": "07-OCT-26", "endDate": "20-OCT-26"})
    assert p["type"] == "frais de service offerts" and p["threshold"] == 25 and p["threshold_unit"] == "EUR"
    assert p["action_id"] == "7653"


def test_only_session_cookies_are_replayed(tmp_path):
    store = CookieStore(tmp_path / "c.json")
    store.save([
        {"name": n, "value": "v", "domain": ".collectandgo.be", "path": "/"}
        for n in ("JSESSIONID", "WC_AUTHENTICATION_1", "reese84", "_ga", "COL_SHOP")
    ] + [{"name": "clpbff_session", "value": "v", "domain": ".colruyt.be", "path": "/"}])
    assert sorted(c.name for c in store.to_jar().jar) == [
        "COL_SHOP", "JSESSIONID", "WC_AUTHENTICATION_1", "clpbff_session", "reese84"]


COGO_HOME = '<script>var WCParamJS = {"loggedIn": true, "WCAuthToken": \'t\', "apiGatewayKey": "k"};</script>'
BASKET = ("https://apip.collectandgo.be/gateway/ecomfoodb2c.eshop.wcsbasketcontextsvcv2/v2/store/90004"
          "/basketcontent/getbasketcontextdata")
ANTIBOT = "<html><title>Colruyt | AntiBot</title></html>"


@respx.mock
async def test_405_antibot_refreshes_token_and_retries(tmp_path, monkeypatch):
    """Imperva answers 405 to the stale token; the fresh one from the browser is replayed once."""
    store = CookieStore(tmp_path / "c.json")
    store.save([{"name": "reese84", "value": "stale", "domain": ".collectandgo.be", "path": "/"}])
    monkeypatch.setattr("colruyt_mcp.client.refresh_antibot", lambda s: s.replace_antibot(
        [{"name": "reese84", "value": "fresh", "domain": ".collectandgo.be", "path": "/"}]))
    respx.get("https://www.collectandgo.be/fr/home").respond(200, text=COGO_HOME)
    basket = respx.get(url__startswith=BASKET).mock(side_effect=lambda req: httpx.Response(
        200, json={"basket": {}}) if "reese84=fresh" in req.headers.get("cookie", "")
        else httpx.Response(405, text=ANTIBOT, headers={"content-type": "text/html; charset=UTF-8"}))
    client = ColruytClient(store, auto_refresh=False)
    try:
        assert await client.cogo.basket() == {"basket": {}}
        assert basket.call_count == 2
        assert store.antibot_cookies()[0]["value"] == "fresh"
    finally:
        await client.aclose()


@respx.mock
async def test_405_without_fresh_token_raises(tmp_path, monkeypatch):
    store = CookieStore(tmp_path / "c.json")
    monkeypatch.setattr("colruyt_mcp.client.refresh_antibot", lambda s: 0)  # no attached browser
    respx.get("https://www.collectandgo.be/fr/home").respond(200, text=COGO_HOME)
    basket = respx.get(url__startswith=BASKET).respond(405, text=ANTIBOT, headers={"content-type": "text/html"})
    client = ColruytClient(store, auto_refresh=False)
    try:
        with pytest.raises(BotBlocked):
            await client.cogo.basket()
        assert basket.call_count == 1
    finally:
        await client.aclose()


def test_xtra_list_entry_product_and_free_text():
    from colruyt_mcp.logic import xtra_list_entry
    assert xtra_list_entry({"technical_article_number": 4817831, "quantity": 1.0}, {"4817831": "BONI riz basmati 2kg"}) == {
        "description": "BONI riz basmati 2kg",
        "productData": {"productId": "4817831", "quantity": 1, "unitCode": "P"}}
    # free text must still carry productData (HTTP 422 otherwise), with an empty productId
    assert xtra_list_entry({"description": "pain"}, {}) == {
        "description": "pain", "productData": {"productId": "", "quantity": 1, "unitCode": "P"}}


def test_xtra_list_quantity_must_be_integer():
    from colruyt_mcp.logic import list_quantity
    assert list_quantity(2.0) == 2 and isinstance(list_quantity(2.0), int)
    with pytest.raises(Exception):
        list_quantity(1.5)
    with pytest.raises(Exception):
        list_quantity(0)


def test_xtra_list_entry_unknown_commercial_number():
    from colruyt_mcp.logic import xtra_list_entry
    with pytest.raises(Exception, match="technical_article_number"):
        xtra_list_entry({"technical_article_number": "12919"}, {})


def test_http_error_message_empty_body_shows_payload():
    from colruyt_mcp.client import http_error_message
    req = httpx.Request("POST", "https://apix.colruyt.be/x/cg/add-items-to-list")
    msg = http_error_message("POST", str(req.url), httpx.Response(422, request=req), {"items": [{"description": "pain"}]})
    assert "HTTP 422" in msg and "corps de réponse vide" in msg and '"description": "pain"' in msg
