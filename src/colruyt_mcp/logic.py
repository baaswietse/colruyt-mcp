"""Business logic on top of the raw client: recipes → products, cart, service-fee actions."""
import asyncio
import html
import math
import re
from datetime import date, datetime, timedelta, timezone

from .client import ColruytClient, ColruytError
from . import config

PROMO_TYPES = {"0": "à l'achat de", "3": "à partir de", "4": "progressive", "71": "frais de service offerts",
               "72": "produit gratuit du moment", "73": "action frais de livraison"}


def _loc(values: list[dict] | None, lang: str = config.LANG) -> str:
    """Pick the right language in [{name, lang}] arrays."""
    if not values:
        return ""
    for v in values:
        if v.get("lang") == lang:
            return v.get("name", "")
    return values[0].get("name", "")


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- WCS products

def wcs_attributes(entry: dict) -> dict:
    return {a["name"]: (a.get("values") or [{}])[0].get("value") for a in entry.get("attributes", [])}


def orderable_units(entry: dict) -> list[str]:
    """Port of parseOrderableUnitsFromWcsProduct (C&G front)."""
    a = wcs_attributes(entry)
    weight = a.get("IsWeightArticle") == "Y"
    pack = _num(a.get("WcsPackSize")) or 0
    has_pack = not weight and pack > 0
    units = []
    if not (a.get("HidePerPiece") == "Y" and has_pack) and a.get("WcsIsOrderableByPiece") == "Y":
        units.append("P")
    if has_pack:
        units.append("PA")
    if weight and a.get("WcsIsOrderableByWeight") == "Y":
        units.append("W")
    if weight and (_num(a.get("WeightToPerson")) or 0) > 0:
        units.append("PR")
    return units or ["P"]


def unit_base(entry: dict, unit: str) -> float:
    a = wcs_attributes(entry)
    if unit == "P":
        return 1
    if unit == "PA":
        return _num(a.get("WcsPackSize")) or 1
    if unit == "W":
        f = _num(a.get("WeightconversionFactor"))
        return f / 1000 if f else 0.1
    if unit == "PR":
        f = _num(a.get("WeightToPerson"))
        return f / 1000 if f else 0.1
    raise ValueError(unit)


def order_item(entry: dict, quantity: float, unit: str | None = None) -> dict:
    """Build a cart orderItem like the C&G front: productId = SKU id, quantity in base units."""
    units = orderable_units(entry)
    unit = unit or units[0]
    if unit not in units:
        raise ColruytError(f"Unité {unit} impossible pour {entry.get('name')} (possibles : {units}).")
    base = unit_base(entry, unit)
    weight = wcs_attributes(entry).get("IsWeightArticle") == "Y"
    sku = entry.get("singleSKUCatalogEntryID") or (entry.get("sKUs") or [{}])[0].get("uniqueID") or entry["uniqueID"]
    qty = round(quantity * base, 3)
    return {"productId": str(sku), "quantity": f"{qty:g}", "xitem_field2": f"{unit}|{base:g}|{'kg' if weight else 'st'}"}


def summarize_wcs(entry: dict) -> dict:
    price = (entry.get("xprice") or [{}])[0]
    return {
        "technical_article_number": entry.get("x_techArticleId"),
        "name": entry.get("name"),
        "price": _num(price.get("basePrice")),
        "price_per_unit": _num(price.get("basePriceVol")),
        "promo_price": _num(price.get("offerPrice")),
        "promo_price_qty": price.get("offerPriceQty"),
        "available": entry.get("isProductAvailable") == "Y" or entry.get("buyable") == "true",
        "units": orderable_units(entry),
        "promotions": [summarize_promo(p) for p in entry.get("promotions", [])],
    }


