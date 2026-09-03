"""One-time LinkedIn OAuth 2.0 authorization-code flow.

Run this script, approve access in the browser it opens, and it saves the
resulting access token to .linkedin_token.json (gitignored, project root).
Re-run this script to get a fresh token once the old one expires.

Reads LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET / LINKEDIN_REDIRECT_URI /
LINKEDIN_SCOPES from .env -- see .env.example. Never hard-code credentials
here; the client secret must never be committed or printed.
"""
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET")
REDIRECT_URI = os.environ.get("LINKEDIN_REDIRECT_URI", "http://localhost:3000/callback")
SCOPES = os.environ.get("LINKEDIN_SCOPES", "openid,profile,w_member_social,email")

AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".linkedin_token.json"
CALLBACK_TIMEOUT_SECONDS = 180


class _CallbackResult:
    code = None
    state = None
    error = None


def _make_handler(expected_state, result):
    callback_path = urllib.parse.urlparse(REDIRECT_URI).path

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            result.error = params.get("error_description", params.get("error", [None]))[0]
            result.state = params.get("state", [None])[0]
            result.code = params.get("code", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if result.error:
                body = "<h2>LinkedIn authorization failed. Check the terminal.</h2>"
            elif result.state != expected_state:
                body = "<h2>State mismatch (possible CSRF). Check the terminal.</h2>"
            else:
                body = "<h2>LinkedIn authorized. You can close this tab.</h2>"
            self.wfile.write(f"<html><body>{body}</body></html>".encode())

        def log_message(self, format, *args):
            pass  # silence default request logging to stderr

    return CallbackHandler


def _authorize(port, state):
    result = _CallbackResult()
    server = http.server.HTTPServer(("localhost", port), _make_handler(state, result))
    server.timeout = CALLBACK_TIMEOUT_SECONDS
    server.handle_request()
    server.server_close()
    return result


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Missing LINKEDIN_CLIENT_ID or LINKEDIN_CLIENT_SECRET in .env.", file=sys.stderr)
        print("Copy .env.example to .env and fill in your app's credentials first.", file=sys.stderr)
        sys.exit(1)

    parsed_redirect = urllib.parse.urlparse(REDIRECT_URI)
    port = parsed_redirect.port or 80

    state = secrets.token_urlsafe(24)
    scope_str = " ".join(s.strip() for s in SCOPES.split(",") if s.strip())

    auth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": scope_str,
    }
    auth_url = f"{AUTHORIZATION_URL}?{urllib.parse.urlencode(auth_params)}"

    print("Opening browser for LinkedIn authorization...")
    print(auth_url)
    webbrowser.open(auth_url)

    result = _authorize(port, state)

    if result.code is None and result.error is None:
        print(f"Timed out after {CALLBACK_TIMEOUT_SECONDS}s waiting for LinkedIn to redirect back. Try again.", file=sys.stderr)
        sys.exit(1)

    if result.error:
        print(f"LinkedIn returned an error: {result.error}", file=sys.stderr)
        sys.exit(1)

    if result.state != state:
        print("State mismatch on callback -- aborting (possible CSRF).", file=sys.stderr)
        sys.exit(1)

    if not result.code:
        print("No authorization code received.", file=sys.stderr)
        sys.exit(1)

    token_response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )

    if token_response.status_code != 200:
        # Never log CLIENT_SECRET -- it isn't in this response body, only the
        # request we sent, so token_response.text is safe to print.
        print(f"Token exchange failed ({token_response.status_code}): {token_response.text}", file=sys.stderr)
        sys.exit(1)

    token_data = token_response.json()
    expires_in = token_data.get("expires_in")
    token_data["obtained_at"] = datetime.now(timezone.utc).isoformat()
    if expires_in:
        token_data["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()

    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass  # best-effort; not all platforms support POSIX permission bits

    print(f"Success. Token saved to {TOKEN_FILE} (gitignored, never commit it).")
    if expires_in:
        print(f"Expires in {expires_in} seconds (~{expires_in // 86400} days).")


if __name__ == "__main__":
    main()
