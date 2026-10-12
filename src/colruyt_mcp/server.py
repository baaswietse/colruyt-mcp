"""MCP server (stdio, or streamable HTTP with a bearer token) exposing Colruyt Xtra / Collect&Go
tools. Never places an order."""
import hmac
import logging
import os
import sys
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import browser, config
from .client import ColruytClient, ColruytError
from .logic import Service
from .session import CookieStore, http_refresh, open_login_tabs, silent_refresh

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Outils Colruyt : catalogue, recettes (dont favoris), liste Xtra (colruyt.be) et panier Collect&Go.
- Le MCP ne valide JAMAIS de commande ni de paiement : l'utilisateur termine lui-même sur collectandgo.be.
- Identifiant produit commun : technical_article_number.
- Recette → produits : map_recipes_to_products classe les candidats (déjà achetés d'abord). Écartez
  les 'pantry_item' sauf demande contraire.
- Les écritures (liste, panier, point d'enlèvement, créneau) sont directes : relisez régulièrement
  get_cart / get_xtra_list pour vérifier le résultat, surtout après plusieurs ajouts.
- Frais de service : list_service_fee_actions / service_fee_progress. Si un seuil est proche,
  proposez les produits de l'action. Vérifiez service_fee_discount dans get_cart.
- Sessions :
  - colruyt.be (liste Xtra, recettes, catalogue, historique) n'ouvre jamais de navigateur : son
    cookie dure environ 60 jours (voir colruyt_days_left dans session_status).
  - Collect&Go (panier, enlèvement, créneau, frais de service) expire après ~30 min d'inactivité.
    Le MCP la renouvelle seul au besoin : le navigateur s'ouvre réduit quelques secondes, sans SMS.
    Prévenez l'utilisateur que ça peut arriver.
  - Si une erreur de session persiste, demandez à l'utilisateur de lancer `colruyt-mcp login` dans
    un terminal (connexion manuelle + SMS). N'appelez pas refresh_session en boucle.
  - La connexion unique (SSO) dure 30 jours. Quand session_status indique login_needed (ou
    sso_warning), appelez request_login : il renvoie l'URL où l'utilisateur voit le navigateur du
    serveur et se connecte (mot de passe + SMS). Envoyez-lui cette URL, attendez qu'il confirme,
    puis appelez finish_login. Ne demandez jamais ses identifiants.
"""

mcp = MCPServer("colruyt", instructions=INSTRUCTIONS)
_state: dict = {}
READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def service() -> Service:
    if "svc" not in _state:
        _state["svc"] = Service(ColruytClient(CookieStore()))
    return _state["svc"]


async def run(coro):
    try:
        return await coro
    except ColruytError as exc:
        raise ToolError(str(exc)) from exc


class RecipeRequest(BaseModel):
    recipe_id: str
    servings: int = Field(4, ge=1, le=20)


class ListItem(BaseModel):
    technical_article_number: str | None = None
    description: str | None = Field(None, description="Texte libre si pas de produit précis")
    quantity: int = Field(1, ge=1, description="Entier (la liste Xtra refuse les décimales)")
    unit: Literal["P", "W"] = "P"


class CartItem(BaseModel):
    technical_article_number: str
    quantity: float = Field(1, gt=0, description="En unités choisies : pièces, packs, kg…")
    unit: Literal["P", "PA", "W", "PR"] | None = Field(None, description="Par défaut : 1re unité vendable")


# ------------------------------------------------------------------ session
@mcp.tool(annotations=READ)
async def session_status() -> dict:
    """État des sessions colruyt.be et Collect&Go. Si login_needed est vrai (connexion unique de 30 jours
    expirée) : appelez request_login, envoyez l'URL à l'utilisateur, attendez sa confirmation, puis
    finish_login. Si sso_warning : prévenez l'utilisateur. Collect&Go se renouvelle seul sinon."""
    return await run(service().session_status())


SITES = {"collect_and_go": ("cogo",), "colruyt": ("colruyt",), "all": ("colruyt", "cogo")}


@mcp.tool(annotations=WRITE)
async def refresh_session(site: Literal["collect_and_go", "colruyt", "all"] = "collect_and_go",
                          method: Literal["auto", "http", "browser"] = "auto") -> dict:
    """Reconnexion via la connexion unique (SSO, sans SMS) : par requêtes HTTP, sinon par le
    navigateur. Rarement utile : Collect&Go se renouvelle déjà automatiquement, et le cookie
    colruyt.be dure ~60 jours."""
    svc = service()
    sites = SITES[site]
    try:
        if method == "auto":
            result = await svc.c.refresh(sites)
        else:
            result = await svc.c.refresh_with(http_refresh if method == "http" else silent_refresh, sites)
    except Exception as exc:  # no browser, profile already in use, ...
        raise ToolError(f"Reconnexion impossible : {exc}") from exc
    return {"refresh": result, "status": await run(svc.session_status())}


@mcp.tool(annotations=WRITE)
async def request_login() -> dict:
    """Connexion manuelle (mot de passe + SMS, par l'utilisateur lui-même, jamais par vous) : ouvre les
    pages de connexion dans le navigateur du serveur et renvoie l'URL de ce navigateur. Envoyez l'URL à
    l'utilisateur, attendez qu'il confirme, puis appelez finish_login. Ne demandez jamais ses identifiants."""
    if browser.attached_port() is None:
        raise ToolError("Pas de navigateur attaché à ce serveur : l'utilisateur doit lancer "
                        "`colruyt-mcp login` sur la machine du serveur.")
    if _state.get("login_tabs"):
        port, tabs = _state["login_tabs"]
        for t in tabs:
            browser.close_tab(port, t)
    try:
        _state["login_tabs"] = open_login_tabs(("colruyt", "cogo"))
    except Exception as exc:
        raise ToolError(f"Impossible d'ouvrir les pages de connexion : {exc}") from exc
    return {"url": config.LOGIN_URL,
            "message": "Les pages de connexion colruyt.be et Collect&Go sont ouvertes dans le navigateur du "
                       "serveur. Demandez à l'utilisateur de s'y connecter (cocher « rester connecté »), "
                       "puis appelez finish_login."}


@mcp.tool(annotations=WRITE)
async def finish_login() -> dict:
    """Après que l'utilisateur a confirmé sa connexion manuelle (voir request_login) : enregistre la
    session depuis le navigateur du serveur, ferme les onglets de connexion et vérifie les deux sessions."""
    if not _state.get("login_tabs"):
        raise ToolError("Appelez d'abord request_login.")
    port, tabs = _state.pop("login_tabs")
    svc = service()
    try:
        result = await svc.c.collect_attached(port, tabs)
    except Exception as exc:
        raise ToolError(f"Lecture des cookies impossible : {exc}") from exc
    return {"login": result, "status": await run(svc.session_status())}


# ------------------------------------------------------------------ catalogue
@mcp.tool(annotations=READ)
async def search_products(query: str, size: int = 10, only_promo: bool = False, only_bio: bool = False) -> dict:
    """Recherche dans le catalogue Colruyt (prix du magasin préféré). bought_before = déjà acheté."""
    return await run(service().search_products(query, size=size, inPromo=only_promo, isBio=only_bio))


@mcp.tool(annotations=READ)
async def get_products(technical_article_numbers: list[str]) -> list[dict]:
    """Fiches produits (prix colruyt.be + disponibilité, unités et promos Collect&Go)."""
    return await run(service().products(technical_article_numbers))


@mcp.tool(annotations=READ)
async def get_purchase_history(
    kind: Literal["last_ticket", "most_bought", "cogo_last_order", "cogo_top"] = "most_bought",
) -> list[dict]:
    """Listes techniques : dernières courses (last_ticket), achetés le plus souvent (most_bought),
    dernière commande C&G, habituels C&G. Lecture seule."""
    return await run(service().purchase_history(kind))


# ------------------------------------------------------------------ recettes
@mcp.tool(annotations=READ)
async def list_favorite_recipes(offset: int = 0, limit: int = 30) -> dict:
    """Recettes favorites (communes Xtra / Collect&Go)."""
    return await run(service().favorite_recipes(offset, limit))


@mcp.tool(annotations=READ)
async def search_recipes(text: str, limit: int = 10) -> list[dict]:
    """Recherche de recettes Colruyt par texte."""
    return await run(service().search_recipes(text, limit))


@mcp.tool(annotations=READ)
async def get_recipe(recipe_id: str) -> dict:
    """Détail d'une recette : ingrédients, étapes, portions."""
    return await run(service().recipe(recipe_id))


@mcp.tool(annotations=READ)
async def map_recipes_to_products(recipes: list[RecipeRequest], candidates: int = 3) -> dict:
    """Pour chaque ingrédient des recettes (au nombre de portions voulu), produits candidats classés,
    avec quantité nécessaire, prix et 'bought_before'."""
    return await run(service().map_recipes([r.model_dump() for r in recipes], candidates))


# ------------------------------------------------------------------ liste Xtra
@mcp.tool(annotations=READ)
async def get_xtra_list(include_completed: bool = False) -> dict:
    """Liste de courses Xtra (colruyt.be)."""
    return await run(service().xtra_list(include_completed))


@mcp.tool(annotations=WRITE)
async def add_to_xtra_list(items: list[ListItem]) -> dict:
    """Ajoute des articles (produit ou texte libre) à la liste Xtra. Uniquement à la demande explicite de
    l'utilisateur ; relisez get_xtra_list ensuite."""
    return await run(service().add_to_xtra_list([i.model_dump() for i in items]))


@mcp.tool(annotations=WRITE)
async def update_xtra_list_item(item_id: str, quantity: int | None = None, done: bool | None = None) -> dict:
    """Modifie la quantité d'un article de la liste Xtra ou le coche/décoche."""
    return await run(service().update_xtra_item(item_id, quantity, done))


@mcp.tool(annotations=DELETE)
async def remove_xtra_list_item(item_id: str) -> dict:
    """Supprime un article de la liste Xtra."""
    return await run(service().remove_xtra_item(item_id))


# ------------------------------------------------------------------ panier C&G
@mcp.tool(annotations=READ)
async def get_cart() -> dict:
    """Panier Collect&Go : lignes, promos, point et créneau d'enlèvement, totaux (produits, promotions,
    produits après promotions ; frais de service à part, ajoutés quand un créneau est réservé)."""
    return await run(service().cart())


@mcp.tool(annotations=WRITE)
async def add_to_cart(items: list[CartItem]) -> dict:
    """Ajoute des produits au panier Collect&Go. Uniquement à la demande explicite de l'utilisateur ; ne
    valide jamais la commande (c'est lui qui termine sur collectandgo.be) ; relisez get_cart ensuite."""
    return await run(service().add_to_cart([i.model_dump() for i in items]))


@mcp.tool(annotations=DELETE)
async def update_cart_item(order_item_id: str, quantity: float) -> dict:
    """Change la quantité d'une ligne du panier (dans l'unité de la ligne). 0 = supprimer."""
    return await run(service().update_cart_item(order_item_id, quantity))


# ------------------------------------------------------------------ enlèvement
@mcp.tool(annotations=READ)
async def list_pickup_points(search: str | None = None, latitude: float | None = None,
                             longitude: float | None = None, radius_km: int = 25) -> list[dict]:
    """Points d'enlèvement Collect&Go : autour du point actuel, d'une position, ou filtrés par nom/ville/CP."""
    return await run(service().pickup_points(latitude, longitude, radius_km, search))


@mcp.tool(annotations=WRITE)
async def set_pickup_point(branch_id: str) -> dict:
    """Choisit le point d'enlèvement (branch_id de list_pickup_points)."""
    return await run(service().set_pickup_point(branch_id))


@mcp.tool(annotations=READ)
async def get_pickup_slots(days: int = 7) -> list[dict]:
    """Créneaux d'enlèvement des prochains jours, avec frais de service de chaque créneau."""
    return await run(service().pickup_slots(days))


@mcp.tool(annotations=WRITE)
async def reserve_pickup_slot(date: str, start: str) -> dict:
    """Réserve un créneau (date AAAAMMJJ, start HHMM, tels que renvoyés par get_pickup_slots), uniquement à
    la demande explicite de l'utilisateur. Ne valide pas la commande."""
    return await run(service().reserve_pickup_slot(date, start))


# ------------------------------------------------------------------ frais de service
@mcp.tool(annotations=READ)
async def list_service_fee_actions() -> list[dict]:
    """Actions « frais de service offerts » de la semaine : marques, seuil (EUR ou pièces), période."""
    actions = await run(service().service_fee_actions())
    return [{k: v for k, v in a.items() if k not in ("product_ids", "tech_ids", "sample_product")} for a in actions]


@mcp.tool(annotations=READ)
async def service_fee_action_details(action_id: str) -> dict:
    """Détail d'une action : paliers (50 % / 100 %), description et produits éligibles."""
    return await run(service().service_fee_action_details(action_id))


@mcp.tool(annotations=READ)
async def service_fee_progress() -> dict:
    """Progression du panier vers chaque seuil d'action frais de service."""
    return await run(service().service_fee_progress())


class TokenCheck:
    """ASGI wrapper: every HTTP request must carry `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = next((v for k, v in scope.get("headers", []) if k == b"authorization"), b"").decode()
            if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def read_token() -> str | None:
    token = os.environ.get("COLRUYT_MCP_TOKEN", "").strip()
    path = os.environ.get("COLRUYT_MCP_TOKEN_FILE", "").strip()
    if not token and path:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    return token or None


def main(transport: str = "stdio", host: str = "0.0.0.0", port: int = 8765) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    for noisy in ("httpx", "httpcore"):  # never log request URLs (they can carry tokens)
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if transport == "stdio":
        mcp.run("stdio")
        return
    token = read_token()
    if not token:
        sys.exit("COLRUYT_MCP_TOKEN (ou COLRUYT_MCP_TOKEN_FILE) est requis pour le transport HTTP.")
    import uvicorn

    from mcp.server.transport_security import TransportSecuritySettings

    log.info("colruyt-mcp HTTP sur %s:%s (chemin /mcp), navigateur attaché : %s", host, port,
             browser.attached_port() or "non")
    # The SDK's DNS-rebinding guard only accepts localhost Host headers; clients reach this server
    # by container name on a private network, and the bearer token already blocks browsers.
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    uvicorn.run(TokenCheck(app, token), host=host, port=port, log_level="warning")
