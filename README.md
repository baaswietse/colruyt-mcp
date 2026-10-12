# colruyt-mcp

*Français : [README.fr.md](README.fr.md)*

> **Unofficial, personal-use project.** Not affiliated with, endorsed by or supported by Colruyt
> Group, Xtra or Collect&Go. Read the [disclaimer](#disclaimer) before using it.

A local (stdio) [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI
assistant help you with your own Colruyt account: search the catalogue, browse Colruyt recipes
(including your favourites), turn recipes into product lists, manage your **Xtra** shopping list
(colruyt.be) and fill your **Collect&Go** cart, pick-up point and time slot. It also tracks the
weekly "free service fee" brand actions.

**It never places or pays an order.** Checkout, payment and cancellation endpoints are blocked in
the client; you always finish the order yourself on collectandgo.be.

## How it works

- Runs on your machine, for your account only. Nothing is sent anywhere else.
- **No credentials are handled.** You log in yourself, in a regular browser window (Chrome, Edge or
  Chromium) with a dedicated profile (SMS second factor included). The tool then reads the session cookies of
  that profile and replays them, the way your browser does.
- It does not bypass the sites' bot protection. Automated browsers get blocked, so login is
  manual by design. If the sites block a request, the tool stops and reports it.
- No catalogue or recipe data is stored. Responses are used for the current request only, apart
  from short-lived in-memory caches.

## Requirements

- Windows or Linux, with Google Chrome, Microsoft Edge or Chromium
- [uv](https://docs.astral.sh/uv/)
- A Colruyt Xtra account (Belgium)

```bash
uv sync
```

## Log in

```bash
uv run colruyt-mcp login
```

1. Your browser opens on the colruyt.be and Collect&Go login pages.
2. Log in on both tabs. Tick "stay signed in" if it is offered.
3. Press Enter in the terminal.

Other commands:

```bash
uv run colruyt-mcp status
```

```bash
uv run colruyt-mcp refresh
```

`status` checks both sessions. `refresh` tries a silent re-login: it briefly opens the browser used at login and reuses
the Azure B2C single sign-on if it is still valid.

Personal files live outside the repository, in `~/.colruyt-mcp/` (override with
`COLRUYT_MCP_HOME`): `cookies.json`, `browser.json` and `<browser>-profile/`. Keep them private: they give access to
your account.

### Choosing the browser

Without a choice, the tool looks for Edge, then Chrome, then Chromium on Windows. On Linux it
looks for `google-chrome`, then `chromium`, then `microsoft-edge`. To pick one:

```bash
uv run colruyt-mcp login --browser chrome
```

`--browser` accepts `chrome`, `edge`, `chromium` or the path to the executable. The
`COLRUYT_MCP_BROWSER` environment variable does the same. The browser used at login is
remembered: `refresh` reuses it with the same profile.

- Each browser gets its own profile, so switching browser means logging in again.
- Close any window already using the colruyt-mcp profile before `login` or `refresh`.
- Linux with snap Chromium: snap confinement hides dot-directories in your home. Set
  `COLRUYT_MCP_HOME` to a non-hidden folder (e.g. `~/colruyt-mcp-data`), or use the `.deb`
  Google Chrome.

### Session lifetime

The two sites behave very differently, and the tool treats them separately.

- **colruyt.be / Xtra** (shopping list, recipes, catalogue, history): the session cookie is
  persistent, about 60 days. These tools **never open a browser**; `session_status` shows the
  expiry and warns under 7 days left.
- **Collect&Go** (cart, pick-up, slots, service fee): the server session expires after about
  half an hour of inactivity. When a Collect&Go tool hits an expired session, the tool renews it
  automatically through the single sign-on, **without a new SMS code**, as long as the single
  sign-on is still valid (tick "stay signed in" at login so it is). The renewal is first tried
  with plain HTTP requests (no browser); only if that fails does the browser used at login open
  minimised for a few seconds. At most one automatic refresh every 5 minutes.
- **Single sign-on** (Azure B2C, login.myxtra.be): the "stay signed in" cookie lasts 30 days and
  is not extended by use. `session_status` shows `sso_expires` and sets `login_needed` when a new
  manual login (password + SMS) is due.

So a session that only uses Xtra tools never opens a browser at all. Set
`COLRUYT_MCP_AUTO_REFRESH=0` to disable the automatic Collect&Go refresh; the assistant then asks
you to run `refresh_session` instead. `refresh_session` takes a `site` (`collect_and_go` by
default, `colruyt`, or `all`) and a `method` (`auto`, `http`, `browser`); it is rarely needed.
If single sign-on has expired, you run `colruyt-mcp login` again (`--method http` and
`--method browser` exist on the `refresh` command too).

Only one machine should renew the Collect&Go session: each single sign-on reconnection ends the
previous Collect&Go server session. If the server mode below is in use, disable the automatic
refresh on your PC.

## Server mode

The server can run on a machine without a screen (a NAS, a container) and be reached over HTTP by
an assistant running elsewhere. The same cookie files are used; the manual login happens in a
browser that is always running on that machine and that you reach through a web desktop (for
example the `linuxserver/chromium` image, which streams a real Chromium over HTTPS).

- `COLRUYT_MCP_DEVTOOLS_PORT`: local DevTools port of that running browser (start it with
  `--remote-debugging-port=<port> --remote-debugging-address=127.0.0.1 --user-data-dir=<profile>`).
  `login` and `refresh` then open tabs in it instead of launching a browser; nothing is attached
  to the pages while they load, cookies are read once afterwards and the tabs are closed.
- `COLRUYT_MCP_LOGIN_URL`: the address of that web desktop. The tool `request_login` opens the
  login pages and returns this URL so the assistant can send it to you; after you have logged in
  (password + SMS), the assistant calls `finish_login`, which reads the cookies and checks both
  sessions.
- `colruyt-mcp serve --http [--host 0.0.0.0] [--port 8765]`: streamable HTTP transport on path
  `/mcp`. Every request must carry `Authorization: Bearer <token>`, with the token from
  `COLRUYT_MCP_TOKEN` or the file named by `COLRUYT_MCP_TOKEN_FILE`. Bind it to a private
  network only; the transport itself has no TLS.

The assistant's instructions (sent at `initialize`) tell it to watch `login_needed` and
`sso_warning` in `session_status`, to call `request_login`, to send you the URL, to wait for
your confirmation and then to call `finish_login`. It is never asked for, and never receives,
your credentials.

## Connect it to Claude

**Claude Desktop.** In Settings → Developer → Edit Config, add this under `mcpServers`, then quit
Claude completely and restart it:

```json
"colruyt": {
  "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
  "args": ["run", "--directory", "C:\\path\\to\\colruyt-mcp", "colruyt-mcp"]
}
```

On Linux, use the full path of `uv` (e.g. `/home/<you>/.local/bin/uv`) and a Linux path for
`--directory`.

**Claude Code:**

```bash
claude mcp add colruyt -- uv run --directory C:\path\to\colruyt-mcp colruyt-mcp
```

## Tools

| Area | Tools |
|---|---|
| Session | `session_status`, `refresh_session`, `request_login`, `finish_login` (server mode) |
| Catalogue | `search_products`, `get_products`, `get_purchase_history` (last ticket, most bought, Collect&Go history) |
| Recipes | `list_favorite_recipes`, `search_recipes`, `get_recipe`, `map_recipes_to_products` |
| Xtra list | `get_xtra_list`, `add_to_xtra_list`, `update_xtra_list_item`, `remove_xtra_list_item` |
| Collect&Go cart | `get_cart`, `add_to_cart`, `update_cart_item` (quantity 0 removes the line) |
| Pick-up | `list_pickup_points`, `set_pickup_point`, `get_pickup_slots`, `reserve_pickup_slot` |
| Service fee | `list_service_fee_actions`, `service_fee_action_details` (50 % / 100 % tiers), `service_fee_progress` |

Write tools act directly on your account. The server tells the assistant to re-read the cart or
list regularly to check the result.

Default language is French (`COLRUYT_MCP_LANG=nl` for Dutch).

## Development

```bash
uv run pytest
```

Tests are offline. `scripts/` holds the discovery tooling (HAR inspection, session replay);
captures go to `captures/`, which git ignores.

## Disclaimer

**Read this before use.** This summary is not legal advice.

**Unofficial.** This project is an independent, non-commercial, personal tool. It is not
affiliated with, authorised, endorsed or supported by Colruyt Group, Etn. Fr. Colruyt NV, Colruyt
Food Retail NV, Bio-Planet NV, Xtra or Collect&Go. The APIs it uses are undocumented internal
interfaces. They may change or disappear at any time, and the tool may break or misbehave as a
result.

**Terms of use apply to you.** Using these sites, with or without this tool, is governed by
Colruyt Group's terms. The versions read on 9 October 2026 contain, among others:

- **Website terms of use** (Colruyt Group sites, version 06/2019), art. 4–5: extracting or reusing
  the site's content, including screen scraping, is prohibited. Under database law, a lawful user
  may still extract **non-substantial** parts for normal use. Substantial or **systematic**
  extraction, and use that harms the database owner, are not allowed.
- **Xtra Digital general terms** (v0202609210), art. 17: the digital channels (software, source
  code, data, structure…) are protected. Reproduction, publication, adaptation or use is
  prohibited unless expressly authorised by Xtra, and so is storing their content in a database.
  Art. 14: Xtra may suspend the service, without notice, in case of (suspected) abuse,
  unauthorised use or a threat to system security.
- **Collect&Go general terms** (V0202601270): service for reasonable private, household use; the
  account may be blocked in case of abuse.
- The Collect&Go `robots.txt` disallows automated access to booking, slot, checkout and account
  pages. The colruyt.be `robots.txt` asks crawlers for a 5-second crawl delay.

None of these documents explicitly addresses personal automation tools or API clients. Their
wording is broad, though, and **this tool may not be considered an authorised use.** Possible
consequences include the suspension of your Xtra or Collect&Go account. Before using the tool,
read the current terms yourself. They are linked from the footers of colruyt.be, collectandgo.be
and monxtra.be.

**Use responsibly.** If you use the tool:

- use it only with **your own** account, for personal, household shopping;
- keep request volumes comparable to manual use. Do not scrape the catalogue, prices, recipes or
  promotions, and do not build datasets from them;
- do not redistribute Colruyt content (texts, images, recipes, product data);
- do not use it to circumvent bot protection, access controls or rate limits.

**No warranty.** The software is provided "as is", without warranty of any kind. You are solely
responsible for what it does on your account. That includes products added to your cart or
list, slots reserved, prices and promotions, which can be wrong or out of date. Always review your
cart on collectandgo.be before ordering. The author accepts no liability for any damage, loss,
account suspension or order error.

**Trademarks.** Colruyt, Xtra, Collect&Go, Bio-Planet, Boni and other names are trademarks of their
respective owners. They are used here only to describe what the software interoperates with.

If you represent Colruyt Group and have concerns about this project, please open an issue. It will
be addressed promptly, including taking the repository down if requested.

## License

[MIT](LICENSE). The licence covers this code only, not any Colruyt content or service.
