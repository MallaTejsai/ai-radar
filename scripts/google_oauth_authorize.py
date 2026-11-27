"""One-time Google OAuth2 consent helper for the Drive backup.

The weekly backup runs unattended, so it needs a *refresh* token, not a short-lived
access token. This script performs the authorization-code flow on your own machine
and prints the refresh token once, for you to paste into GitHub Secrets.

Rules baked in:

* ``drive.file`` scope only - the app can see the files it created, nothing else.
* The client secret and refresh token are never written to disk or to a log file.
* The token is printed to stdout; redirect it only somewhere you trust, and never
  paste it into a chat, an issue or a commit.

Usage (PowerShell):

    $env:GOOGLE_OAUTH_CLIENT_ID = "....apps.googleusercontent.com"
    $env:GOOGLE_OAUTH_CLIENT_SECRET = "...."
    python scripts/google_oauth_authorize.py

If the browser cannot reach the local callback (remote/headless machine), add
``--manual`` and paste the ``code`` value from the redirected URL.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
REDIRECT_PATH = "/oauth2callback"


class _Callback(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if state != self.expected_state:
            self.__class__.error = "state mismatch - the response did not come from this request"
        elif params.get("error"):
            self.__class__.error = (params.get("error") or [""])[0]
        else:
            self.__class__.code = (params.get("code") or [None])[0]

        body = (
            b"<html><body style='font-family:sans-serif'>"
            b"<h2>AI Radar</h2><p>Authorization received. You can close this tab "
            b"and return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:  # keep the terminal clean
        return


def _build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",  # forces a new refresh token even on re-consent
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def _exchange(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed URL
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Obtain a Google Drive refresh token")
    parser.add_argument("--port", type=int, default=8765, help="local callback port")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="print the URL and read the code from stdin instead of running a local server",
    )
    args = parser.parse_args(argv)

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first "
            "(Google Cloud Console -> APIs & Services -> Credentials -> OAuth client, "
            "type 'Desktop app').",
            file=sys.stderr,
        )
        return 2

    redirect_uri = f"http://127.0.0.1:{args.port}{REDIRECT_PATH}"
    state = secrets.token_urlsafe(24)
    url = _build_auth_url(client_id, redirect_uri, state)

    print("1. Open this URL and approve access to the app's own Drive files only:\n")
    print(url + "\n")

    if args.manual:
        print("2. After approving, your browser lands on a 127.0.0.1 URL that may not load.")
        print("   Copy the 'code' query parameter and paste it here.\n")
        code = input("code: ").strip()
    else:
        _Callback.expected_state = state
        print(f"2. Waiting for the callback on {redirect_uri} ...")
        webbrowser.open(url)
        server = HTTPServer(("127.0.0.1", args.port), _Callback)
        try:
            server.handle_request()
        finally:
            server.server_close()
        if _Callback.error:
            print(f"Authorization failed: {_Callback.error}", file=sys.stderr)
            return 1
        code = _Callback.code or ""

    if not code:
        print("No authorization code was received.", file=sys.stderr)
        return 1

    try:
        tokens = _exchange(code, client_id, client_secret, redirect_uri)
    except Exception as exc:  # urllib raises HTTPError with a useful body
        body = ""
        if hasattr(exc, "read"):
            try:
                body = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                body = ""
        print(f"Token exchange failed: {exc} {body}", file=sys.stderr)
        return 1

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(
            "Google returned no refresh_token. Re-run with prompt=consent (this script "
            "sets it) or remove the app's previous grant at "
            "https://myaccount.google.com/permissions and try again.",
            file=sys.stderr,
        )
        return 1

    print("\n3. Add these as GitHub repository secrets (Settings -> Secrets and variables -> Actions):\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print("GOOGLE_OAUTH_CLIENT_SECRET=<the secret you already have>")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")
    print("\n4. Keep the OAuth app's publishing status on 'In production'.")
    print("   A testing-mode app issues refresh tokens that expire after 7 days,")
    print("   which would silently break the weekly backup.\n")
    print("The refresh token is printed once. It is not written to disk by this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
