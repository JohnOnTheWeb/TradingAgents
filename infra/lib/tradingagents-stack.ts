import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
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

    // Bucket CodeBuild writes Lambda zips to. Versioned so every rebuild
    // produces a new object version; the Lambdas below reference the
    // current version, and we update them via a follow-up `cdk deploy`
    // (or `aws lambda update-function-code` hook in the buildspec).
    const buildArtifactsBucket = new s3.Bucket(this, "BuildArtifactsBucket", {
      bucketName: `ta-build-artifacts-${this.account}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          // Keep the current version indefinitely; expire old versions after 30 days.
          noncurrentVersionExpiration: cdk.Duration.days(30),
        },
      ],
    });

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
        BUILD_ARTIFACTS_BUCKET: { value: buildArtifactsBucket.bucketName },
        DATA_TOOLS_FN_NAME: { value: "ta-mcp-data-tools" },
        MEMORY_LOG_FN_NAME: { value: "ta-mcp-memory-log" },
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
    buildArtifactsBucket.grantReadWrite(codebuildProject);
    // CodeBuild updates the Lambda code inline after each build so we don't
    // need a second `cdk deploy` just to pick up a new zip.
    codebuildProject.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:UpdateFunctionCode"],
        resources: [
          `arn:aws:lambda:${this.region}:${this.account}:function:ta-mcp-data-tools`,
          `arn:aws:lambda:${this.region}:${this.account}:function:ta-mcp-memory-log`,
        ],
      }),
    );

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

    const invokeAgentFn = new lambda.Function(this, "InvokeAgentFn", {
      functionName: "ta-invoke-agent",
      runtime: pythonRuntime,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, "invoke_agent")),
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      environment: {
        // AGENTCORE_RUNTIME_ARN is set below once the runtime resource exists.
        AGENTCORE_TIMEOUT: "900",
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    invokeAgentFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: ["*"],
      }),
    );

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
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    mdStoreSecret.grantRead(aggregateFn);
    notificationsTopic.grantPublish(aggregateFn);

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

    // Gateway-target Lambdas: code zips are produced by CodeBuild and
    // uploaded to the build-artifacts bucket. The first deploy uses the
    // placeholder zip shipped under infra/lambdas/_bootstrap/ so the
    // resources can be created before CodeBuild has run; CodeBuild then
    // swaps in the real package via `aws lambda update-function-code`.
    const dataToolsFn = new lambda.Function(this, "DataToolsFn", {
      functionName: "ta-mcp-data-tools",
      runtime: pythonRuntime,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, "_bootstrap")),
      timeout: cdk.Duration.minutes(2),
      memorySize: 1024,
      environment: {
        TRADINGAGENTS_MEMORY_BACKEND: "dynamodb",
        TRADINGAGENTS_MEMORY_TABLE: memoryTable.tableName,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    buildArtifactsBucket.grantRead(dataToolsFn);

    const memoryLogFn = new lambda.Function(this, "MemoryLogFn", {
      functionName: "ta-mcp-memory-log",
      runtime: pythonRuntime,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, "_bootstrap")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        TRADINGAGENTS_MEMORY_TABLE: memoryTable.tableName,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    buildArtifactsBucket.grantRead(memoryLogFn);
    memoryTable.grantReadWriteData(memoryLogFn);

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
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.*`,
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
          },
          ProtocolConfiguration: "HTTP",
          NetworkConfiguration: { NetworkMode: "PUBLIC" },
          Tags: { UsedBy: "TauricTrading" },
        },
      });

      // Feed the runtime ARN into the invoker Lambda env.
      // Per CFN docs, Ref on AWS::BedrockAgentCore::Runtime returns the ARN.
      (invokeAgentFn.node.defaultChild as lambda.CfnFunction).addPropertyOverride(
        "Environment.Variables.AGENTCORE_RUNTIME_ARN",
        agentRuntime.ref,
      );
    }

    // --- Gateway ------------------------------------------------------

    const gatewayRole = new iam.Role(this, "GatewayRole", {
      roleName: "ta-gateway-role",
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description:
        "Service role that AgentCore Gateway uses to invoke MCP target Lambdas",
    });
    dataToolsFn.grantInvoke(gatewayRole);
    memoryLogFn.grantInvoke(gatewayRole);

    if (agentCoreEnabled) {
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

      const dataToolsTarget = new cdk.CfnResource(
        this,
        "GatewayTargetDataTools",
        {
          type: "AWS::BedrockAgentCore::GatewayTarget",
          properties: {
            GatewayIdentifier: gateway.ref,
            Name: "data-tools",
            Description: "Market-data tools: yfinance / alpha_vantage",
            TargetConfiguration: {
              McpTargetConfiguration: {
                LambdaTargetConfiguration: {
                  LambdaArn: dataToolsFn.functionArn,
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
            TargetConfiguration: {
              McpTargetConfiguration: {
                LambdaTargetConfiguration: {
                  LambdaArn: memoryLogFn.functionArn,
                },
              },
            },
          },
        },
      );
      memoryLogTarget.addDependency(gateway);
    }

    // ------------------------------------------------------------------
    // Step Functions state machine
    // ------------------------------------------------------------------

    const buildErrorBranch = (
      stage: string,
    ): sfn.IChainable => {
      const notify = new tasks.LambdaInvoke(this, `NotifyError_${stage}`, {
        lambdaFunction: errorHandlerFn,
        payload: sfn.TaskInput.fromObject({
          stage,
          "run_id.$": "$$.Execution.Input.run_id",
          "trade_date.$": "$$.Execution.Input.trade_date",
          "ticker.$": "$.ticker",
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

    const invokeAgentTask = new tasks.LambdaInvoke(this, "InvokeAgentTask", {
      lambdaFunction: invokeAgentFn,
      payload: sfn.TaskInput.fromObject({
        "run_id.$": "$.run_id",
        "trade_date.$": "$.trade_date",
        "deep_model.$": "$.deep_model",
        "quick_model.$": "$.quick_model",
        "ticker.$": "$.ticker",
      }),
      outputPath: "$.Payload",
    });
    invokeAgentTask.addRetry({
      errors: [
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "States.TaskFailed",
      ],
      interval: cdk.Duration.seconds(10),
      maxAttempts: 2,
      backoffRate: 2,
    });

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
      resultPath: "$.results",
    });
    tickerMap.itemProcessor(invokeAgentTask);
    tickerMap.addCatch(buildErrorBranch("invoke_agent_map"), {
      resultPath: "$.error",
    });

    const aggregateTask = new tasks.LambdaInvoke(this, "AggregateTask", {
      lambdaFunction: aggregateFn,
      payload: sfn.TaskInput.fromObject({
        "run_id.$": "$.config.run_id",
        "trade_date.$": "$.config.trade_date",
        "results.$": "$.results",
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
    new cdk.CfnOutput(this, "BuildArtifactsBucketOut", {
      value: buildArtifactsBucket.bucketName,
    });
    new cdk.CfnOutput(this, "MemoryTableOut", { value: memoryTable.tableName });
    new cdk.CfnOutput(this, "NotificationsTopicOut", {
      value: notificationsTopic.topicArn,
    });
    if (agentRuntime) {
      new cdk.CfnOutput(this, "AgentCoreRuntimeArnOut", {
        value: agentRuntime.ref,
      });
    }
    if (gateway) {
      new cdk.CfnOutput(this, "GatewayUrlOut", {
        value: cdk.Fn.getAtt(gateway.logicalId, "GatewayUrl").toString(),
      });
    }
  }
}
