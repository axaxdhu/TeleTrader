"""Generate a daily FYERS access token and store it in ``.env``.

The FYERS counterpart to ``kite_login.py``. FYERS access tokens expire daily, so
a fresh one must be generated before each trading day. This helper does the
**token exchange** only — you still log in yourself in the browser (FYERS ID +
TOTP + PIN), so no password/2FA is ever automated or stored (consistent with the
app's manual-auth design).

Default (auto-capture) flow — hands-off:

1. Run ``uv run python fyers_login.py``.
2. Your browser opens the FYERS login page automatically. Log in and approve.
3. FYERS redirects to your app's Redirect URI (a local address such as
   ``http://localhost:8765``); a tiny local server running here catches the
   ``auth_code`` automatically — nothing to copy or paste.
4. The script exchanges it (with ``FYERS_SECRET_ID``) for the day's access token
   and writes ``FYERS_ACCESS_TOKEN`` into ``.env``.

The Redirect URI configured in your FYERS app (https://myapi.fyers.in) MUST match
``--redirect-uri`` (default ``http://localhost:8765``), and its host/port must
match ``--host``/``--port``. If it can't be localhost, use ``--manual`` (paste the
redirect URL yourself) or ``--auth-code``.

Then start the app: ``uv run python main.py`` (with the channel's broker = fyers).

``FYERS_APP_ID`` and ``FYERS_SECRET_ID`` must already be set in ``.env``. The auth
code is single-use and short-lived, so generate and exchange it in one go.
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

_ENV_VAR = "FYERS_ACCESS_TOKEN"
_DEFAULT_ENV = Path(__file__).resolve().parent / ".env"
_DEFAULT_REDIRECT = "http://localhost:8765"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765
_CAPTURE_TIMEOUT_S = 180

_SUCCESS_HTML = (
    b"<!doctype html><html><body style='font-family:sans-serif;text-align:center;"
    b"margin-top:4rem'><h2>&#10003; Login captured.</h2>"
    b"<p>You can close this tab and return to the terminal.</p></body></html>"
)
_FAILURE_HTML = (
    b"<!doctype html><html><body style='font-family:sans-serif;text-align:center;"
    b"margin-top:4rem'><h2>No auth_code in this request.</h2></body></html>"
)


def _extract_auth_code(pasted: str) -> str:
    """Return the auth code from a raw code or a full redirect URL."""
    pasted = pasted.strip()
    if "auth_code=" in pasted or pasted.lower().startswith("http"):
        query = parse_qs(urlparse(pasted).query)
        codes = query.get("auth_code")
        if not codes or not codes[0]:
            raise ValueError("No 'auth_code' found in the pasted URL")
        return codes[0]
    return pasted


def _mask(secret: str) -> str:
    """Show only the ends of a secret, e.g. ``abcd…wxyz``."""
    if len(secret) <= 8:
        return "…"
    return f"{secret[:4]}…{secret[-4:]}"


def _write_token(env_path: Path, token: str) -> None:
    """Set (or append) ``FYERS_ACCESS_TOKEN`` in ``env_path`` without touching else."""
    line = f"{_ENV_VAR}={token}\n"
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
    else:
        lines = []

    for i, existing in enumerate(lines):
        if existing.lstrip().split("=", 1)[0].strip() == _ENV_VAR and not existing.lstrip().startswith("#"):
            lines[i] = line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(line)

    env_path.write_text("".join(lines))


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures ``auth_code`` from the FYERS login redirect, then stops."""

    auth_code: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        code = parse_qs(urlparse(self.path).query).get("auth_code", [None])[0]
        if code:
            _CallbackHandler.auth_code = code
            body, status = _SUCCESS_HTML, 200
        else:
            body, status = _FAILURE_HTML, 400  # e.g. the browser's /favicon.ico
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence default request logging
        pass


def _capture_via_server(login_url: str, host: str, port: int, open_browser: bool) -> str:
    """Open the login page and capture the redirected ``auth_code`` locally."""
    _CallbackHandler.auth_code = None
    server = HTTPServer((host, port), _CallbackHandler)
    server.timeout = 1
    try:
        print("\nOpening the FYERS login page in your browser…")
        print(f"If it doesn't open, visit:\n   {login_url}\n")
        if open_browser:
            webbrowser.open(login_url)
        print(f"Waiting for the login redirect on http://{host}:{port}/ …")
        deadline = time.monotonic() + _CAPTURE_TIMEOUT_S
        while _CallbackHandler.auth_code is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if _CallbackHandler.auth_code is None:
        raise TimeoutError(
            f"No redirect received within {_CAPTURE_TIMEOUT_S}s. Check that your "
            f"FYERS app's Redirect URI is http://{host}:{port} (or use --manual)."
        )
    return _CallbackHandler.auth_code


def _capture_via_paste(login_url: str) -> str:
    """Fallback: print the login URL and read the auth code from stdin."""
    print("\n1) Open this URL, log in, and approve:\n")
    print(f"   {login_url}\n")
    print(
        "2) After login your browser is redirected to your app's Redirect URI\n"
        "   containing 'auth_code=...'.\n"
    )
    raw = input("3) Paste the auth_code (or the whole redirect URL) here: ")
    return _extract_auth_code(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--auth-code",
        help="Skip login entirely and exchange this auth code (or redirect URL).",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Paste the auth code yourself instead of auto-capturing it.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser (still auto-captures the redirect).",
    )
    parser.add_argument(
        "--redirect-uri",
        default=_DEFAULT_REDIRECT,
        help=f"Redirect URI registered in your FYERS app (default {_DEFAULT_REDIRECT}).",
    )
    parser.add_argument("--host", default=_DEFAULT_HOST, help="Callback host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="Callback port (default 8765).")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=_DEFAULT_ENV,
        help=f"Path to the .env file to update (default: {_DEFAULT_ENV}).",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file)

    import os

    app_id = os.getenv("FYERS_APP_ID")
    secret_id = os.getenv("FYERS_SECRET_ID")
    if not app_id or not secret_id:
        print("FYERS_APP_ID and FYERS_SECRET_ID must be set in .env first.", file=sys.stderr)
        return 1

    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=args.redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    login_url = session.generate_authcode()

    try:
        if args.auth_code:
            auth_code = _extract_auth_code(args.auth_code)
        elif args.manual:
            auth_code = _capture_via_paste(login_url)
        else:
            try:
                auth_code = _capture_via_server(
                    login_url, args.host, args.port, open_browser=not args.no_browser
                )
            except OSError as exc:
                print(f"Could not start the local callback server: {exc}", file=sys.stderr)
                print("Falling back to manual paste.\n", file=sys.stderr)
                auth_code = _capture_via_paste(login_url)
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return 1
    except (ValueError, TimeoutError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    session.set_token(auth_code)
    response = session.generate_token()
    access_token = response.get("access_token") if isinstance(response, dict) else None
    if not access_token:
        print(f"FYERS rejected the token exchange: {response}", file=sys.stderr)
        print(
            "The auth code is single-use and expires quickly — re-run and try "
            "again with a fresh login.",
            file=sys.stderr,
        )
        return 1

    _write_token(args.env_file, access_token)

    print(f"\n✓ Wrote {_ENV_VAR}={_mask(access_token)} to {args.env_file}")
    print("  Valid for today's session — start the app with the channel's broker = fyers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
