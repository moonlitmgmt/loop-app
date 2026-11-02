# Loop — what still needs real accounts or decisions

This codebase is a working local foundation. It is not public, not on any
app store, and takes no payments. Before a public launch, these items
need real accounts, decisions, or vendors:

## Required for launch

1. **Public hosting and domain** — pick a host and a domain name, deploy
   this app behind HTTPS, and move uploads and the database off a single
   local file (managed database plus object storage for media).
2. **PayPal for real Pro / Elite payments (chosen provider, superseding
   Stripe)** — PayPal Subscriptions is Loop's payments provider, and the
   code support is already in the app (see README, "Payments (PayPal)").
   What Cody must do to link his own PayPal:
   1. Create a PayPal Business account. A Business account is required
      for subscriptions — a personal account cannot sell them.
   2. In the PayPal Developer Dashboard, create an app to get a Client
      ID and Secret. Start in sandbox.
   3. Set `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and
      `PAYPAL_MODE=sandbox` as environment variables on the host.
   4. Run `python setup_paypal_plans.py` once to create the Loop Pro
      ($4.99/mo) and Loop Elite ($19.99/mo) plans, then set the printed
      `PAYPAL_PRO_PLAN_ID` and `PAYPAL_ELITE_PLAN_ID` on the host.
   5. Test the full subscribe and cancel flow in sandbox, including
      the webhook endpoint with a `PAYPAL_WEBHOOK_ID` set.
   6. For live payments, create a live app in the Developer Dashboard,
      set `PAYPAL_MODE=live` with the live credentials, re-run plan
      setup, and set the live plan ids.
   Until those steps are done, the Premium page shows "payments are
   not connected yet" and no payment is taken. Failed-payment dunning
   and refunds still need to be built out after launch.
3. **Live-streaming infrastructure** — pick a streaming provider and wire
   real broadcasting, viewers, and replay storage to the Lives retention
   rules already recorded per Live.
4. **App-store accounts** — Apple Developer and Google Play accounts,
   plus native or wrapped mobile builds, store listings, screenshots,
   and review submissions.
5. **Moderation tooling** — reporting, blocking, review queues, and
   automated checks for uploads, posts, messages, and artist pages.
6. **Terms and privacy policy** — written terms of service and privacy
   policy covering accounts, uploads, payments, data retention
   (including Snips expiry and Live replay rules), and age requirements.

## Also needed before real users

- A real session secret (`LOOP_SECRET_KEY`) and HTTPS everywhere.
- Server-side video length verification with a real media probe, plus
  transcoding and size limits for uploads.
- Email verification and password reset flows.
- Real geolocation for the "Near me" artist filter.
- Backups for the database and uploaded media.
- Rate limiting and abuse controls on signup, posting, and messaging.
