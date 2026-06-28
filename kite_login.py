"""Generate a daily Zerodha Kite access token and store it in ``.env``.

Kite access tokens expire every morning (~6 AM IST), so a fresh one must be
generated before each trading day. This helper does the **token exchange** only —
you still log in yourself in the browser, so no Zerodha password or 2FA is ever
automated or stored (consistent with the app's manual-auth design).

Default (auto-capture) flow — hands-off:

1. Run ``uv run python kite_login.py``.
2. Your browser opens the Kite login page automatically. Log in and approve.
3. Kite redirects to your app's redirect URL (a local address such as
   ``http://127.0.0.1:8765/``); a tiny local server running here catches the
   ``request_token`` automatically — nothing to copy or paste.
4. The script exchanges it (with ``KITE_API_SECRET``) for the day's access token
   and writes ``KITE_ACCESS_TOKEN`` into ``.env``.

This requires your Kite app's **redirect URL** to be a localhost address whose
host/port match ``--host``/``--port`` (defaults ``127.0.0.1:8765``). If it isn't,
use ``--manual`` (paste the redirect URL yourself) or ``--request-token``.

Then start the app: ``uv run python main.py`` (with ``EXECUTION_MODE=kite``).

``KITE_API_KEY`` and ``KITE_API_SECRET`` must already be set in ``.env``. The
request token is single-use and short-lived, so generate and exchange it in one go.
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

_ENV_VAR = "KITE_ACCESS_TOKEN"
_DEFAULT_ENV = Path(__file__).resolve().parent / ".env"
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
    b"margin-top:4rem'><h2>No request_token in this request.</h2></body></html>"
)


def _extract_request_token(pasted: str) -> str:
    """Return the request token from a raw token or a full redirect URL."""
    pasted = pasted.strip()
    if "request_token=" in pasted or pasted.lower().startswith("http"):
        query = parse_qs(urlparse(pasted).query)
        tokens = query.get("request_token")
        if not tokens or not tokens[0]:
            raise ValueError("No 'request_token' found in the pasted URL")
        return tokens[0]
    return pasted


def _mask(secret: str) -> str:
    """Show only the ends of a secret, e.g. ``abcd…wxyz``."""
    if len(secret) <= 8:
        return "…"
    return f"{secret[:4]}…{secret[-4:]}"


def _write_token(env_path: Path, token: str) -> None:
    """Set (or append) ``KITE_ACCESS_TOKEN`` in ``env_path`` without touching else."""
    line = f"{_ENV_VAR}={token}\n"
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
    else:
        lines = []

    for i, existing in enumerate(lines):
        # Match the assignment even if currently blank or commented-out indentation.
        if existing.lstrip().split("=", 1)[0].strip() == _ENV_VAR and not existing.lstrip().startswith("#"):
            lines[i] = line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(line)

    env_path.write_text("".join(lines))


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures ``request_token`` from the Kite login redirect, then stops."""

    request_token: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        token = parse_qs(urlparse(self.path).query).get("request_token", [None])[0]
        if token:
            _CallbackHandler.request_token = token
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
    """Open the login page and capture the redirected ``request_token`` locally."""
    _CallbackHandler.request_token = None
    server = HTTPServer((host, port), _CallbackHandler)
    server.timeout = 1  # so handle_request() returns and we can check the deadline
    try:
        print("\nOpening the Kite login page in your browser…")
        print(f"If it doesn't open, visit:\n   {login_url}\n")
        if open_browser:
            webbrowser.open(login_url)
        print(f"Waiting for the login redirect on http://{host}:{port}/ …")
        deadline = time.monotonic() + _CAPTURE_TIMEOUT_S
        while _CallbackHandler.request_token is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if _CallbackHandler.request_token is None:
        raise TimeoutError(
            f"No redirect received within {_CAPTURE_TIMEOUT_S}s. Check that your "
            f"Kite app's redirect URL is http://{host}:{port}/ (or use --manual)."
        )
    return _CallbackHandler.request_token


def _capture_via_paste(login_url: str) -> str:
    """Fallback: print the login URL and read the request token from stdin."""
    print("\n1) Open this URL, log in, and approve:\n")
    print(f"   {login_url}\n")
    print(
        "2) After login your browser is redirected to your app's redirect URL\n"
        "   containing 'request_token=...'.\n"
    )
    raw = input("3) Paste the request_token (or the whole redirect URL) here: ")
    return _extract_request_token(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-token",
        help="Skip login entirely and exchange this request token (or redirect URL).",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Paste the request token yourself instead of auto-capturing it.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser (still auto-captures the redirect).",
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

    # Read credentials directly so the helper works even if other config is absent.
    import os

    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    if not api_key or not api_secret:
        print("KITE_API_KEY and KITE_API_SECRET must be set in .env first.", file=sys.stderr)
        return 1

    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import KiteException

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    try:
        if args.request_token:
            request_token = _extract_request_token(args.request_token)
        elif args.manual:
            request_token = _capture_via_paste(login_url)
        else:
            try:
                request_token = _capture_via_server(
                    login_url, args.host, args.port, open_browser=not args.no_browser
                )
            except OSError as exc:
                # Port busy / cannot bind — fall back to manual paste.
                print(f"Could not start the local callback server: {exc}", file=sys.stderr)
                print("Falling back to manual paste.\n", file=sys.stderr)
                request_token = _capture_via_paste(login_url)
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return 1
    except (ValueError, TimeoutError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except KiteException as exc:
        print(f"Kite rejected the token exchange: {exc}", file=sys.stderr)
        print(
            "The request token is single-use and expires quickly — re-run and try "
            "again with a fresh login.",
            file=sys.stderr,
        )
        return 1

    access_token = session["access_token"]
    _write_token(args.env_file, access_token)

    print(f"\n✓ Wrote {_ENV_VAR}={_mask(access_token)} to {args.env_file}")
    print(f"  Logged in as: {session.get('user_id', '?')}")
    print("  Valid for today's session — start the app with EXECUTION_MODE=kite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
