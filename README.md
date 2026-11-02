# Loop — public-app codebase, first working version

Loop is a social app: a home feed with groups-style posting, Taps (short
videos), stories, Snips (disappearing messages) with streaks, a Discover
page with Local Artists, artist sub-profiles, linked accounts, Lives with
tier replay rules, and Pro / Elite tiers. This is a real codebase with
real saved state in SQLite — unlike the earlier prototype, nothing here
resets when you reload.

## Install and run

```bash
cd ~/workspace/loop-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open <http://127.0.0.1:5000> in a browser.

The first run creates `loop.db` (SQLite) and an `uploads/` folder next to
`app.py`, and seeds clearly labeled demo data.

## Demo logins

Password for all three demo accounts is `demo1234`:

- `demo_amy` — Free plan
- `demo_bo` — Pro plan
- `demo_june` — Elite plan

## What works in this version

- Signup, login, logout, sessions; passwords stored as PBKDF2 hashes.
- Profiles with a bio, friends-only personal posts, and public artist pages.
- Feed posts (public or friends-only) that persist in SQLite.
- Taps: video upload to local `uploads/` storage, with the video length
  recorded and tier limits enforced on upload — Free 60 seconds, Pro 180
  seconds, Elite 600 seconds. Over-limit uploads are refused with the
  limit named and the upgrade path shown.
- Stories that disappear after 24 hours; Snips (messages that disappear
  after 24 hours); streaks counting consecutive days of messages.
- Discover Local Artists with craft filters (Music, Art, Dance, Comedy,
  Photography) and an Everyone / Near me / Worldwide toggle.
- Artist pages: public sub-profiles linked to a main user, with a pinned
  showcase. Personal posts and Snips stay friends-only.
- Linked accounts with tier caps enforced: Free 0, Pro up to 2, Elite
  unlimited, with a limit prompt and upgrade path when capped.
- Lives: replay rules recorded per Live — Free not saved, Pro saved 24
  hours, Elite user choice per Live (don't save / 24 hours / 7 days /
  30 days / keep until deleted). Actual live broadcasting is out of scope
  for this phase.
- Premium page with Pro / Elite cards and a comparison table. When
  PayPal is configured (see "Payments (PayPal)" below), subscribing
  goes through PayPal approval and cancelling cancels the PayPal
  subscription. When PayPal is not configured, the page says payments
  are not connected yet and tier switching stays a clearly labeled demo
  switch that charges nothing.

## Payments (PayPal)

PayPal Subscriptions is Loop's payments provider. All PayPal code lives
in `paypal.py` and uses only the standard library. Configuration comes
only from environment variables — no credential is stored in code, and
no credential or access token is ever logged.

- `PAYPAL_CLIENT_ID` — from a PayPal Developer Dashboard app.
- `PAYPAL_CLIENT_SECRET` — from the same app. Never commit it anywhere.
- `PAYPAL_MODE` — `sandbox` (default) or `live`.
- `PAYPAL_PRO_PLAN_ID` — plan id for Loop Pro ($4.99/mo).
- `PAYPAL_ELITE_PLAN_ID` — plan id for Loop Elite ($19.99/mo).
- `PAYPAL_WEBHOOK_ID` — webhook id. Required before webhook events are
  trusted: without it, the webhook endpoint acknowledges events and
  applies nothing. See "Webhooks" below.

### Creating the plans (deploy time, once per mode)

After real credentials exist, run:

```bash
PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... PAYPAL_MODE=sandbox \
  .venv/bin/python setup_paypal_plans.py
```

The script refuses to run without credentials. It creates the Loop
product and the Pro and Elite monthly plans, then prints the two plan
ids. Set them as `PAYPAL_PRO_PLAN_ID` and `PAYPAL_ELITE_PLAN_ID` on the
host. Re-running creates duplicate plans in PayPal, so run it once per
mode and keep the first set of ids.

### Subscribe and cancel flow

1. On the Premium page, a signed-in user picks Pro or Elite. The app
   creates a PayPal subscription for that plan's id and redirects the
   user to PayPal's approval URL. Nothing is charged on Loop itself.
2. PayPal returns the user to `/premium/paypal/return`. The app asks
   PayPal for the subscription's status and only sets the user's tier
   once PayPal reports it ACTIVE. If approval is still pending, the
   plan updates later via the webhook.
3. Cancelling on the Premium page calls PayPal's cancel endpoint for
   the active subscription and drops the user back to Free.
4. If the user cancels approval on PayPal's side, they land on
   `/premium/paypal/cancel` and nothing is taken.

### Webhooks

PayPal subscription events arrive at `POST /webhooks/paypal`. Signature
verification is REQUIRED before trusting any event in production: the
endpoint verifies each event against PayPal's verify endpoint using
`PAYPAL_WEBHOOK_ID`, and only then applies it — ACTIVATED sets the
user's tier, CANCELLED or EXPIRED drops them to Free. Without a webhook
id, or when verification fails, the event is acknowledged and ignored
and no tier ever changes. Failed-payment dunning is not handled yet;
that is a later step.

## Configuration

- `LOOP_DB` — path to the SQLite file (default: `loop.db` next to `app.py`).
- `LOOP_SECRET_KEY` — session secret. The default is a dev-only value;
  set a real secret before any shared use.

## Notes and limits

- Video length is read by the browser before upload and, for MP4 files,
  re-checked server-side with a dependency-free box parser in
  `media.py`. Production should verify lengths with a real media probe.
- "Near me" currently means artists marked local; real geolocation is a
  later step (see NEXT_STEPS.md).
- This build is for local use. It is not public, not on any app store,
  and takes no payments until PayPal is linked as described above. See
  NEXT_STEPS.md for what a public launch needs.
