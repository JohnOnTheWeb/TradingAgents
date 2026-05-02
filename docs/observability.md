# Unified Observability

TradingAgents emits OpenTelemetry traces, logs, and metrics from both AgentCore
Runtime and the per-ticker Fargate task. Everything lands in Amazon OpenSearch
Service (traces + logs) and Amazon Managed Prometheus (metrics) via a single
Amazon OpenSearch Ingestion (OSIS) pipeline.

```
Fargate task ──┐
               ├──▶ OTLP/HTTP (SigV4) ──▶ OSIS pipeline ──▶ OpenSearch (traces+logs)
AgentCore     ─┘                                         └▶ AMP (metrics)
```

Dashboards live in OpenSearch Dashboards as three levels — **Fleet → Run →
Ticker** — with dashboard drilldowns between them and a URL drilldown out to
the md-store report.

## Rollout

Two-phase deploy mirrors the existing `agentCoreEnabled` pattern. Env-var
injection on the Runtime and Fargate containers is gated on *both*
`observabilityEnabled` and `agentCoreEnabled` being true, so the infra flag can
be flipped on in phase A without the app image needing to know about OTel.

### Phase A — infrastructure only

```bash
cdk deploy -c observabilityEnabled=true -c agentCoreEnabled=<current>
```

Creates:

- `ta-observability` OpenSearch domain (`t3.small.search`, 1 node, 20 GB gp3)
- `tradingagents/opensearch-master` Secrets Manager secret (master user creds)
- `tradingagents-metrics` Amazon Managed Prometheus workspace
- `ta-otel` OSIS pipeline with three sub-pipelines (traces / logs / metrics)
- `ta-osis-pipeline-role` and `ta-observability-admin` IAM roles
- `osis:Ingest` on the pipeline ARN added to the Runtime + Fargate task roles

No app behavior change yet — the container image has not been rebuilt, so
`OTEL_EXPORTER_OTLP_ENDPOINT` is injected only after the image knows how to
consume it.

### Phase B — rebuild + redeploy

1. Trigger CodeBuild so a new ECR image ships with the `[otel]` extra (OpenTelemetry
   SDK + the `opensearch-genai-observability-sdk` auto-instrumentation).
2. Redeploy with both flags on:
   ```bash
   cdk deploy -c observabilityEnabled=true -c agentCoreEnabled=true
   ```
3. The Runtime and Fargate containers now receive the OTLP env vars:
   - `OTEL_EXPORTER_OTLP_ENDPOINT` — OSIS public ingest URL
   - `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
   - `OTEL_SERVICE_NAME` — `tradingagents-runtime` or `tradingagents-task-runner`
   - `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod,service.namespace=tradingagents`
   - `TA_OTEL_SIGV4=1` — enables SigV4 signing on the OTLP exporter

### Import dashboards (one-time)

After Phase B:

1. Look up the Dashboards URL from the stack output:
   ```bash
   aws cloudformation describe-stacks --stack-name TradingAgentsStack \
     --query 'Stacks[0].Outputs[?OutputKey==`OpenSearchDashboardsUrl`].OutputValue' \
     --output text
   ```
2. Fetch the master user credentials:
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id tradingagents/opensearch-master \
     --query SecretString --output text | jq
   ```
3. In Dashboards: **Stack Management → Saved Objects → Import** — upload in this
   order (they share the `ta-traces-index-pattern` reference):
   - `repo/infra/dashboards/fleet.ndjson`
   - `repo/infra/dashboards/run.ndjson`
   - `repo/infra/dashboards/ticker.ndjson`
4. Dashboard drilldowns (Fleet row → Run, Run row → Ticker) and the Ticker
   URL drilldown to `https://<md-store>/files/TauricTraders/{ta.ticker}_{ta.trade_date}.md`
   are configured in the Dashboards UI after import — they can't round-trip
   cleanly through NDJSON. One-click setup is in Options → Drilldowns on each
   affected panel.

## Span shape

```
ta.fargate_invoke            (task_runner.py root)
 └─ ta.invocation            (app.invocations, remote parent via traceparent)
     ├─ ta.agent_node × ~11  (wrap_node around each LangGraph node)
     │   └─ gen_ai.bedrock.converse × N   (auto-instrumentation)
     └─ ta.md_store_write
```

Key attributes:

- **Roots inherit**: `ta.run_id`, `ta.ticker`, `ta.trade_date`
- **Per node**: `ta.agent_node`
- **On `ta.invocation`**: `ta.decision`, `ta.cost_usd`
- **On Bedrock spans (auto)**: `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
- **On `ta.md_store_write`**: `http.status_code`, `ta.bytes_written`

## Verification

After Phase B, smoke the full path:

```bash
aws stepfunctions start-execution \
  --state-machine-arn $(aws cloudformation describe-stacks \
    --stack-name TradingAgentsStack \
    --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArnOut`].OutputValue' \
    --output text) \
  --input '{"run_id":"smoke-1","tickers":["AMZN"],"trade_date":"2026-05-01"}'
```

Checks:

1. **OSIS logs** — `/aws/vendedlogs/OpenSearchService/pipelines/ta-otel` should show
   `received N spans` once the run starts emitting.
2. **OpenSearch Discover** — open `ss4o_traces-ta-*`, filter `ta.run_id: smoke-1`.
   Expect 1 × `ta.fargate_invoke`, 1 × `ta.invocation`, ~11 × `ta.agent_node`,
   multiple `gen_ai.bedrock.converse`, 1 × `ta.md_store_write`.
3. **AMP** — confirm metrics are flowing:
   ```bash
   awscurl --service aps \
     "https://aps-workspaces.$AWS_REGION.amazonaws.com/workspaces/$AMP_ID/api/v1/label/__name__/values"
   ```
   Expect `gen_ai_client_token_usage` or `process_cpu_seconds_total` in the output.
4. **Dashboards** — Fleet → drilldown by `ta.run_id=smoke-1` → Run → drilldown
   by ticker → Ticker → "Open report" URL drilldown should resolve to the
   md-store markdown.

## Rollback

```bash
cdk deploy -c observabilityEnabled=false
```

The env-var injection disappears on the next Runtime/Fargate deploy; the Python
tracer becomes a no-op because `OTEL_EXPORTER_OTLP_ENDPOINT` is unset. The
OpenSearch domain, AMP workspace, and OSIS pipeline stay in place (all
`RemovalPolicy.RETAIN`) so historical traces aren't lost.

To fully tear down observability (e.g., in a dev account):

```bash
aws cloudformation delete-stack --stack-name TradingAgentsStack  # retains data stores
# then manually: aws es delete-domain --domain-name ta-observability
#                aws osis delete-pipeline --pipeline-name ta-otel
#                aws amp delete-workspace --workspace-id <id>
```
