#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { AwsSolutionsChecks, NagSuppressions } from "cdk-nag";
import { TradingAgentsStack } from "../lib/tradingagents-stack";

const app = new cdk.App();

// Default to the IGENV account; CDK_DEFAULT_ACCOUNT overrides it when you
// run `cdk deploy --profile <other>`.
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT ?? "590183796434",
  region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
};

const stack = new TradingAgentsStack(app, "TradingAgentsStack", {
  env,
  description:
    "TradingAgents on Amazon Bedrock AgentCore — ECR + CodeBuild + Runtime + Gateway + Step Functions + EventBridge",
});

// Apply the required UsedBy tag to every resource in the app.
cdk.Tags.of(app).add("UsedBy", "TauricTrading");

// cdk-nag to surface obvious security posture issues at synth.
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

// Stack-wide suppressions for findings that are either inherent to the
// construct shapes we're using or intentionally accepted for this workload.
NagSuppressions.addStackSuppressions(stack, [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "CDK L2 Lambda constructs attach AWSLambdaBasicExecutionRole automatically. Replacing it with a handcrafted policy per function adds drift risk for no meaningful blast-radius reduction — the managed policy only grants CloudWatch Logs on each function's own log group.",
  },
  {
    id: "AwsSolutions-IAM5",
    reason:
      "Wildcards come from CDK-generated grants (grantPullPush, grantInvoke, grantReadWrite) where the wildcard is the scoped resource ARN itself (bucket-arn/*, function-arn:*, dynamodb table/index/*) or narrow Bedrock inference-profile patterns. Further tightening would require raw IAM policies that remove CDK's future-proofing.",
  },
  {
    id: "AwsSolutions-L1",
    reason:
      "Python 3.12 is the latest version currently supported by the Lambda handlers (3.13 isn't GA for all CDK bundling flows yet). Upgrade path tracked separately.",
  },
  {
    id: "AwsSolutions-S1",
    reason:
      "Server access logging is unnecessary for ta-config (single JSON file edited manually) and ta-build-artifacts (CI-internal Lambda zips). CloudTrail data events + the versioning we enabled provide the forensic trail we need.",
  },
  {
    id: "AwsSolutions-SMG4",
    reason:
      "The md-store bearer token is issued by the external md-store service, not an AWS-managed credential — Secrets Manager automatic rotation doesn't apply. Rotation is performed manually by regenerating the token at the md-store service side.",
  },
  {
    id: "AwsSolutions-CB4",
    reason:
      "CodeBuild artifacts are written to an S3 bucket with bucket-owner-enforced SSE-S3 encryption; adding a customer-managed KMS key to the build environment itself adds operational cost without meaningful additional protection for this workload.",
  },
  {
    id: "AwsSolutions-SNS3",
    reason:
      "Topic policies enforcing aws:SecureTransport block SES subscription deliveries to internal mail relays in some SES configurations. For an email-only fan-out topic with no cross-account publishers, SSL enforcement is not required.",
  },
  {
    id: "AwsSolutions-SF1",
    reason:
      "Step Functions logging is set to ERROR for cost control. ALL-level logging doubles CloudWatch Logs volume for a workflow that already emits detailed per-ticker Lambda logs.",
  },
  {
    id: "AwsSolutions-SQS3",
    reason:
      "SchedulerDlq IS the dead-letter queue for the EventBridge Scheduler target (not itself consumed by another workload), so it does not need a further DLQ.",
  },
  {
    id: "AwsSolutions-SQS4",
    reason:
      "SQS DLQ is only written by the EventBridge Scheduler service role in this account; no cross-account or external producers exist that would benefit from TLS enforcement.",
  },
  {
    id: "AwsSolutions-ECS4",
    reason:
      "Container Insights is intentionally disabled for cost control; per-task CloudWatch Logs + task metrics are sufficient for this batch workload (one task per ticker per day).",
  },
  {
    id: "AwsSolutions-ECS2",
    reason:
      "Task env vars hold only non-sensitive resource names (S3 bucket, DynamoDB table, ECR tag). Per-run secrets (AgentCore runtime ARN, ticker, date) arrive via Step Functions containerOverrides, not hardcoded in the task definition.",
  },
  {
    id: "AwsSolutions-OS1",
    reason:
      "Observability domain is deliberately public-access so the OSIS pipeline (AWS-managed service) can write to it without VPC peering. Access is restricted via fine-grained access control + IAM.",
  },
  {
    id: "AwsSolutions-OS3",
    reason:
      "OSIS pipeline endpoint is public; IP allowlisting isn't compatible with OSIS (its source IPs aren't stable). Authentication relies on FGAC + SigV4.",
  },
  {
    id: "AwsSolutions-OS4",
    reason:
      "Dedicated master nodes are unnecessary for a single-node observability domain (D1 in the observability plan). This is a low-traffic internal tool; data-node-only shape is the documented decision.",
  },
  {
    id: "AwsSolutions-OS7",
    reason:
      "Zone awareness requires an even number of data nodes across multiple AZs. The plan (D1) specifies a single node to keep costs at ~$25/mo; HA is explicitly out of scope for this observability tool.",
  },
  {
    id: "AwsSolutions-OS9",
    reason:
      "Slow-log publishing doubles CloudWatch Logs cost for a single-node observability tool where operators can query the domain directly. OSIS pipeline already publishes its own logs for the ingest path.",
  },
  {
    id: "AwsSolutions-ELB2",
    reason:
      "Brokerage-MCP ALB access logs are disabled; the upstream Fargate container already logs every /mcp call to CloudWatch with tool name + latency, so ALB-level logs duplicate state without adding forensic value.",
  },
  {
    id: "AwsSolutions-EC23",
    reason:
      "Brokerage-MCP ALB security group allows 0.0.0.0/0 on :80 by design — AgentCore Runtime runs as a managed public service (NetworkMode: PUBLIC) that cannot reach an internal ALB. Access is gated by a shared-secret header checked by the MCP server (BROKERAGE_SHARED_SECRET); only clients with the secret from Secrets Manager can call /mcp.",
  },
]);
