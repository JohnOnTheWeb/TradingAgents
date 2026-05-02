import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as opensearch from "aws-cdk-lib/aws-opensearchservice";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as ses from "aws-cdk-lib/aws-ses";
import * as sns from "aws-cdk-lib/aws-sns";
import * as snsSubs from "aws-cdk-lib/aws-sns-subscriptions";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const LAMBDA_DIR = path.join(REPO_ROOT, "infra", "lambdas");
const EMAIL = "jotw@amazon.com";

// S3 keys CodeBuild writes the Lambda zips to. Must match buildspec.yml.
const DATA_TOOLS_ZIP_KEY = "lambdas/data-tools.zip";
const MEMORY_LOG_ZIP_KEY = "lambdas/memory-log.zip";

export class TradingAgentsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // AgentCore Runtime validates the ECR image at create time, so its
    // creation has to happen AFTER CodeBuild has pushed :latest. We gate
    // it behind a context flag: first deploy = false (creates ECR, CodeBuild,
    // Lambdas, etc.); trigger CodeBuild; second deploy with
    // `-c agentCoreEnabled=true` adds the Runtime + Gateway + Targets.
    const agentCoreEnabled =
      this.node.tryGetContext("agentCoreEnabled") === "true" ||
      this.node.tryGetContext("agentCoreEnabled") === true;

    // Unified observability via OpenSearch + AMP + OSIS. Gated on a context
    // flag (default false) so `cdk synth` stays green until the operator
    // explicitly opts in — mirrors the agentCoreEnabled two-phase rollout.
    const observabilityEnabled =
      this.node.tryGetContext("observabilityEnabled") === "true" ||
      this.node.tryGetContext("observabilityEnabled") === true;

    // ------------------------------------------------------------------
    // Data stores
    // ------------------------------------------------------------------

    const memoryTable = new dynamodb.Table(this, "MemoryLogTable", {
      tableName: "ta-memory-log",
      partitionKey: { name: "ticker", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "trade_date", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    memoryTable.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "trade_date", type: dynamodb.AttributeType.STRING },
    });

    const configBucket = new s3.Bucket(this, "ConfigBucket", {
      bucketName: `ta-config-${this.account}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // (No build-artifacts bucket needed — Gateway Lambdas run as ECR
    // container images sharing the AgentCore Runtime's image, so CodeBuild
    // only produces one artifact: the ECR tag.)

    const mdStoreSecret = new secretsmanager.Secret(this, "MdStoreBearer", {
      secretName: "tradingagents/md-store-bearer",
      description:
        "Bearer token for the md-store MCP server used by TradingAgents",
    });

    // ------------------------------------------------------------------
    // ECR + CodeBuild
    // ------------------------------------------------------------------

    const ecrRepo = new ecr.Repository(this, "AgentCoreImage", {
      repositoryName: "tradingagents-agentcore",
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      lifecycleRules: [
        { maxImageCount: 10, description: "Retain last 10 images" },
      ],
    });

    // CodeBuild sources the repo via a plain `git clone` inside the
    // buildspec so we don't need CodeBuild-level GitHub credentials for
    // this public fork (first-run deploy friendliness).
    const codebuildProject = new codebuild.Project(this, "BuildProject", {
      projectName: "tradingagents-build",
      source: codebuild.Source.gitHub({
        owner: "JohnOnTheWeb",
        repo: "TradingAgents",
        branchOrRef: "main",
        webhook: false,
        cloneDepth: 1,
      }),
      environment: {
        // AgentCore requires ARM64 images; use the ARM CodeBuild image.
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
        computeType: codebuild.ComputeType.SMALL,
        privileged: true,
      },
      buildSpec: codebuild.BuildSpec.fromSourceFilename("buildspec.yml"),
      environmentVariables: {
        AWS_ACCOUNT_ID: { value: this.account },
        AWS_REGION: { value: this.region },
        ECR_REPOSITORY: { value: ecrRepo.repositoryName },
      },
      logging: {
        cloudWatch: {
          logGroup: new logs.LogGroup(this, "BuildLogs", {
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
          }),
        },
      },
    });
    ecrRepo.grantPullPush(codebuildProject);

    // ------------------------------------------------------------------
    // Observability — OpenSearch + AMP + OSIS (gated)
    //
    // Shape follows the AWS blog "Unified observability in Amazon
    // OpenSearch Service": traces + logs land in OpenSearch via an OSIS
    // pipeline (simple-schema-for-observability indices), metrics land in
    // Amazon Managed Prometheus via remote_write. Exposed ingest endpoint
    // is SigV4-authenticated; the Runtime + Fargate task roles get
    // osis:Ingest below.
    // ------------------------------------------------------------------

    let observabilityDomain: opensearch.Domain | undefined;
    let ampWorkspace: cdk.CfnResource | undefined;
    let osisPipeline: cdk.CfnResource | undefined;
    let osisPipelineArn: string | undefined;
    let osisIngestEndpoint: string | undefined;

    if (observabilityEnabled) {
      const observabilityAdminRole = new iam.Role(
        this,
        "ObservabilityAdminRole",
        {
          roleName: "ta-observability-admin",
          assumedBy: new iam.AccountPrincipal(this.account),
          description:
            "Admin role that can sign in to OpenSearch Dashboards for TradingAgents observability",
        },
      );

      const osMasterSecret = new secretsmanager.Secret(
        this,
        "OpenSearchMasterSecret",
        {
          secretName: "tradingagents/opensearch-master",
          description:
            "Fine-grained access control master user for the TradingAgents OpenSearch domain",
          generateSecretString: {
            secretStringTemplate: JSON.stringify({ username: "tradingagents" }),
            generateStringKey: "password",
            // OpenSearch FGAC requires upper + lower + digit + special; keep the
            // char set Secrets-Manager friendly by excluding quoting/escape chars.
            excludeCharacters: "\"'\\/@ ",
            includeSpace: false,
            passwordLength: 24,
            requireEachIncludedType: true,
          },
        },
      );

      // OSIS pipeline role — trusted by the Ingestion service. Created
      // before the Domain so we can set the access policy inline on the
      // domain (avoids a race against CDK's async addAccessPolicies custom
      // resource — OSIS validates perms at create time).
      const osisPipelineRole = new iam.Role(this, "OsisPipelineRole", {
        roleName: "ta-osis-pipeline-role",
        assumedBy: new iam.ServicePrincipal("osis-pipelines.amazonaws.com"),
        description:
          "Role OSIS assumes to write to OpenSearch + Amazon Managed Prometheus",
      });

      observabilityDomain = new opensearch.Domain(
        this,
        "ObservabilityDomain",
        {
          version: opensearch.EngineVersion.OPENSEARCH_2_17,
          domainName: "ta-observability",
          capacity: {
            dataNodes: 1,
            dataNodeInstanceType: "t3.small.search",
            masterNodes: 0,
          },
          ebs: {
            volumeSize: 20,
            volumeType: ec2.EbsDeviceVolumeType.GP3,
          },
          zoneAwareness: { enabled: false },
          enforceHttps: true,
          nodeToNodeEncryption: true,
          encryptionAtRest: { enabled: true },
          fineGrainedAccessControl: {
            masterUserName: "tradingagents",
            masterUserPassword: osMasterSecret.secretValueFromJson("password"),
          },
          accessPolicies: [
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              principals: [
                new iam.ArnPrincipal(osisPipelineRole.roleArn),
                new iam.ArnPrincipal(observabilityAdminRole.roleArn),
              ],
              actions: ["es:ESHttp*"],
              resources: [
                `arn:aws:es:${this.region}:${this.account}:domain/ta-observability/*`,
              ],
            }),
          ],
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        },
      );
      cdk.Tags.of(observabilityDomain).add("UsedBy", "TauricTrading");

      // Attach the pipeline role's domain perms as a discrete Policy so we
      // can make the OSIS pipeline explicitly depend on it (defeats the
      // race where OSIS validates before the role's DefaultPolicy exists).
      const osisPipelineRolePolicy = new iam.Policy(
        this,
        "OsisPipelineRolePolicy",
        {
          policyName: "ta-osis-pipeline-role-policy",
          statements: [
            new iam.PolicyStatement({
              actions: ["es:DescribeDomain", "es:ESHttp*"],
              resources: [
                observabilityDomain.domainArn,
                `${observabilityDomain.domainArn}/*`,
              ],
            }),
          ],
        },
      );
      osisPipelineRolePolicy.attachToRole(osisPipelineRole);

      // Amazon Managed Prometheus workspace (L1 — no L2 construct yet).
      ampWorkspace = new cdk.CfnResource(this, "AmpWorkspace", {
        type: "AWS::APS::Workspace",
        properties: {
          Alias: "tradingagents-metrics",
          Tags: [{ Key: "UsedBy", Value: "TauricTrading" }],
        },
      });
      const ampWorkspaceArn = cdk.Fn.getAtt(
        ampWorkspace.logicalId,
        "Arn",
      ).toString();
      const ampRemoteWriteUrl = cdk.Fn.join("", [
        "https://aps-workspaces.",
        this.region,
        ".amazonaws.com/workspaces/",
        cdk.Fn.getAtt(ampWorkspace.logicalId, "WorkspaceId").toString(),
        "/api/v1/remote_write",
      ]);
      osisPipelineRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ["aps:RemoteWrite"],
          resources: [ampWorkspaceArn],
        }),
      );

      // OSIS CloudWatch log group.
      const osisLogGroup = new logs.LogGroup(this, "OsisPipelineLogs", {
        logGroupName: "/aws/vendedlogs/OpenSearchService/pipelines/ta-otel",
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      const pipelineName = "ta-otel";
      const osHost = cdk.Fn.join("", [
        "https://",
        observabilityDomain.domainEndpoint,
      ]);

      // Pipeline configuration YAML — single OTLP traces sub-pipeline.
      // Traces use index_type: trace-analytics-raw (index name managed by
      // the plugin). OTel logs (otel_logs_source) and metrics (prometheus
      // sink) are deferred — the app doesn't emit structured OTel logs
      // yet, and the prometheus sink has non-trivial auth semantics that
      // need a separate pass. All three dashboards (Fleet/Run/Ticker)
      // read from traces only, so this is sufficient for Phase-A.
      const pipelineConfig = cdk.Fn.join("", [
        "version: '2'\n",
        "otlp-traces:\n",
        "  source:\n",
        "    otel_trace_source:\n",
        "      path: /v1/traces\n",
        "  sink:\n",
        "    - opensearch:\n",
        `        hosts: [ "${osHost}" ]\n`,
        "        aws:\n",
        `          sts_role_arn: "${osisPipelineRole.roleArn}"\n`,
        `          region: "${this.region}"\n`,
        "        index_type: trace-analytics-raw\n",
      ]);

      osisPipeline = new cdk.CfnResource(this, "OsisPipeline", {
        type: "AWS::OSIS::Pipeline",
        properties: {
          PipelineName: pipelineName,
          MinUnits: 1,
          MaxUnits: 2,
          PipelineConfigurationBody: pipelineConfig,
          LogPublishingOptions: {
            IsLoggingEnabled: true,
            CloudWatchLogDestination: {
              LogGroup: osisLogGroup.logGroupName,
            },
          },
          Tags: [{ Key: "UsedBy", Value: "TauricTrading" }],
        },
      });
      osisPipeline.addDependency(
        observabilityDomain.node.defaultChild as cdk.CfnResource,
      );
      osisPipeline.addDependency(ampWorkspace);
      // OSIS validates the role's access to the domain at CreatePipeline
      // time; the role's inline policy must exist first.
      osisPipeline.node.addDependency(osisPipelineRolePolicy);

      osisPipelineArn = cdk.Fn.getAtt(
        osisPipeline.logicalId,
        "PipelineArn",
      ).toString();
      const ingestEndpoints = cdk.Token.asList(
        cdk.Fn.getAtt(osisPipeline.logicalId, "IngestEndpointUrls"),
      );
      osisIngestEndpoint = cdk.Fn.join("", [
        "https://",
        cdk.Fn.select(0, ingestEndpoints),
      ]);
    }

    // ------------------------------------------------------------------
    // Notification plumbing
    // ------------------------------------------------------------------

    const notificationsTopic = new sns.Topic(this, "NotificationsTopic", {
      topicName: "tradingagents-notifications",
      displayName: "TradingAgents Run Notifications",
    });
    notificationsTopic.addSubscription(new snsSubs.EmailSubscription(EMAIL));

    new ses.EmailIdentity(this, "SenderIdentity", {
      identity: ses.Identity.email(EMAIL),
    });

    // ------------------------------------------------------------------
    // Lambdas
    // ------------------------------------------------------------------

    const pythonRuntime = lambda.Runtime.PYTHON_3_12;

    // --- Orchestration Lambdas (no tradingagents package needed) ------

    const getConfigFn = new lambda.Function(this, "GetConfigFn", {
      functionName: "ta-get-config",
      runtime: pythonRuntime,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, "get_config")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        TRADINGAGENTS_CONFIG_BUCKET: configBucket.bucketName,
        DEFAULT_DEEP_MODEL: "us.anthropic.claude-opus-4-7",
        DEFAULT_QUICK_MODEL: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    configBucket.grantRead(getConfigFn);

    // NOTE: The old ta-invoke-agent Lambda has been replaced by an ECS
    // Fargate task (see TaskRunner below). Lambda's 15-min hard cap made
    // long deep-research runs impossible; Fargate has no such limit.

    const aggregateFn = new lambda.Function(this, "AggregateFn", {
      functionName: "ta-aggregate",
      runtime: pythonRuntime,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, "aggregate")),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      environment: {
        MD_STORE_SECRET_ID: mdStoreSecret.secretName,
        MD_STORE_AGENT_ID: "tauric-traders",
        SNS_NOTIFICATIONS_TOPIC: notificationsTopic.topicArn,
        TRADINGAGENTS_CONFIG_BUCKET: configBucket.bucketName,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    mdStoreSecret.grantRead(aggregateFn);
    notificationsTopic.grantPublish(aggregateFn);
    // Aggregator reads per-ticker result JSONs written by Fargate tasks.
    configBucket.grantRead(aggregateFn);

    const errorHandlerFn = new lambda.Function(this, "ErrorHandlerFn", {
      functionName: "ta-error-handler",
      runtime: pythonRuntime,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, "error_handler")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      environment: {
        SNS_NOTIFICATIONS_TOPIC: notificationsTopic.topicArn,
        LOG_GROUP_NAME: "/aws/lambda/ta-error-handler",
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    notificationsTopic.grantPublish(errorHandlerFn);

    // --- Gateway-target Lambdas (bundled with tradingagents package) --

    // Gateway-target Lambdas are container images pulled from the same
    // ECR repo as the AgentCore Runtime. The container image supports up
    // to 10 GB, avoiding the 250 MB unzipped zip limit that pandas +
    // yfinance would blow past. Because the image has to exist before
    // these Lambdas can be created, we gate them (and the downstream
    // Gateway + targets) behind the same `agentCoreEnabled` context flag.
    let dataToolsFn: lambda.DockerImageFunction | undefined;
    let memoryLogFn: lambda.DockerImageFunction | undefined;

    if (agentCoreEnabled) {
      dataToolsFn = new lambda.DockerImageFunction(this, "DataToolsFn", {
        functionName: "ta-mcp-data-tools",
        code: lambda.DockerImageCode.fromEcr(ecrRepo, {
          tagOrDigest: "latest",
          cmd: ["handler.handler"],
          entrypoint: [
            "/usr/local/bin/python",
            "-m",
            "awslambdaric",
          ],
          workingDirectory: "/home/appuser/app/infra/lambdas/data_tools",
        }),
        architecture: lambda.Architecture.ARM_64,
        timeout: cdk.Duration.minutes(2),
        memorySize: 1024,
        environment: {
          TRADINGAGENTS_MEMORY_BACKEND: "dynamodb",
          TRADINGAGENTS_MEMORY_TABLE: memoryTable.tableName,
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });

      memoryLogFn = new lambda.DockerImageFunction(this, "MemoryLogFn", {
        functionName: "ta-mcp-memory-log",
        code: lambda.DockerImageCode.fromEcr(ecrRepo, {
          tagOrDigest: "latest",
          cmd: ["handler.handler"],
          entrypoint: [
            "/usr/local/bin/python",
            "-m",
            "awslambdaric",
          ],
          workingDirectory: "/home/appuser/app/infra/lambdas/memory_log",
        }),
        architecture: lambda.Architecture.ARM_64,
        timeout: cdk.Duration.seconds(30),
        memorySize: 512,
        environment: {
          TRADINGAGENTS_MEMORY_TABLE: memoryTable.tableName,
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      memoryTable.grantReadWriteData(memoryLogFn);
    }

    // ------------------------------------------------------------------
    // AgentCore Runtime + Gateway + Targets (raw CFN; no L2 construct yet)
    // ------------------------------------------------------------------

    const runtimeRole = new iam.Role(this, "AgentCoreRuntimeRole", {
      roleName: "ta-agentcore-runtime-role",
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description:
        "Execution role for the TradingAgents AgentCore runtime container",
    });
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        // Cross-region inference profiles fan out to multiple regions (e.g.
        // us. prefix routes to us-east-1/us-east-2/us-west-2) so the
        // foundation-model ARN needs a region wildcard; the profile ARN
        // itself is account-scoped to this region.
        resources: [
          `arn:aws:bedrock:*::foundation-model/anthropic.claude-*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.*`,
        ],
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeGateway"],
        resources: ["*"],
      }),
    );
    mdStoreSecret.grantRead(runtimeRole);
    memoryTable.grantReadWriteData(runtimeRole);
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: [`arn:aws:logs:${this.region}:${this.account}:*`],
      }),
    );
    // AgentCore Runtime needs to pull the ECR image — these are the exact
    // actions AgentCore's managed control plane requires at create time.
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",
        ],
        resources: [ecrRepo.repositoryArn],
      }),
    );
    if (observabilityEnabled && osisPipelineArn) {
      runtimeRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ["osis:Ingest"],
          resources: [osisPipelineArn],
        }),
      );
    }

    // Shared env vars injected when observability is on. AgentCore Runtime
    // and Fargate both need the same OTLP config so spans from both sides
    // reach the OSIS endpoint. Gated additionally on agentCoreEnabled so the
    // observability flag can be flipped on during a phase-1 deploy without
    // the app image failing (spans just land locally until the rebuilt
    // image ships).
    const observabilityEnvVars =
      observabilityEnabled && agentCoreEnabled && osisIngestEndpoint
        ? {
            OTEL_EXPORTER_OTLP_ENDPOINT: osisIngestEndpoint,
            OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf",
            OTEL_RESOURCE_ATTRIBUTES:
              "deployment.environment=prod,service.namespace=tradingagents",
            TA_OTEL_SIGV4: "1",
          }
        : undefined;

    let agentRuntime: cdk.CfnResource | undefined;
    let gateway: cdk.CfnResource | undefined;

    if (agentCoreEnabled) {
      agentRuntime = new cdk.CfnResource(this, "AgentCoreRuntime", {
        type: "AWS::BedrockAgentCore::Runtime",
        properties: {
          // Pattern [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens allowed.
          AgentRuntimeName: "tradingagents_runtime",
          RoleArn: runtimeRole.roleArn,
          AgentRuntimeArtifact: {
            ContainerConfiguration: {
              ContainerUri: `${ecrRepo.repositoryUri}:latest`,
            },
          },
          EnvironmentVariables: {
            TRADINGAGENTS_MEMORY_BACKEND: "dynamodb",
            TRADINGAGENTS_MEMORY_TABLE: memoryTable.tableName,
            MD_STORE_SECRET_ID: mdStoreSecret.secretName,
            MD_STORE_AGENT_ID: "tauric-traders",
            AWS_DEFAULT_REGION: this.region,
            ...(observabilityEnvVars
              ? {
                  ...observabilityEnvVars,
                  OTEL_SERVICE_NAME: "tradingagents-runtime",
                }
              : {}),
          },
          ProtocolConfiguration: "HTTP",
          NetworkConfiguration: { NetworkMode: "PUBLIC" },
          Tags: { UsedBy: "TauricTrading" },
        },
      });

      // Feed the runtime ARN into the invoker Lambda env.
      // Note: the runtime ARN is consumed by the Fargate task (see
      // TaskRunnerTaskDef below) via containerOverrides, not a Lambda env.
    }

    // --- Gateway ------------------------------------------------------

    const gatewayRole = new iam.Role(this, "GatewayRole", {
      roleName: "ta-gateway-role",
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description:
        "Service role that AgentCore Gateway uses to invoke MCP target Lambdas",
    });

    if (agentCoreEnabled && dataToolsFn && memoryLogFn) {
      dataToolsFn.grantInvoke(gatewayRole);
      memoryLogFn.grantInvoke(gatewayRole);

      gateway = new cdk.CfnResource(this, "AgentCoreGateway", {
        type: "AWS::BedrockAgentCore::Gateway",
        properties: {
          Name: "tradingagents-gw",
          ProtocolType: "MCP",
          RoleArn: gatewayRole.roleArn,
          AuthorizerType: "AWS_IAM",
          Tags: { UsedBy: "TauricTrading" },
        },
      });

      // Tool schemas — declared inline because Gateway requires them for
      // every MCP Lambda target. Kept intentionally minimal: ticker + date
      // where applicable. The Lambdas accept additional fields but Gateway
      // only advertises these to agent clients.
      const dateInput = {
        Type: "object",
        Properties: {
          ticker: { Type: "string", Description: "Stock ticker symbol" },
          trade_date: {
            Type: "string",
            Description: "ISO trade date, YYYY-MM-DD",
          },
        },
        Required: ["ticker", "trade_date"],
      };

      const dataToolsTarget = new cdk.CfnResource(
        this,
        "GatewayTargetDataTools",
        {
          type: "AWS::BedrockAgentCore::GatewayTarget",
          properties: {
            GatewayIdentifier: gateway.ref,
            Name: "data-tools",
            Description: "Market-data tools: yfinance / alpha_vantage",
            CredentialProviderConfigurations: [
              { CredentialProviderType: "GATEWAY_IAM_ROLE" },
            ],
            TargetConfiguration: {
              Mcp: {
                Lambda: {
                  LambdaArn: dataToolsFn.functionArn,
                  ToolSchema: {
                    InlinePayload: [
                      {
                        Name: "get_stock_data",
                        Description: "OHLCV price history for a ticker",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_indicators",
                        Description: "Technical indicators (MACD, RSI, etc.)",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_fundamentals",
                        Description: "Company fundamentals summary",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_balance_sheet",
                        Description: "Latest balance-sheet items",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_cashflow",
                        Description: "Cash-flow statement summary",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_income_statement",
                        Description: "Income statement summary",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_news",
                        Description: "Ticker-specific news headlines",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_insider_transactions",
                        Description: "Recent insider trades",
                        InputSchema: dateInput,
                      },
                      {
                        Name: "get_global_news",
                        Description: "Top macro / global news",
                        InputSchema: {
                          Type: "object",
                          Properties: {
                            trade_date: {
                              Type: "string",
                              Description: "ISO trade date",
                            },
                          },
                          Required: ["trade_date"],
                        },
                      },
                    ],
                  },
                },
              },
            },
          },
        },
      );
      dataToolsTarget.addDependency(gateway);

      const memoryLogTarget = new cdk.CfnResource(
        this,
        "GatewayTargetMemoryLog",
        {
          type: "AWS::BedrockAgentCore::GatewayTarget",
          properties: {
            GatewayIdentifier: gateway.ref,
            Name: "memory-log",
            Description: "Persistent decision log backed by DynamoDB",
            CredentialProviderConfigurations: [
              { CredentialProviderType: "GATEWAY_IAM_ROLE" },
            ],
            TargetConfiguration: {
              Mcp: {
                Lambda: {
                  LambdaArn: memoryLogFn.functionArn,
                  ToolSchema: {
                    InlinePayload: [
                      {
                        Name: "get_past_context",
                        Description:
                          "Recent same-ticker decisions plus cross-ticker lessons",
                        InputSchema: {
                          Type: "object",
                          Properties: {
                            ticker: { Type: "string" },
                            n_same: { Type: "integer" },
                            n_cross: { Type: "integer" },
                          },
                          Required: ["ticker"],
                        },
                      },
                      {
                        Name: "store_decision",
                        Description: "Append a pending decision to the log",
                        InputSchema: {
                          Type: "object",
                          Properties: {
                            ticker: { Type: "string" },
                            trade_date: { Type: "string" },
                            final_trade_decision: { Type: "string" },
                          },
                          Required: [
                            "ticker",
                            "trade_date",
                            "final_trade_decision",
                          ],
                        },
                      },
                      {
                        Name: "get_pending_entries",
                        Description:
                          "List pending decisions awaiting outcome resolution",
                        InputSchema: {
                          Type: "object",
                          Properties: {},
                        },
                      },
                    ],
                  },
                },
              },
            },
          },
        },
      );
      memoryLogTarget.addDependency(gateway);
    }

    // ------------------------------------------------------------------
    // ECS Fargate — per-ticker invoker replaces the old ta-invoke-agent
    // Lambda (which was boxed in by Lambda's 15-min hard cap).
    //
    // The task reuses the same ECR image; Step Functions overrides the
    // container CMD with `python -m tradingagents.agentcore.task_runner`
    // and injects per-ticker env vars.
    // ------------------------------------------------------------------

    let ecsCluster: ecs.Cluster | undefined;
    let taskDef: ecs.FargateTaskDefinition | undefined;
    let taskSecurityGroup: ec2.SecurityGroup | undefined;
    let taskVpc: ec2.IVpc | undefined;
    let agentRuntimeArnValue: string | undefined;

    if (agentCoreEnabled && agentRuntime) {
      taskVpc = ec2.Vpc.fromLookup(this, "DefaultVpc", { isDefault: true });

      ecsCluster = new ecs.Cluster(this, "TaskCluster", {
        clusterName: "tradingagents-tasks",
        vpc: taskVpc,
        containerInsightsV2: ecs.ContainerInsights.DISABLED,
      });

      const taskLogGroup = new logs.LogGroup(this, "TaskRunnerLogs", {
        logGroupName: "/aws/ecs/tradingagents-tasks",
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      taskDef = new ecs.FargateTaskDefinition(this, "TaskRunnerTaskDef", {
        family: "tradingagents-task-runner",
        cpu: 1024,
        memoryLimitMiB: 2048,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.ARM64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
      });

      taskDef.addContainer("TaskRunner", {
        containerName: "task-runner",
        image: ecs.ContainerImage.fromEcrRepository(ecrRepo, "latest"),
        // Override the image's uvicorn CMD so this container runs the
        // Fargate invoker instead of the AgentCore FastAPI server.
        entryPoint: ["python", "-m"],
        command: ["tradingagents.agentcore.task_runner"],
        workingDirectory: "/home/appuser/app",
        logging: ecs.LogDrivers.awsLogs({
          logGroup: taskLogGroup,
          streamPrefix: "task",
        }),
        // TA_* env vars are supplied per-run via containerOverrides on the
        // RunTask call; only the constants live here.
        environment: {
          TA_CONFIG_BUCKET: configBucket.bucketName,
          TA_RESULT_KEY_PREFIX: "runs/",
          AGENTCORE_TIMEOUT: "3600",
          TRADINGAGENTS_MEMORY_BACKEND: "dynamodb",
          TRADINGAGENTS_MEMORY_TABLE: memoryTable.tableName,
          ...(observabilityEnvVars
            ? {
                ...observabilityEnvVars,
                OTEL_SERVICE_NAME: "tradingagents-task-runner",
              }
            : {}),
        },
      });

      // Task role (application permissions): invoke AgentCore Runtime + write S3 results.
      taskDef.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ["bedrock-agentcore:InvokeAgentRuntime"],
          resources: ["*"],
        }),
      );
      configBucket.grantReadWrite(taskDef.taskRole);
      if (observabilityEnabled && osisPipelineArn) {
        taskDef.taskRole.addToPrincipalPolicy(
          new iam.PolicyStatement({
            actions: ["osis:Ingest"],
            resources: [osisPipelineArn],
          }),
        );
      }

      // Security group — egress-only, no inbound.
      taskSecurityGroup = new ec2.SecurityGroup(this, "TaskSg", {
        vpc: taskVpc,
        securityGroupName: "tradingagents-task-sg",
        description: "Egress-only SG for TradingAgents Fargate tasks",
        allowAllOutbound: true,
      });

      // Full AgentCore runtime ARN — passed per-run to the container.
      agentRuntimeArnValue = cdk.Fn.getAtt(
        agentRuntime.logicalId,
        "AgentRuntimeArn",
      ).toString();
    }

    // ------------------------------------------------------------------
    // Step Functions state machine
    // ------------------------------------------------------------------

    const buildErrorBranch = (stage: string): sfn.IChainable => {
      const notify = new tasks.LambdaInvoke(this, `NotifyError_${stage}`, {
        lambdaFunction: errorHandlerFn,
        payload: sfn.TaskInput.fromObject({
          stage,
          // Keep the payload shape forgiving — different catch sites have
          // different input shapes (GetConfig has no $.config yet; Map
          // catches see the parent state's input, not a per-iteration one).
          // Pass the whole current input as "context" plus the error dict.
          "context.$": "States.JsonToString($)",
          "error.$": "$.error",
        }),
      });
      return notify.next(new sfn.Fail(this, `Failed_${stage}`));
    };

    const getConfigTask = new tasks.LambdaInvoke(this, "GetConfigTask", {
      lambdaFunction: getConfigFn,
      resultSelector: {
        "run_id.$": "$.Payload.run_id",
        "trade_date.$": "$.Payload.trade_date",
        "deep_model.$": "$.Payload.deep_model",
        "quick_model.$": "$.Payload.quick_model",
        "tickers.$": "$.Payload.tickers",
      },
      resultPath: "$.config",
    });
    getConfigTask.addCatch(buildErrorBranch("get_config"), {
      resultPath: "$.error",
    });

    // ------------------------------------------------------------------
    // Per-ticker Fargate run (inside the Map)
    // ------------------------------------------------------------------
    //
    // The Map iterates the expanded tickers array from GetConfig. For each
    // ticker we RunTask on Fargate with per-ticker env vars. The task
    // writes its result to s3://config_bucket/runs/<run_id>/<ticker>.json;
    // Map output is intentionally minimal because the aggregator reads
    // those S3 objects directly.

    // Build a concrete per-ticker run only when Fargate is available.
    // When agentCoreEnabled=false, the state machine short-circuits to
    // the aggregator with an empty ticker list so the stack still syncs.
    const perTickerRun =
      agentCoreEnabled && ecsCluster && taskDef && taskSecurityGroup && agentRuntimeArnValue
        ? new tasks.EcsRunTask(this, "RunTickerOnFargate", {
            cluster: ecsCluster,
            taskDefinition: taskDef,
            launchTarget: new tasks.EcsFargateLaunchTarget({
              platformVersion: ecs.FargatePlatformVersion.LATEST,
            }),
            assignPublicIp: true,
            subnets: { subnetType: ec2.SubnetType.PUBLIC },
            securityGroups: [taskSecurityGroup],
            integrationPattern: sfn.IntegrationPattern.RUN_JOB,
            containerOverrides: [
              {
                containerDefinition: taskDef.defaultContainer!,
                environment: [
                  { name: "TA_RUN_ID", value: sfn.JsonPath.stringAt("$.run_id") },
                  { name: "TA_TICKER", value: sfn.JsonPath.stringAt("$.ticker.symbol") },
                  { name: "TA_TRADE_DATE", value: sfn.JsonPath.stringAt("$.trade_date") },
                  { name: "AGENTCORE_RUNTIME_ARN", value: agentRuntimeArnValue },
                  {
                    name: "TA_ANALYSTS",
                    value: sfn.JsonPath.jsonToString(
                      sfn.JsonPath.objectAt("$.ticker.analysts"),
                    ),
                  },
                  {
                    name: "TA_DEBATE_ROUNDS",
                    value: sfn.JsonPath.format(
                      "{}",
                      sfn.JsonPath.stringAt("$.ticker.debate_rounds"),
                    ),
                  },
                  { name: "TA_DEEP_MODEL", value: sfn.JsonPath.stringAt("$.deep_model") },
                  { name: "TA_QUICK_MODEL", value: sfn.JsonPath.stringAt("$.quick_model") },
                ],
              },
            ],
            resultPath: sfn.JsonPath.DISCARD,
          })
        : null;

    const tickerMap = new sfn.Map(this, "PerTickerMap", {
      maxConcurrency: 3,
      itemsPath: "$.config.tickers",
      itemSelector: {
        "run_id.$": "$.config.run_id",
        "trade_date.$": "$.config.trade_date",
        "deep_model.$": "$.config.deep_model",
        "quick_model.$": "$.config.quick_model",
        "ticker.$": "$$.Map.Item.Value",
      },
      // Per-iteration output discarded; aggregator reads results from S3.
      resultPath: sfn.JsonPath.DISCARD,
    });
    if (perTickerRun) {
      tickerMap.itemProcessor(perTickerRun);
    } else {
      // Agent not yet enabled (phase-1 deploy). Give the Map a no-op item
      // processor so cdk synth succeeds; the state machine isn't meant to
      // be invoked until phase-2.
      tickerMap.itemProcessor(new sfn.Pass(this, "NoopTickerPass"));
    }
    // Map-level catch: route to the notifier if the Map state itself
    // explodes (e.g. invalid items). Per-ticker failures are tolerated by
    // the Fargate task (it writes a failure JSON to S3 and exits non-zero).
    tickerMap.addCatch(buildErrorBranch("invoke_agent_map"), {
      resultPath: sfn.JsonPath.DISCARD,
    });

    const aggregateTask = new tasks.LambdaInvoke(this, "AggregateTask", {
      lambdaFunction: aggregateFn,
      payload: sfn.TaskInput.fromObject({
        "run_id.$": "$.config.run_id",
        "trade_date.$": "$.config.trade_date",
        "tickers.$": "$.config.tickers",
        "config_bucket": configBucket.bucketName,
      }),
      outputPath: "$.Payload",
    });
    aggregateTask.addCatch(buildErrorBranch("aggregate"), {
      resultPath: "$.error",
    });

    const chain = getConfigTask.next(tickerMap).next(aggregateTask);

    const stateMachineLogGroup = new logs.LogGroup(this, "StateMachineLogs", {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const stateMachine = new sfn.StateMachine(this, "StateMachine", {
      stateMachineName: "tradingagents-run",
      stateMachineType: sfn.StateMachineType.STANDARD,
      definitionBody: sfn.DefinitionBody.fromChainable(chain),
      timeout: cdk.Duration.hours(2),
      tracingEnabled: true,
      logs: {
        destination: stateMachineLogGroup,
        level: sfn.LogLevel.ERROR,
        includeExecutionData: true,
      },
    });

    // ------------------------------------------------------------------
    // EventBridge Scheduler
    // ------------------------------------------------------------------

    const dlq = new sqs.Queue(this, "SchedulerDlq", {
      queueName: "tradingagents-scheduler-dlq",
      retentionPeriod: cdk.Duration.days(14),
    });

    const schedulerRole = new iam.Role(this, "SchedulerRole", {
      roleName: "ta-scheduler-role",
      assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com"),
    });
    stateMachine.grantStartExecution(schedulerRole);
    dlq.grantSendMessages(schedulerRole);

    new scheduler.CfnSchedule(this, "DailySchedule", {
      name: "tradingagents-daily",
      description:
        "MON-FRI 18:00 ET — kick off the TradingAgents multi-ticker run",
      scheduleExpression: "cron(0 22 ? * MON-FRI *)",
      scheduleExpressionTimezone: "UTC",
      state: "DISABLED", // enable manually after smoke test
      flexibleTimeWindow: { mode: "OFF" },
      target: {
        arn: stateMachine.stateMachineArn,
        roleArn: schedulerRole.roleArn,
        input: JSON.stringify({ config_key: "watchlist.json" }),
        retryPolicy: {
          maximumEventAgeInSeconds: 3600,
          maximumRetryAttempts: 2,
        },
        deadLetterConfig: { arn: dlq.queueArn },
      },
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------

    new cdk.CfnOutput(this, "StateMachineArnOut", {
      value: stateMachine.stateMachineArn,
      description:
        'Start execution with input {"config_key":"watchlist.json"}',
    });
    new cdk.CfnOutput(this, "EcrRepoUriOut", { value: ecrRepo.repositoryUri });
    new cdk.CfnOutput(this, "ConfigBucketOut", { value: configBucket.bucketName });
    new cdk.CfnOutput(this, "MemoryTableOut", { value: memoryTable.tableName });
    new cdk.CfnOutput(this, "NotificationsTopicOut", {
      value: notificationsTopic.topicArn,
    });
    if (agentRuntime) {
      new cdk.CfnOutput(this, "AgentCoreRuntimeArnOut", {
        value: cdk.Fn.getAtt(
          agentRuntime.logicalId,
          "AgentRuntimeArn",
        ).toString(),
      });
    }
    if (gateway) {
      new cdk.CfnOutput(this, "GatewayUrlOut", {
        value: cdk.Fn.getAtt(gateway.logicalId, "GatewayUrl").toString(),
      });
    }
    if (observabilityDomain) {
      new cdk.CfnOutput(this, "OpenSearchDomainEndpoint", {
        value: observabilityDomain.domainEndpoint,
      });
      new cdk.CfnOutput(this, "OpenSearchDashboardsUrl", {
        value: `https://${observabilityDomain.domainEndpoint}/_dashboards/`,
      });
    }
    if (ampWorkspace) {
      new cdk.CfnOutput(this, "AmpWorkspaceId", {
        value: cdk.Fn.getAtt(ampWorkspace.logicalId, "WorkspaceId").toString(),
      });
    }
    if (osisIngestEndpoint) {
      new cdk.CfnOutput(this, "OsisPipelineIngestUrl", {
        value: osisIngestEndpoint,
      });
    }
  }
}
