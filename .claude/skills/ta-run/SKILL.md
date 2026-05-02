---
name: ta-run
description: Kick off a TradingAgents research run via the web API. Takes one or more tickers with per-ticker analyst/debate-round config, SigV4-signs the request using AWS_PROFILE=IGENV, and returns the executionArn so the user can poll progress.
---

# ta-run

Use this skill when the user says things like:

- "run research on NVDA"
- "kick off a deep run of AAPL and MSFT"
- "start a TradingAgents run for TSLA market+news rounds=1"

## What it does

POSTs a JSON body to `${WEB_API_URL}/runs` with SigV4 (AWS_PROFILE=IGENV), where the backend starts the `tradingagents-run` Step Functions execution. Returns the executionArn immediately — the run itself takes 20+ minutes.

## Request shape

```json
{
  "tickers": [
    {"symbol": "NVDA", "analysts": ["market","news","fundamentals"], "debate_rounds": 2},
    {"symbol": "AAPL", "analysts": ["market"], "debate_rounds": 1}
  ],
  "trade_date": "today"
}
```

- `symbol`: required, uppercase, `^[A-Z][A-Z0-9.-]{0,9}$`.
- `analysts`: optional subset of `market`, `social`, `news`, `fundamentals`. Defaults to all four if omitted.
- `debate_rounds`: optional integer 0–5. Defaults to 1.
- `trade_date`: optional, `YYYY-MM-DD` or `"today"`. Defaults to today.

## How Claude should run it

1. **Resolve the API URL**: read from `TA_API_URL` env var. If unset, run `aws cloudformation describe-stacks --stack-name TradingAgentsStack --query "Stacks[0].Outputs[?OutputKey=='WebApiUrl'].OutputValue" --output text --profile IGENV` once per session.
2. **Parse the user's request** into the JSON body. If they say "quick" or "default", assume all analysts + 1 round. If they give free-form per-ticker config, convert it literally.
3. **Confirm the body** with the user before sending (single-line summary: `"NVDA (market+news, 2 rounds), AAPL (all, 1 round) — OK to run?"`).
4. On confirmation, shell out:
   ```
   python .claude/skills/ta-run/trigger.py --body '<json>'
   ```
   The helper handles SigV4 signing with `AWS_PROFILE=IGENV`.
5. **Report back** the `executionArn`, `run_id`, and a ready-to-copy poll command:
   ```
   aws stepfunctions describe-execution --execution-arn <arn> --profile IGENV --query 'status'
   ```
6. If the user asks to poll, call:
   ```
   python .claude/skills/ta-run/trigger.py --status <executionArn>
   ```

## Prerequisites

- `AWS_PROFILE=IGENV` (or a profile that the API Gateway resource policy allows — `execute-api:Invoke` on `arn:aws:execute-api:us-east-1:<acct>:<api-id>/*`).
- The stack was deployed with `-c apiEnabled=true` (outputs `WebApiUrl`).
- Python with `boto3` + `botocore` + `requests` available.

## Gotchas

- The response from `POST /runs` is async — a 200 means SFN accepted the execution, not that the run succeeded.
- Execution ARNs contain `:` which must be URL-encoded when using the status endpoint directly over HTTP. The helper script encodes automatically.
- Step Functions rejects duplicate execution names; the backend makes names unique by prefixing a UUID.
