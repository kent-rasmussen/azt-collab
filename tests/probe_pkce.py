#!/usr/bin/env python3
"""Manual probe: validate the research finding that a GitHub App's
``POST /login/oauth/access_token`` requires ``client_secret`` even
when the request includes a valid ``code_verifier``.

Not a pytest test (filename intentionally lacks the ``test_`` prefix
so it isn't auto-collected). Run it by hand from a desktop venv —
needs a real browser, a real GitHub login, and the App's
``client_id`` (and, for cases 3 and 4, the ``client_secret``). See
``docs/web_flow_migration_plan.md`` Phase 1 for the four test cases.

Usage:

    export AZT_GITHUB_APP_CLIENT_ID='Iv23li66Fo9MBReatv6i'
    # Optional, only needed for cases 3 & 4 (the success cases):
    export AZT_GITHUB_APP_CLIENT_SECRET='...'
    python tests/probe_pkce.py

Pre-req: register ``http://127.0.0.1:9876/cb`` as a callback URL on
the App at ``github.com/settings/apps/<your-app>``.

The script walks the user through up to three browser authorizations
(GitHub's ``code`` is single-use, so each test case needs a fresh
authorize). For each, the script:

  1. Builds a one-shot ``http.server`` on 127.0.0.1:9876 to capture
     the redirect.
  2. Prints the authorize URL and opens the browser.
  3. Waits for the redirect, parses ``code`` + ``state``.
  4. Exchanges ``code`` against ``/login/oauth/access_token`` with
     the case's parameter combination and prints the response.

Pass = observed behaviour matches the prediction in the plan. Any
deviation (e.g. case 2 returning a token without a secret) means
the research is wrong and the plan needs to be revised. The script
exits non-zero on a deviation.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser


REDIRECT_URI = 'http://127.0.0.1:9876/cb'
AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
TOKEN_URL = 'https://github.com/login/oauth/access_token'


# ── PKCE helpers ────────────────────────────────────────────────────────────

def _pkce_pair():
    """Return ``(code_verifier, code_challenge)`` per RFC 7636 §4.

    Verifier is 64 URL-safe random bytes (base64url-encoded ⇒ ~86
    chars, well within RFC 7636's 43-128 range). Challenge is
    ``BASE64URL(SHA256(verifier))`` with no padding.
    """
    verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(64)).rstrip(b'=').decode('ascii')
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode('ascii')).digest()
    ).rstrip(b'=').decode('ascii')
    return verifier, challenge


# ── Loopback server for the redirect ────────────────────────────────────────

class _CallbackResult:
    """Mutable holder so the request handler can stash the parsed
    redirect into the parent thread's scope."""
    __slots__ = ('code', 'state', 'error', 'raw')

    def __init__(self):
        self.code = None
        self.state = None
        self.error = None
        self.raw = None


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    # Set by the caller before serve_forever:
    result = None

    def log_message(self, fmt, *args):
        # Quiet — the script's own prints are the diagnostic surface.
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        self.result.raw = self.path
        self.result.code = (qs.get('code') or [None])[0]
        self.result.state = (qs.get('state') or [None])[0]
        self.result.error = (qs.get('error') or [None])[0]
        body = (
            b'<!doctype html><html><body>'
            b'<h2>You can close this tab.</h2>'
            b'<p>The probe captured the redirect. Switch back to the '
            b'terminal.</p></body></html>'
        )
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _capture_redirect(timeout_s=300):
    """Spin up a one-shot loopback server, return the parsed
    ``_CallbackResult`` once the browser hits ``/cb``. Times out
    after ``timeout_s`` (default 5 min) to keep the script unstuck if
    the user bails on the GitHub page."""
    result = _CallbackResult()
    handler_cls = type('_Handler', (_RedirectHandler,), {'result': result})
    httpd = socketserver.TCPServer(('127.0.0.1', 9876), handler_cls)
    httpd.timeout = timeout_s
    done = threading.Event()

    def _serve():
        # Single request, then close.
        httpd.handle_request()
        done.set()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    if not done.wait(timeout_s + 5):
        httpd.server_close()
        raise TimeoutError(
            f'No redirect within {timeout_s}s — aborting probe.')
    httpd.server_close()
    return result


# ── Authorize / exchange round-trip ─────────────────────────────────────────

def _authorize(client_id, *, send_pkce):
    """Run one round-trip: open the authorize page, wait for the
    redirect, return ``(code, code_verifier or None, state)``.

    ``send_pkce=False`` lets us test the no-PKCE control case
    (#4 in the plan)."""
    state = secrets.token_urlsafe(32)
    if send_pkce:
        verifier, challenge = _pkce_pair()
    else:
        verifier, challenge = None, None

    params = {
        'client_id': client_id,
        'redirect_uri': REDIRECT_URI,
        'state': state,
    }
    if challenge is not None:
        params['code_challenge'] = challenge
        params['code_challenge_method'] = 'S256'
    url = AUTHORIZE_URL + '?' + urllib.parse.urlencode(params)

    print()
    print(f'  Opening: {url}')
    print('  Waiting for redirect to 127.0.0.1:9876/cb …')
    webbrowser.open(url)
    cb = _capture_redirect()
    if cb.error:
        raise RuntimeError(f'Authorize step returned error: {cb.error}')
    if cb.state != state:
        raise RuntimeError(
            f'state mismatch: sent {state!r}, got {cb.state!r}')
    if not cb.code:
        raise RuntimeError(f'no code in redirect: {cb.raw!r}')
    print(f'  Got code (len={len(cb.code)})')
    return cb.code, verifier, state


def _exchange(client_id, code, *, code_verifier=None, client_secret=None):
    """POST ``/login/oauth/access_token`` and return ``(http_status,
    body_dict)``. Sends ``Accept: application/json`` so GitHub
    returns JSON (default is form-encoded)."""
    body = {
        'client_id': client_id,
        'code': code,
        'redirect_uri': REDIRECT_URI,
    }
    if code_verifier is not None:
        body['code_verifier'] = code_verifier
    if client_secret is not None:
        body['client_secret'] = client_secret
    data = urllib.parse.urlencode(body).encode('ascii')
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            raw = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as ex:
        status = ex.code
        raw = ex.read().decode('utf-8', errors='replace')
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {'_raw': raw}
    return status, parsed


# ── Cases ───────────────────────────────────────────────────────────────────

def case_2_pkce_no_secret(client_id):
    """The critical case: PKCE in hand, no ``client_secret``.
    Research predicts an error response."""
    print()
    print('=== Case 2: PKCE + verifier, NO client_secret ===')
    print('Expected: error response (validates research finding).')
    code, verifier, _ = _authorize(client_id, send_pkce=True)
    status, body = _exchange(
        client_id, code, code_verifier=verifier, client_secret=None)
    print(f'  HTTP {status}: {body!r}')
    if 'access_token' in body:
        print('  ✗ UNEXPECTED: got a token without a secret. Research '
              'is wrong; pure-PKCE web flow IS legal.')
        return False
    if body.get('error'):
        print(f"  ✓ Got error '{body['error']}' — research validated.")
        return True
    print(f'  ✗ UNEXPECTED: no token and no error. body={body!r}')
    return False


def case_3_pkce_with_secret(client_id, client_secret):
    """Control case: PKCE + secret. Expected: success."""
    print()
    print('=== Case 3: PKCE + verifier + client_secret ===')
    print('Expected: access_token returned.')
    code, verifier, _ = _authorize(client_id, send_pkce=True)
    status, body = _exchange(
        client_id, code,
        code_verifier=verifier, client_secret=client_secret)
    print(f'  HTTP {status}: {_redact(body)!r}')
    if 'access_token' in body:
        print('  ✓ Got access_token with PKCE + secret.')
        return True
    print(f'  ✗ UNEXPECTED: PKCE+secret rejected. body={body!r}')
    return False


def case_4_secret_no_pkce(client_id, client_secret):
    """Control case: no PKCE, just secret (today's device-flow-equivalent
    web shape). Expected: success — confirms PKCE is optional."""
    print()
    print('=== Case 4: client_secret only (no PKCE) ===')
    print('Expected: access_token returned (PKCE is optional).')
    code, _, _ = _authorize(client_id, send_pkce=False)
    status, body = _exchange(
        client_id, code, code_verifier=None, client_secret=client_secret)
    print(f'  HTTP {status}: {_redact(body)!r}')
    if 'access_token' in body:
        print('  ✓ Got access_token with secret only.')
        return True
    print(f'  ✗ UNEXPECTED: secret-only rejected. body={body!r}')
    return False


def _redact(body):
    """Replace token-shaped fields so we don't log secrets to a
    terminal that may be screen-shared."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    for k in ('access_token', 'refresh_token'):
        if k in out and out[k]:
            v = out[k]
            out[k] = f'{v[:6]}…[redacted, len={len(v)}]'
    return out


# ── Driver ──────────────────────────────────────────────────────────────────

def main():
    client_id = os.environ.get('AZT_GITHUB_APP_CLIENT_ID', '').strip()
    if not client_id:
        sys.exit('Set AZT_GITHUB_APP_CLIENT_ID before running.')
    client_secret = os.environ.get('AZT_GITHUB_APP_CLIENT_SECRET', '').strip()

    print('PKCE probe — see docs/web_flow_migration_plan.md Phase 1.')
    print()
    print(f'  client_id    = {client_id}')
    print(f'  redirect_uri = {REDIRECT_URI}')
    print(f'  client_secret= {"set" if client_secret else "NOT set"}')
    print()
    print('Pre-req: confirm the redirect URI above is registered on '
          'the App at github.com/settings/apps/<your-app>.')
    print()

    results = []

    # Case 2 is the headline: validates whether mobile-safe pure-PKCE
    # is legal. Always run it.
    results.append(('case_2_pkce_no_secret',
                    case_2_pkce_no_secret(client_id)))

    if client_secret:
        results.append(('case_3_pkce_with_secret',
                        case_3_pkce_with_secret(
                            client_id, client_secret)))
        results.append(('case_4_secret_no_pkce',
                        case_4_secret_no_pkce(
                            client_id, client_secret)))
    else:
        print()
        print('AZT_GITHUB_APP_CLIENT_SECRET not set; skipping cases 3 '
              'and 4 (control success cases). Set it to fully validate.')

    print()
    print('=== Summary ===')
    for name, passed in results:
        print(f'  [{"PASS" if passed else "FAIL"}] {name}')
    if not all(p for _, p in results):
        sys.exit(2)
    sys.exit(0)


if __name__ == '__main__':
    main()
