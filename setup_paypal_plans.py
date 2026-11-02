"""Create Loop's PayPal subscription plans (deploy-time setup).

Run this ONCE per PayPal mode (sandbox, then live) after real
credentials exist. It refuses to run without credentials, and it never
prints or stores any credential — only the plan ids, which are not
secrets. Record the printed ids as environment variables on the host:

    PAYPAL_PRO_PLAN_ID=<printed Pro id>
    PAYPAL_ELITE_PLAN_ID=<printed Elite id>

Re-running creates duplicate plans in PayPal, so run it once per mode
and keep the first set of ids. Requires:

    PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_MODE

Usage:  python setup_paypal_plans.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paypal import (
    PayPalError,
    create_plan,
    create_product,
    get_access_token,
    paypal_configured,
    paypal_mode,
)


def main():
    if not paypal_configured():
        print(
            "Missing PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET. Set them from"
            " a PayPal Developer Dashboard app first (see README).",
            file=sys.stderr,
        )
        return 1
    try:
        token = get_access_token()
        product_id = create_product(token)
        pro_id = create_plan(token, product_id, "pro")
        elite_id = create_plan(token, product_id, "elite")
    except PayPalError as exc:
        print("PayPal setup failed: %s" % exc, file=sys.stderr)
        return 1
    print("PayPal mode: %s" % paypal_mode())
    print("Product id: %s" % product_id)
    print("Set these environment variables on the host:")
    print("PAYPAL_PRO_PLAN_ID=%s" % pro_id)
    print("PAYPAL_ELITE_PLAN_ID=%s" % elite_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
