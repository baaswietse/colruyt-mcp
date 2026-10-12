"""colruyt-mcp CLI: `serve` (default: stdio MCP; `--http` for streamable HTTP with a bearer
token), `login`, `refresh`, `status`."""
import argparse
import asyncio
import json

from .session import CookieStore, auto_refresh, http_refresh, interactive_login, silent_refresh


def main() -> None:
    parser = argparse.ArgumentParser(prog="colruyt-mcp")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "login", "refresh", "status"])
    parser.add_argument("--site", choices=["cogo", "colruyt", "all"], default="all",
                        help="refresh : site(s) à reconnecter (défaut : all)")
    parser.add_argument("--method", choices=["auto", "http", "browser"], default="auto",
                        help="refresh : HTTP (sans navigateur), navigateur, ou auto = HTTP puis navigateur")
    parser.add_argument("--browser", help="login: edge | chrome | chromium | chemin de l'exécutable "
                                          "(par défaut : celui du dernier login, sinon détection automatique)")
    parser.add_argument("--http", action="store_true", help="serve : transport HTTP (COLRUYT_MCP_TOKEN requis)")
    parser.add_argument("--host", default="0.0.0.0", help="serve --http : adresse d'écoute (défaut 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="serve --http : port (défaut 8765)")
    args = parser.parse_args()
    cmd = args.command
    store = CookieStore()
    if cmd == "serve":
        from .server import main as serve
        serve("streamable-http" if args.http else "stdio", args.host, args.port)
    elif cmd == "login":
        print(json.dumps(interactive_login(store, args.browser), indent=1, ensure_ascii=False))
    elif cmd == "refresh":
        sites = ("colruyt", "cogo") if args.site == "all" else (args.site,)
        fn = {"auto": auto_refresh, "http": http_refresh, "browser": silent_refresh}[args.method]
        print(json.dumps(fn(store, sites), indent=1, ensure_ascii=False))
    else:
        from .client import ColruytClient
        from .logic import Service

        async def status():
            svc = Service(ColruytClient(store))
            try:
                return await svc.session_status()
            finally:
                await svc.c.aclose()

        print(json.dumps(asyncio.run(status()), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
