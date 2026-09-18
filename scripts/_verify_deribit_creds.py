"""One-shot Deribit credential verification (read-only, no orders).

Calls the /public/auth endpoint to confirm the client_id+client_secret
work, then calls /private/get_account_summary to confirm we can actually
read account state. Does NOT place orders, withdraw, or modify anything.

Usage:
    python scripts/_verify_deribit_creds.py <client_id> <client_secret>
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


PROD_BASE = "https://www.deribit.com"
TESTNET_BASE = "https://test.deribit.com"


def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return {"_http_error": e.code, "_body": raw}
    except urllib.error.URLError as e:
        return {"_url_error": str(e.reason)}


def main(client_id: str, client_secret: str, env: str = "prod") -> int:
    base = PROD_BASE if env == "prod" else TESTNET_BASE

    print(f"=== Deribit credential verification ({env}) ===")
    print(f"Endpoint: {base}/api/v2/public/auth")
    print(f"client_id: {client_id[:4]}***  (len={len(client_id)})")
    print(f"client_secret: {client_secret[:4]}***  (len={len(client_secret)})")
    print()

    # Step 1: authenticate
    auth_resp = _post_json(
        f"{base}/api/v2/public/auth",
        {
            "jsonrpc": "2.0",
            "method": "public/auth",
            "params": {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            "id": int(time.time() * 1000),
        },
    )

    if "_http_error" in auth_resp:
        print(f"[FAIL] HTTP {auth_resp['_http_error']}")
        print(f"  body: {auth_resp['_body']}")
        return 1
    if "error" in auth_resp:
        err = auth_resp["error"]
        print(f"[FAIL] Deribit auth error:")
        print(f"  code: {err.get('code')}")
        print(f"  message: {err.get('message')}")
        return 1
    if "result" not in auth_resp:
        print(f"[FAIL] Unexpected response shape: {auth_resp}")
        return 1

    result = auth_resp["result"]
    access_token = result.get("access_token", "")
    expires_in = result.get("expires_in", 0)
    scope = result.get("scope", "")
    print(f"[OK] Authentication succeeded")
    print(f"  access_token: {access_token[:20]}*** (len={len(access_token)})")
    print(f"  expires_in: {expires_in}s")
    print(f"  scope: {scope}")
    print()

    # Step 2: read account summary (read-only)
    summary_resp = _post_json(
        f"{base}/api/v2/private/get_account_summary",
        {
            "jsonrpc": "2.0",
            "method": "private/get_account_summary",
            "params": {
                "currency": "BTC",
                "extended": True,
            },
            "id": int(time.time() * 1000),
        },
        # NOTE: this won't actually auth the private call without the bearer
        # token. But Deribit's public/auth returns the token in `result`;
        # we need to send it as `Authorization: Bearer <token>` for the
        # private endpoint. Re-do:
    )

    # proper private call with bearer auth
    req = urllib.request.Request(
        f"{base}/api/v2/private/get_account_summary",
        data=json.dumps({
            "jsonrpc": "2.0",
            "method": "private/get_account_summary",
            "params": {"currency": "BTC", "extended": True},
            "id": int(time.time() * 1000),
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            summary = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        summary = {"_http_error": e.code, "_body": e.read().decode("utf-8", errors="replace")}

    if "_http_error" in summary:
        print(f"[FAIL] private/get_account_summary HTTP {summary['_http_error']}")
        print(f"  body: {summary['_body']}")
        return 1
    if "error" in summary:
        print(f"[FAIL] private call error:")
        print(f"  {summary['error']}")
        return 1

    s = summary.get("result", {})
    print(f"[OK] Account summary (BTC):")
    print(f"  equity:    {s.get('equity', 0)}")
    print(f"  balance:   {s.get('balance', 0)}")
    print(f"  available: {s.get('available_funds', 0)}")
    print(f"  margin:    {s.get('initial_margin', 0)}")
    print()
    print("=== Credentials VALIDATED. Safe to wire into the bot. ===")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python scripts/_verify_deribit_creds.py <client_id> <client_secret> [prod|testnet]")
        sys.exit(2)
    cid = sys.argv[1]
    secret = sys.argv[2]
    env = sys.argv[3] if len(sys.argv) > 3 else "prod"
    sys.exit(main(cid, secret, env))