---
name: brokerage-refresh
description: Refresh Schwab and Tastytrade OAuth refresh tokens. Schwab requires a browser-redirect handshake every ~7 days; Tastytrade tokens rotate less often but can also be re-minted here. Updates both Secrets Manager and the local .brokerage-tokens.json so the brokerage-mcp server can authenticate.
---

# brokerage-refresh

Run this skill when:

- The Schwab refresh token has expired (after ~7 days) — the brokerage-mcp logs will show `401 Unauthorized` from Schwab's /oauth/token endpoint.
- A fresh Tastytrade refresh token is desired.
- Setting up `brokerage_mcp` for the first time on a new machine.

## What it does

1. Prompts for which broker(s) to refresh.
2. **Schwab path**: opens the Schwab `authorize` URL in the default browser, waits for you to paste the `code` query parameter from the callback URL, then exchanges the code + (client_id, client_secret) for fresh `access_token` + `refresh_token`.
3. **Tastytrade path**: uses the existing refresh token + (client_id, client_secret) to mint a new pair (does not require interactive approval unless the refresh token is itself expired).
4. Writes both refresh tokens to:
   - `aws secretsmanager put-secret-value --secret-id brokerage/schwab-oauth` (and the Tastytrade equivalent).
   - `repo/.brokerage-tokens.json` (for local dev).
5. Tells the user the new refresh token's expiry so they can calendar the next rotation.

## How Claude should run it

1. Confirm with the user which brokers to refresh ("both" is the default).
2. Shell out to `python repo/.claude/skills/brokerage-refresh/refresh.py <schwab|tastytrade|both>`.
3. For Schwab: the script will print the authorize URL; ask the user to visit it and paste the `code` parameter from the callback URL. Accept that code and forward it into the script's stdin.
4. Report where tokens were written and when they'll expire.

## Prerequisites

- `AWS_PROFILE=IGENV` set (or another profile with `secretsmanager:GetSecretValue` + `PutSecretValue` on `brokerage/*`).
- `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_REDIRECT_URI` env vars OR an existing `brokerage/schwab-oauth` secret with those fields.
- Same for Tastytrade: `TASTYTRADE_CLIENT_ID`, `TASTYTRADE_CLIENT_SECRET` (Tastytrade does not use a redirect URI for the refresh-token flow).

## Gotchas

- Schwab's redirect URI must match the one registered on the Schwab developer app **to the trailing slash**.
- The `code` in the callback URL is URL-encoded — the skill script auto-decodes it, so paste the raw value from the address bar.
- Do NOT commit `.brokerage-tokens.json` — it's gitignored by convention, but double-check before `git add`.
- If the Tastytrade refresh token has itself expired, you'll need to go through Tastytrade's web OAuth flow manually; this skill only handles the refresh-token grant type.