def summarize_promo(p: dict) -> dict:
    return {k: v for k, v in {
        "type": PROMO_TYPES.get(p.get("promoType"), p.get("promoType")),
        "label": p.get("strapLine"), "detail": p.get("secondStrapLine", "").strip(", "),
        "threshold": _num(p.get("staffelFrom")), "threshold_unit": {"A": "EUR", "P": "pièces"}.get(p.get("staffelUnit")),
        "discount_pct": _num(p.get("discount")), "from": p.get("startDate"), "to": p.get("endDate"),
        "action_id": p.get("commercialId") or p.get("couponId"),
    }.items() if v not in (None, "", [])}


def list_quantity(quantity) -> int:
    """The Xtra list BFF rejects non-integer JSON numbers (1.0 → HTTP 400)."""
    q = float(quantity)
    if not q.is_integer() or q < 1:
        raise ColruytError(f"Quantité {quantity} invalide pour la liste Xtra : entier ≥ 1 attendu.")
    return int(q)


def xtra_list_entry(item: dict, names: dict[str, str]) -> dict:
    """One add-items-to-list entry. productData is mandatory (HTTP 422 without it): free-text items
    carry an empty productId, like the ones the site creates."""
    tech = str(item["technical_article_number"]) if item.get("technical_article_number") else ""
    description = item.get("description") or names.get(tech, "")
    if not description:
        raise ColruytError(f"Article sans description ni produit connu : {item}. "
                           "Utilisez le technical_article_number (pas le numéro commercial) ou une description.")
    return {"description": description,
            "productData": {"productId": tech, "quantity": list_quantity(item.get("quantity", 1)),
                            "unitCode": item.get("unit") or "P"}}


def summarize_clp(p: dict) -> dict:
    price = p.get("price") or {}
    return {
        "technical_article_number": p.get("technicalArticleNumber"),
        "commercial_article_number": p.get("commercialArticleNumber"),
        "name": p.get("LongName") or f"{p.get('brand', '')} {p.get('name', '')} {p.get('content', '')}".strip(),
        "brand": p.get("brand"),
        "content": p.get("content"),
        "price": price.get("basicPrice"),
        "price_per_unit": price.get("measurementUnitPrice"),
        "unit_of_measure": price.get("measurementUnit"),
        "in_promo": p.get("inPromo"),
        "available": p.get("isAvailable"),
        "category": p.get("topCategoryName"),
        "is_weight_article": p.get("IsWeightArticle"),
    }


# ---------------------------------------------------------------- service

