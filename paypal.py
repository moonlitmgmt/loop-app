"""PayPal Subscriptions support for Loop.

Configuration comes only from environment variables; nothing here stores,
invents, or logs any credential, and nothing here ever logs a PayPal
access token. Without credentials the app runs exactly as before and the
Premium page says payments are not connected yet.

Environment variables:
  PAYPAL_CLIENT_ID     - from a PayPal Developer Dashboard app.
  PAYPAL_CLIENT_SECRET - from the same app. Never commit it anywhere.
  PAYPAL_MODE          - "sandbox" (default) or "live".
  PAYPAL_PRO_PLAN_ID   - plan id from setup_paypal_plans.py (Pro $4.99/mo).
  PAYPAL_ELITE_PLAN_ID - plan id from setup_paypal_plans.py (Elite $19.99/mo).
  PAYPAL_WEBHOOK_ID    - webhook id, required before webhook events are
                         trusted in production (see app.py).

Uses only the Python standard library, matching media.py's approach, so
no new dependency is added.
"""

import base64
import json
import os
import urllib.error
import urllib.request

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

PLAN_AMOUNTS = {"pro": "4.99", "elite": "19.99"}
PLAN_NAMES = {"pro": "Loop Pro", "elite": "Loop Elite"}


class PayPalError(Exception):
    """Any failure talking to PayPal. Messages never contain credentials
    or access tokens, so they are safe to show generically to users."""


def _env(name):
    value = os.environ.get(name, "").strip()
    return value or None


def paypal_mode():
    mode = (os.environ.get("PAYPAL_MODE", "sandbox") or "sandbox").strip().lower()
    return mode if mode in BASE_URLS else "sandbox"


def paypal_configured():
    """True when a PayPal app's credentials are present (any mode)."""
    return bool(_env("PAYPAL_CLIENT_ID") and _env("PAYPAL_CLIENT_SECRET"))


def paypal_plan_id(tier):
    if tier == "pro":
        return _env("PAYPAL_PRO_PLAN_ID")
    if tier == "elite":
        return _env("PAYPAL_ELITE_PLAN_ID")
    return None


def paypal_ready():
    """True when a real subscribe flow can run: credentials plus both
    plan ids. The Premium page shows the real flow only when ready."""
    return paypal_configured() and bool(paypal_plan_id("pro")) \
        and bool(paypal_plan_id("elite"))


def _request(method, path, token=None, payload=None):
    """One PayPal REST call. Raises PayPalError on any transport or API
    failure; the raised message never includes tokens or credentials."""
    url = BASE_URLS[paypal_mode()] + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise PayPalError(
            "PayPal API %s %s failed (HTTP %s): %s"
            % (method, path, exc.code, detail)
        ) from exc
    except OSError as exc:
        # HTTPError is handled above; this covers URLError, timeouts,
        # DNS failures, and dropped connections — all must fail
        # gracefully, never as an unhandled error page.
        raise PayPalError(
            "Could not reach PayPal (%s)." % exc
        ) from exc


def get_access_token():
    """Client-credentials grant. Raises PayPalError without credentials
    or when PayPal refuses them."""
    client_id = _env("PAYPAL_CLIENT_ID")
    secret = _env("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        raise PayPalError("PayPal credentials are not configured.")
    url = BASE_URLS[paypal_mode()] + "/v1/oauth2/token"
    auth = base64.b64encode(
        ("%s:%s" % (client_id, secret)).encode("ascii")
    ).decode("ascii")
    req = urllib.request.Request(
        url, data=b"grant_type=client_credentials", method="POST"
    )
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PayPalError(
            "PayPal token request failed (HTTP %s)." % exc.code
        ) from exc
    except OSError as exc:
        raise PayPalError(
            "Could not reach PayPal (%s)." % exc
        ) from exc
    token = body.get("access_token")
    if not token:
        raise PayPalError("PayPal did not return an access token.")
    return token


# ---------------------------------------------------------------- catalog
def create_product(token):
    """Create the Loop product in PayPal's catalog. Returns its id."""
    data = _request("POST", "/v1/catalogs/products", token, {
        "name": "Loop",
        "description": "Loop social app subscriptions",
        "type": "SERVICE",
        "category": "SOFTWARE",
    })
    return data["id"]


def create_plan(token, product_id, tier):
    """Create one monthly billing plan. Returns its id."""
    payload = {
        "product_id": product_id,
        "name": PLAN_NAMES[tier],
        "description": "%s monthly subscription" % PLAN_NAMES[tier],
        "billing_cycles": [{
            "frequency": {"interval_unit": "MONTH", "interval_count": 1},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,  # 0 = renews until cancelled
            "pricing_scheme": {
                "fixed_price": {
                    "value": PLAN_AMOUNTS[tier],
                    "currency_code": "USD",
                },
            },
        }],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "setup_fee_failure_action": "CONTINUE",
            "payment_failure_threshold": 3,
        },
    }
    data = _request("POST", "/v1/billing/plans", token, payload)
    return data["id"]


# ---------------------------------------------------------------- subscriptions
def create_subscription(token, plan_id, return_url, cancel_url):
    """Create a subscription and return (subscription_id, approval_url).
    The user must approve it on PayPal before it becomes ACTIVE."""
    payload = {
        "plan_id": plan_id,
        "application_context": {
            "brand_name": "Loop",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }
    data = _request("POST", "/v1/billing/subscriptions", token, payload)
    approve = None
    for link in data.get("links", []):
        if link.get("rel") == "approve":
            approve = link.get("href")
    if not approve:
        raise PayPalError("PayPal did not return an approval URL.")
    return data["id"], approve


def get_subscription(token, subscription_id):
    return _request(
        "GET", "/v1/billing/subscriptions/%s" % subscription_id, token
    )


def cancel_subscription(token, subscription_id, reason):
    _request(
        "POST",
        "/v1/billing/subscriptions/%s/cancel" % subscription_id,
        token,
        {"reason": reason},
    )


# ---------------------------------------------------------------- webhooks
def verify_webhook_signature(token, headers, raw_body, webhook_id):
    """Verify a PayPal webhook event against PayPal's verify endpoint.

    Returns True only when PayPal answers verification_status SUCCESS.
    Callers must treat anything else as untrusted and change no state.
    headers is a mapping (Flask's request.headers works); raw_body is
    the exact request bytes PayPal sent.
    """
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    payload = {
        "auth_algo": headers.get("PAYPAL-AUTH-ALGO", ""),
        "cert_url": headers.get("PAYPAL-CERT-URL", ""),
        "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID", ""),
        "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG", ""),
        "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME", ""),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    data = _request(
        "POST", "/v1/notifications/verify-webhook-signature", token, payload
    )
    return data.get("verification_status") == "SUCCESS"