class Service:
    def __init__(self, client: ColruytClient):
        self.c = client
        self._history: set[str] | None = None
        self._tiers: dict[tuple, list[dict]] = {}  # (action_id, end date) -> tiers, pages are heavy

    # -- session
    async def session_status(self) -> dict:
        out = {"cookies": self.c.store.summary()}
        out["colruyt_be"] = await self.c.clp.auth_check()
        try:
            # a status check must never open a browser
            await self.c.cogo.bootstrap(force=True, auto_refresh=False)
            out["collect_and_go"] = True
        except ColruytError:
            out["collect_and_go"] = False
            out["collect_and_go_note"] = ("Session expirée après inactivité : elle sera renouvelée "
                                          "automatiquement au prochain outil Collect&Go.")
        days = out["cookies"].get("colruyt_days_left")
        if days is not None and days < 7:
            out["colruyt_warning"] = (f"Le cookie colruyt.be expire dans {days} jour(s) : "
                                      "prévoyez `colruyt-mcp login`.")
        sso_days = out["cookies"].get("sso_days_left")
        out["login_needed"] = not out["cookies"]["sso_session"] or sso_days == 0
        if out["login_needed"]:
            out["login_note"] = ("La connexion unique (30 jours) est absente ou expirée : une nouvelle "
                                 "connexion manuelle (mot de passe + SMS) est nécessaire. "
                                 + ("Utilisez request_login." if config.LOGIN_URL else "Lancez `colruyt-mcp login`."))
        elif sso_days is not None and sso_days < 5:
            out["sso_warning"] = (f"La connexion unique expire dans {sso_days} jour(s) : prévoyez une "
                                  "nouvelle connexion manuelle (mot de passe + SMS).")
        return out

    # -- history
    async def history_ids(self) -> set[str]:
        if self._history is None:
            ids: set[str] = set()
            for fn in (self.c.clp.most_bought, self.c.clp.last_ticket):
                try:
                    ids |= {p["technicalArticleNumber"] for p in await fn()}
                except ColruytError:
                    pass
            self._history = ids
        return self._history

    async def purchase_history(self, kind: str) -> list[dict]:
        if kind == "last_ticket":
            return [summarize_clp(p) for p in await self.c.clp.last_ticket()]
        if kind == "most_bought":
            return [summarize_clp(p) for p in await self.c.clp.most_bought()]
        data = await (self.c.cogo.last_order() if kind == "cogo_last_order" else self.c.cogo.top_products())
        out = []
        for lst in data.get("list", []):
            for cat in lst.get("listItems", []):
                for p in cat.get("products", []):
                    out.append({"name": p.get("productName"), "category": cat.get("categoryName"),
                                "commercial_article_number": p.get("itemPartNumber"),
                                "cogo_product_id": p.get("productId"), "available": p.get("available")})
        return out

    # -- recipes
    def _recipe_summary(self, r: dict) -> dict:
        return {"recipe_id": r.get("recipeId"), "name": _loc(r.get("name")),
                "servings": r.get("numberOfServings"), "total_time_min": (r.get("totalTime") or 0) // 60 or None,
                "nutriscore": r.get("nutriscore") or None}

    async def favorite_recipes(self, offset: int = 0, limit: int = 30) -> dict:
        ids = await self.c.cogo.favourite_recipe_ids()
        page = ids[offset:offset + limit]
        recipes = await self.c.cogo.search_recipes(ids=page, limit=len(page)) if page else []
        by_id = {r["recipeId"]: r for r in recipes}
        return {"total": len(ids), "offset": offset,
                "recipes": [self._recipe_summary(by_id[i]) for i in page if i in by_id]}

    async def search_recipes(self, text: str, limit: int = 10) -> list[dict]:
        return [self._recipe_summary(r) for r in await self.c.cogo.search_recipes(text=text, limit=limit)]

    async def recipe(self, recipe_id: str) -> dict:
        details = await self.c.cogo.recipe_details([recipe_id])
        if not details:
            raise ColruytError(f"Recette {recipe_id} introuvable.")
        r = details[0]
        return self._recipe_summary(r) | {
            "ingredients": [{"text": i.get("ingredientStringConcatenated"), "quantity": i.get("quantity"),
                             "unit": (i.get("unit") or {}).get("shortName")}
                            for i in sorted(r.get("ingredientList", []), key=lambda i: i.get("displaySeq", 0))],
            "instructions": [
                " — ".join(t for t in (_loc((s.get("instruction") or {}).get("title")),
                                       _loc((s.get("instruction") or {}).get("description"))) if t)
                for s in sorted(r.get("recipeInstruction", []), key=lambda s: s.get("order", 0))],
        }

    async def _branch_id(self) -> str:
        cart = await self.c.cogo.cart()
        if cart.get("x_handoverPointId"):
            return cart["x_handoverPointId"]
        return (await self.c.clp.get(f"{config.CLP_BFF}/cg/preferredStore"))["value"]

    async def map_recipes(self, recipes: list[dict], candidates: int = 3) -> dict:
        """recipes = [{recipe_id, servings}]. Returns, per ingredient line, ranked product candidates."""
        branch = await self._branch_id()
        history = await self.history_ids()
        results = await asyncio.gather(*(self.c.cogo.productize(r["recipe_id"], int(r.get("servings") or 4), branch)
                                         for r in recipes))
        details = {d["recipeId"]: d for d in await self.c.cogo.recipe_details([r["recipe_id"] for r in recipes])}
        lines, all_ids = [], []
        for r, res in zip(recipes, results):
            d = details.get(r["recipe_id"], {})
            # productize lines come in the recipe's ingredient order, but in Dutch: use the localized text
            texts = [i.get("ingredientStringConcatenated")
                     for i in sorted(d.get("ingredientList", []), key=lambda i: i.get("displaySeq", 0))]
            for line in sorted(res.get("lines", []), key=lambda line: line.get("order", 0)):
                order = line.get("order", 0)
                cands = []
                for hit in line.get("results", []):
                    ids = {i["type"]: i["identifier"] for i in (hit.get("article") or {}).get("identifiers", [])}
                    tech = ids.get("technicalArticleNumber")
                    if tech and tech not in [c["technical_article_number"] for c in cands]:
                        calc = hit.get("calculation") or {}
                        cands.append({"technical_article_number": tech, "needed": calc.get("amount"),
                                      "needed_unit": calc.get("unit"), "bought_before": tech in history})
                # previously bought products first, server order otherwise
                cands.sort(key=lambda c: not c["bought_before"])
                cands = cands[:candidates]
                all_ids += [c["technical_article_number"] for c in cands]
                lines.append({"recipe": _loc(d.get("name")) or r["recipe_id"], "servings": r.get("servings"),
                              "recipe_base_servings": d.get("numberOfServings"),
                              "ingredient": texts[order] if order < len(texts) and len(texts) == len(res.get("lines", []))
                              else line.get("line"),
                              "pantry_item": line.get("ironStock", False),
                              "candidates": cands})
        details = {p["technicalArticleNumber"]: summarize_clp(p)
                   for p in await self.c.clp.products_by_tech(sorted(set(all_ids)))}
        missing = sorted(set(all_ids) - set(details))
        if missing:
            for e in await self.c.cogo.products_by_tech(missing):
                w = summarize_wcs(e)
                details[w["technical_article_number"]] = w | {"in_promo": bool(w["promotions"])}
        for line in lines:
            for cand in line["candidates"]:
                d = details.get(cand["technical_article_number"], {})
                cand |= {k: d.get(k) for k in ("name", "price", "price_per_unit", "unit_of_measure", "in_promo", "available")}
                if cand.get("needed_unit") == "pcs" and cand.get("needed"):
                    cand["suggested_quantity"] = math.ceil(cand["needed"])
        return {"pickup_branch_id": branch, "lines": lines,
                "note": "Les 'pantry_item' (sel, poivre, huile…) sont souvent déjà à la maison."}

    # -- products
    async def search_products(self, query: str, size: int = 10, **filters) -> dict:
        data = await self.c.clp.search_products(query, size=size, **filters)
        history = await self.history_ids()
        return {"total": data.get("totalProductsFound"),
                "products": [summarize_clp(p) | {"bought_before": p.get("technicalArticleNumber") in history}
                             for p in data.get("products", [])]}

    async def products(self, tech_ids: list[str]) -> list[dict]:
        clp = {p["technicalArticleNumber"]: summarize_clp(p) for p in await self.c.clp.products_by_tech(tech_ids)}
        cogo = {e.get("x_techArticleId"): summarize_wcs(e) for e in await self.c.cogo.products_by_tech(tech_ids)}
        return [clp.get(t, {"technical_article_number": t}) | {"collect_and_go": cogo.get(t)} for t in tech_ids]

    # -- Xtra list
    async def xtra_list(self, include_completed: bool = False) -> dict:
        data = await self.c.clp.shopping_list()
        items = [{"item_id": i["id"], "description": i.get("description"),
                  "technical_article_number": (i.get("productData") or {}).get("productId"),
                  "quantity": (i.get("productData") or {}).get("quantity"),
                  "unit": (i.get("productData") or {}).get("unitCode"),
                  "done": i.get("completedAt") is not None}
                 for i in data["items"] if include_completed or i.get("completedAt") is None]
        return {"list_name": (data["list"] or {}).get("name"), "count": len(items), "items": items}

    async def add_to_xtra_list(self, items: list[dict]) -> dict:
        """items = [{technical_article_number?, description?, quantity?, unit? (P|W)}]"""
        tech_ids = [str(i["technical_article_number"]) for i in items if i.get("technical_article_number")]
        names = {p["technicalArticleNumber"]: summarize_clp(p)["name"] for p in await self.c.clp.products_by_tech(tech_ids)}
        body = [xtra_list_entry(i, names) for i in items]
        await self.c.clp.add_list_items(body)
        return {"added": [b["description"] for b in body]}

    async def update_xtra_item(self, item_id: str, quantity: float | None = None, done: bool | None = None) -> dict:
        data = await self.c.clp.shopping_list()
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        if not item:
            raise ColruytError(f"Article {item_id} absent de la liste.")
        upd = {k: item.get(k) for k in ("id", "description", "productData", "completedAt", "createdAt")}
        if isinstance(upd["createdAt"], (int, float)):  # the list returns epoch seconds, updates want ISO
            upd["createdAt"] = datetime.fromtimestamp(upd["createdAt"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if quantity is not None:
            upd["productData"] = dict(upd.get("productData") or {"productId": "", "unitCode": "P"},
                                      quantity=list_quantity(quantity))
        if done is not None:
            upd["completedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if done else None
        await self.c.clp.update_list_items([upd])
        return {"updated": upd["description"]}

    async def remove_xtra_item(self, item_id: str) -> dict:
        await self.c.clp.remove_list_item(item_id)
        return {"removed": item_id}

    # -- cart
    async def cart(self) -> dict:
        cart, basket = await asyncio.gather(self.c.cogo.cart(), self.c.cogo.basket())
        items = basket.get("basket", {}).get("basketItems", [])
        names = {}
        if items:
            params = [("id", i["productId"]) for i in items] + list(
                (await self.c.cogo.base_params(pageSize=str(len(items)), pageNumber="1", appVersion="v2")).items())
            view = await self.c.cogo.call("GET", "wcsproductviewsearchsvc", "v1", "/productview/byIds", params=params)
            names = {e["uniqueID"]: e for e in view.get("catalogEntryView", [])}
        return {
            "order_id": cart.get("orderId"),
            "pickup": {"branch_id": cart.get("x_handoverPointId"), "name": cart.get("x_handoverPointTitle"),
                       "service": cart.get("x_handoverServiceId"), "date": basket.get("handoverDate"),
                       "from": basket.get("handoverTimeFrom"), "to": basket.get("handoverTimeTo"),
                       "modifiable_until": f"{basket.get('aortDate')} {basket.get('aortTime')}"},
            # Observed on a real cart: x_grandTotal = totalProductPrice + totalAdjustment (promotions,
            # negative); the service fee is added only once a pick-up slot is chosen. Line totals
            # (x_totalProductsAmount) can differ slightly (deposits, rounding).
            "totals": {"products_full_price": _num(cart.get("totalProductPrice")),
                       "promotions_discount": _num(cart.get("totalAdjustment")),
                       "products_after_promotions": _num(cart.get("x_grandTotal")),
                       "service_fee": _num(cart.get("x_totalServiceFee")),
                       "service_fee_discount": _num(cart.get("x_totalServiceFeeDiscount")),
                       "note": "products_after_promotions = produits moins promotions, hors frais de service "
                               "(ajoutés quand un créneau est réservé). Montant définitif : sur collectandgo.be."},
            "lines": [{"order_item_id": i["orderItemId"], "name": (names.get(i["productId"]) or {}).get("name"),
                       "technical_article_number": (names.get(i["productId"]) or {}).get("x_techArticleId"),
                       "quantity": i.get("quantity"), "unit": i.get("unitType"), "step": i.get("step"),
                       "total": i.get("totalProduct"),
                       "promotions": [summarize_promo(p) for p in i.get("promotions") or []]} for i in items],
        }

    async def add_to_cart(self, items: list[dict]) -> dict:
        """items = [{technical_article_number, quantity, unit? (P|PA|W|PR)}]"""
        entries = {e.get("x_techArticleId"): e for e in
                   await self.c.cogo.products_by_tech([str(i["technical_article_number"]) for i in items])}
        order_items, errors = [], []
        for i in items:
            e = entries.get(str(i["technical_article_number"]))
            if not e:
                errors.append(f"{i['technical_article_number']} : introuvable sur Collect&Go")
                continue
            try:
                order_items.append(order_item(e, float(i.get("quantity", 1)), i.get("unit")))
            except ColruytError as exc:
                errors.append(str(exc))
        added = []
        if order_items:
            res = await self.c.cogo.add_to_cart(order_items)
            added = [{"order_item_id": o.get("orderItemId"), "quantity": o.get("quantity"),
                      "promotion": o.get("xitem_promoStrapLine")} for o in res.get("orderItem", [])]
        return {"added": added, "errors": errors,
                "reminder": "Vérifiez de temps en temps avec get_cart (quantités, frais de service)."}

    async def update_cart_item(self, order_item_id: str, quantity: float) -> dict:
        basket = await self.c.cogo.basket()
        line = next((i for i in basket["basket"]["basketItems"] if i["orderItemId"] == order_item_id), None)
        if not line:
            raise ColruytError(f"Ligne {order_item_id} absente du panier.")
        entry = (await self.c.cogo.call("GET", "wcsproductviewsearchsvc", "v1", "/productview/byIds",
                                        params=[("id", line["productId"])] + list((await self.c.cogo.base_params(pageSize="1", pageNumber="1", appVersion="v2")).items()))
                 ).get("catalogEntryView", [{}])[0]
        unit = line.get("unitType") or "P"
        weight = wcs_attributes(entry).get("IsWeightArticle") == "Y"
        step = line.get("step") or 1
        field2 = f"{unit}|{step:g}|{'kg' if weight else 'st'}"
        qty = round(quantity * step, 3)
        res = await self.c.cogo.update_cart(basket["orderId"], [{"orderItemId": order_item_id, "quantity": f"{qty:g}",
                                                                 "xitem_field2": field2}])
        return {"order_item_id": order_item_id, "quantity": qty if quantity else 0,
                "removed": quantity == 0, "promotion": ((res or {}).get("orderItem") or [{}])[0].get("xitem_promoStrapLine")}

    # -- pickup
    async def pickup_points(self, latitude: float | None, longitude: float | None, radius_km: int, search: str | None) -> list:
        if latitude is None or longitude is None:
            if search:
                latitude, longitude, radius_km = 50.6, 4.6, max(radius_km, 200)  # centre of Belgium
            else:
                current = (await self.c.cogo.cart()).get("x_handoverPointId")
                geo = (await self.c.cogo.pickup_point_details(current)).get("geoCoordinates") or {}
                latitude, longitude = geo.get("latitude", 50.6), geo.get("longitude", 4.6)
        places = await self.c.cogo.places_near(latitude, longitude, radius_km)
        out = []
        for p in places:
            if config.HANDOVER_SERVICE not in (p.get("handoverServices") or []):
                continue
            addr = p.get("address") or {}
            item = {"branch_id": p.get("branchId"), "name": p.get("commercialName"),
                    "address": f"{addr.get('streetName', '')} {addr.get('houseNumber', '')}, {addr.get('postalcode', '')} {addr.get('cityName', '')}".strip(),
                    "distance_km": round(p.get("distance") or 0, 1)}
            if search and search.lower() not in (item["name"] + " " + item["address"]).lower():
                continue
            out.append(item)
        return out[:25]

    async def set_pickup_point(self, branch_id: str) -> dict:
        res = await self.c.cogo.reserve_pickup_point(branch_id)
        cart = await self.c.cogo.cart()
        return {"response": res, "pickup_now": cart.get("x_handoverPointTitle"), "branch_id": cart.get("x_handoverPointId")}

    async def _windows(self, days: int) -> tuple[dict, list[dict]]:
        cart = await self.c.cogo.cart()
        start = date.today() + timedelta(days=1)
        data = await self.c.cogo.slots(cart["x_handoverPointId"], cart["orderId"],
                                       start.strftime("%Y%m%d"), (start + timedelta(days=days)).strftime("%Y%m%d"))
        slots = []
        for day in data.get("handoverWindowsView", []):
            for w in day.get("handoverWindows", []):
                fee = (w.get("serviceFees") or [{}])[0]
                slots.append({"date": day["handoverDate"], "from": w["handoverWindowStart"], "to": w["handoverWindowEnd"],
                              "available": bool(w.get("windowIsAvailable") and w.get("orderable")),
                              "service_fee": fee.get("serviceFee"), "_fee_tech": fee.get("techArtId"),
                              "_seller": fee.get("sellingPartnerId", "COGOBE"),
                              "_aort": w.get("aort"), "_port": w.get("port"),
                              "_point": day["handoverPointId"], "_service": day["handoverServiceId"],
                              "_min": w.get("minimumOrderValue", 0)})
        return cart, slots

    async def pickup_slots(self, days: int = 7) -> list[dict]:
        _, slots = await self._windows(days)
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in slots]

    async def reserve_pickup_slot(self, slot_date: str, start: str) -> dict:
        _, slots = await self._windows(10)
        s = next((x for x in slots if x["date"] == slot_date and x["from"] == start), None)
        if not s:
            raise ColruytError(f"Créneau {slot_date} {start} introuvable.")
        if not s["available"]:
            raise ColruytError(f"Créneau {slot_date} {start} indisponible.")
        basket = await self.c.cogo.basket()
        body = {"handoverDate": s["date"], "handoverFrom": s["from"], "handoverTo": s["to"],
                "sellingPartnerId": s["_seller"], "handoverPointId": s["_point"], "handoverServiceId": s["_service"],
                "serviceFulfilmentTechArtId": str(s["_fee_tech"]),
                "aort": f"{s['_aort']['date']} {s['_aort']['time']}", "port": f"{s['_port']['date']} {s['_port']['time']}",
                "fulfilmentPathIndicators": basket.get("fulfilmentPath") or [], "minimumOrderValue": s["_min"]}
        check = await self.c.cogo.validate_slot(body)
        if check.get("cancelOrder") or check.get("productsNotInRange"):
            return {"reserved": False, "reason": "Le site signale un problème", "validation": check}
        res = await self.c.cogo.reserve_slot(body)
        return {"reserved": res.get("message") == "OK", "slot": {k: s[k] for k in ("date", "from", "to", "service_fee")},
                "price_increase": check.get("priceIncrease")}

    # -- service-fee actions
    async def service_fee_actions(self) -> list[dict]:
        actions: dict[str, dict] = {}
        page = 1
        while True:
            data = await self.c.cogo.search_products("*", facet='x_offers:"FREESERVICEOFFER"', page=page, size=100)
            entries = data.get("catalogEntryView", [])
            for e in entries:
                attrs = wcs_attributes(e)
                brand = attrs.get("BrandName") or attrs.get("Marques") or attrs.get("Merken") or ""
                for p in e.get("promotions", []):
                    if p.get("promoType") != "71":
                        continue
                    aid = p.get("commercialId") or p.get("couponId")
                    a = actions.setdefault(aid, {
                        "action_id": aid, "threshold": _num(p.get("staffelFrom")),
                        "threshold_unit": {"A": "EUR", "P": "pièces"}.get(p.get("staffelUnit"), p.get("staffelUnit")),
                        "from": p.get("startDate"), "to": p.get("endDate"),
                        "brands": set(), "product_ids": set(), "tech_ids": set(), "sample_product": e.get("uniqueID")})
                    a["brands"].add(brand.strip().title())
                    for pid in (e.get("uniqueID"), e.get("singleSKUCatalogEntryID")):
                        if pid:
                            a["product_ids"].add(pid)
                    if e.get("x_techArticleId"):
                        a["tech_ids"].add(e["x_techArticleId"])
            if len(entries) < 100 or page >= 10:
                break
            page += 1
        out = []
        for a in actions.values():
            out.append(a | {"brands": sorted(b for b in a["brands"] if b), "product_count": len(a["tech_ids"]),
                            "product_ids": sorted(a["product_ids"]), "tech_ids": sorted(a["tech_ids"])})
        return sorted(out, key=lambda a: a["brands"])

    async def service_fee_action_details(self, action_id: str) -> dict:
        action = next((a for a in await self.service_fee_actions() if a["action_id"] == action_id), None)
        if not action:
            raise ColruytError(f"Action {action_id} introuvable cette semaine.")
        params = [("id", action["sample_product"])] + list((await self.c.cogo.base_params(pageSize="1", pageNumber="1", appVersion="v2")).items())
        entry = (await self.c.cogo.call("GET", "wcsproductviewsearchsvc", "v1", "/productview/byIds", params=params)
                 ).get("catalogEntryView", [{}])[0]
        promo = next((p for p in entry.get("promotions", []) if p.get("promoType") == "71"), {})
        tiers = [{"discount_pct": 100, "threshold": action["threshold"], "unit": action["threshold_unit"]}]
        action["description"] = None
        if promo.get("couponId") and promo.get("commercialId"):
            text = html.unescape(re.sub(r"<[^>]+>", " ", await self.c.cogo.promotion_page(
                promo["couponId"], promo["commercialId"], promo.get("couponVersion") or "SCP-S_0")))
            text = re.sub(r"\s+", " ", text)
            m = re.search(r"(\d+)\s*%\s*de réduction sur vos frais de service à l.achat de\s*(\d+(?:[.,]\d+)?)\s*(€|pi[eè]ces?)", text)
            if m:
                tiers.insert(0, {"discount_pct": int(m.group(1)), "threshold": _num(m.group(2)),
                                 "unit": "EUR" if m.group(3) == "€" else "pièces"})
            banner = re.search(r"Faites des économies sur vos frais de service(.{0,400}?)Combinez", text)
            action["description"] = banner.group(1).strip() if banner else None
        self._tiers[(action_id, action["to"])] = tiers
        products = await self.c.cogo.products_by_tech(action["tech_ids"])
        return {k: v for k, v in action.items() if k not in ("product_ids", "tech_ids", "sample_product")} | {
            "tiers": tiers, "products": [summarize_wcs(p) for p in products]}

    async def service_fee_progress(self) -> dict:
        actions = await self.service_fee_actions()
        cart = await self.cart()
        basket = await self.c.cogo.basket()
        lines = basket.get("basket", {}).get("basketItems", [])
        progress = []
        for a in actions:
            ids = set(a["product_ids"])
            mine = [ln for ln in lines if ln["productId"] in ids]
            if a["threshold_unit"] == "EUR":
                reached = round(sum(ln.get("totalProduct") or 0 for ln in mine), 2)
            else:
                reached = sum(ln.get("quantity") or 0 for ln in mine)
            key = (a["action_id"], a["to"])
            if mine and key not in self._tiers:
                await self.service_fee_action_details(a["action_id"])  # fetch the 50 % tier when relevant
            tiers = self._tiers.get(key) or [{"discount_pct": 100, "threshold": a["threshold"], "unit": a["threshold_unit"]}]
            progress.append({
                "action_id": a["action_id"], "brands": a["brands"], "unit": a["threshold_unit"], "in_cart": reached,
                "tiers": [t | {"missing": max(0, round(t["threshold"] - reached, 2)), "reached": reached >= t["threshold"]}
                          for t in tiers],
            })
        best = lambda p: max((t["discount_pct"] for t in p["tiers"] if t["reached"]), default=0)
        gap = lambda p: min(t["missing"] / (t["threshold"] or 1) for t in p["tiers"])
        progress.sort(key=lambda p: (-best(p), gap(p)))
        return {"cart_totals": cart["totals"], "actions": progress,
                "note": "Le palier 50 % n'est connu que pour les actions déjà entamées (ou via "
                        "service_fee_action_details). Le site applique la réduction lui-même : vérifiez "
                        "service_fee_discount dans get_cart."}
