import {
  ArnFormat,
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps,
  Tags,
} from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigwv2Authorizers from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as apigwv2Integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda_ from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3vectors from "aws-cdk-lib/aws-s3vectors";
import type { Construct } from "constructs";
import { join } from "node:path";

export type VaultId = string & { readonly __brand: "VaultId" };
export type AwsRegion = string & { readonly __brand: "AwsRegion" };

export interface CairnStackConfig {
  readonly stackName: string;
  readonly vaultId: VaultId;
  readonly vaultName: string;
  readonly dimensions: number;
  readonly region: AwsRegion;
  readonly embedModel: string;
  readonly enableSyncEndpoint: boolean;
}

export interface CairnVaultStackProps extends StackProps {
  readonly vaultId: VaultId;
  readonly vaultName: string;
  readonly dimensions: number;
  readonly embedModel: string;
  readonly enableSyncEndpoint?: boolean;
}

const VECTOR_DIMENSION_LIMIT = 4096;

function parseNonEmptyString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${field} must be a non-empty string`);
  }
  return value.trim();
}

function parseVaultId(value: unknown): VaultId {
  const id = parseNonEmptyString(value, "vaultId").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(id)) {
    throw new Error("vaultId must be the 32-character Cairn logical vault ID");
  }
  return id as VaultId;
}

function parseRegion(value: unknown): AwsRegion {
  const region = parseNonEmptyString(value, "region");
  if (!/^[a-z]{2}(?:-[a-z]+)+-\d$/.test(region)) {
    throw new Error("region must be an AWS region name, such as us-west-2");
  }
  return region as AwsRegion;
}

function parseDimensions(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > VECTOR_DIMENSION_LIMIT) {
    throw new Error(`dimensions must be an integer from 1 through ${VECTOR_DIMENSION_LIMIT}`);
  }
  return parsed;
}

function parseBoolean(value: unknown, field: string): boolean {
  if (value === undefined || value === false || value === "false") return false;
  if (value === true || value === "true") return true;
  throw new Error(`${field} must be true or false`);
}

function resourceSlug(vaultId: VaultId): string {
  return `cairn-vault-${vaultId}`;
}

export function parseStackConfig(value: {
  readonly vaultId: unknown;
  readonly vaultName: unknown;
  readonly dimensions: unknown;
  readonly region: unknown;
  readonly embedModel?: unknown;
  readonly enableSyncEndpoint?: unknown;
}): CairnStackConfig {
  const vaultId = parseVaultId(value.vaultId);
  const vaultName = parseNonEmptyString(value.vaultName, "vaultName");
  const dimensions = parseDimensions(value.dimensions);
  const region = parseRegion(value.region);
  const embedModel = parseNonEmptyString(value.embedModel ?? "hash", "embedModel");
  if (embedModel.length > 256 || /[\u0000-\u001f\u007f]/.test(embedModel)) {
    throw new Error("embedModel must be at most 256 printable characters");
  }
  const enableSyncEndpoint = parseBoolean(value.enableSyncEndpoint, "enableSyncEndpoint");
  return {
    stackName: `${resourceSlug(vaultId)}-${vaultId.slice(0, 8)}`,
    vaultId,
    vaultName,
    dimensions,
    region,
    embedModel,
    enableSyncEndpoint,
  };
}

export class CairnVaultStack extends Stack {
  constructor(scope: Construct, id: string, props: CairnVaultStackProps) {
    super(scope, id, props);

    const slug = resourceSlug(props.vaultId);
    const vectorBucketName = `${slug}-vectors`;
    const vectorIndexName = `${slug}-index`;
    const vectorBucketArn = this.formatArn({
      service: "s3vectors",
      resource: "bucket",
      resourceName: vectorBucketName,
      arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
    });
    const vectorIndexArn = this.formatArn({
      service: "s3vectors",
      resource: "bucket",
      resourceName: `${vectorBucketName}/index/${vectorIndexName}`,
      arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
    });
    const vaultPartition = `VAULT#${props.vaultId}`;
    const vaultPrefix = `${props.vaultId}/`;
    const key = new kms.Key(this, "VaultKey", {
      alias: `alias/${slug}`,
      description: `Encryption key for Cairn vault ${props.vaultId}`,
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const memories = new dynamodb.Table(this, "VaultTable", {
      tableName: `${slug}-memory`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: key,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: "ttlEpoch",
      removalPolicy: RemovalPolicy.RETAIN,
    });
    for (const index of [
      { name: "ByVault", partitionKey: "GSI0PK", sortKey: "GSI0SK" },
      { name: "ByCanonical", partitionKey: "GSI1PK", sortKey: "GSI1SK" },
      { name: "ByTask", partitionKey: "GSI2PK", sortKey: "GSI2SK" },
      { name: "ByContentHash", partitionKey: "GSI3PK", sortKey: "GSI3SK" },
      { name: "ByTokenId", partitionKey: "GSI4PK", sortKey: "GSI4SK" },
      { name: "ByEventId", partitionKey: "GSI5PK", sortKey: "GSI5SK" },
    ]) {
      memories.addGlobalSecondaryIndex({
        indexName: index.name,
        partitionKey: { name: index.partitionKey, type: dynamodb.AttributeType.STRING },
        sortKey: { name: index.sortKey, type: dynamodb.AttributeType.STRING },
        projectionType: dynamodb.ProjectionType.ALL,
      });
    }

    const embeddings = new dynamodb.Table(this, "EmbeddingCache", {
      tableName: `${slug}-embedding-cache`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: key,
      timeToLiveAttribute: "ttlEpoch",
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const content = new s3.Bucket(this, "ContentBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: key,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      versioned: true,
      lifecycleRules: [{ abortIncompleteMultipartUploadAfter: Duration.days(7) }],
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const vectorBucket = new s3vectors.CfnVectorBucket(this, "VectorBucket", {
      vectorBucketName,
      encryptionConfiguration: {
        sseType: "aws:kms",
        kmsKeyArn: key.keyArn,
      },
      tags: [
        { key: "cairn:vault-id", value: props.vaultId },
        { key: "cairn:vault-name", value: props.vaultName.slice(0, 128) },
      ],
    });
    key.addToResourcePolicy(new iam.PolicyStatement({
      sid: "AllowS3VectorsIndexMaintenance",
      principals: [new iam.ServicePrincipal("indexing.s3vectors.amazonaws.com")],
      actions: ["kms:Decrypt"],
      resources: ["*"],
      conditions: {
        ArnEquals: {
          "aws:SourceArn": [vectorBucketArn, vectorIndexArn],
        },
        StringEquals: { "aws:SourceAccount": this.account },
        "ForAnyValue:StringEquals": {
          "kms:EncryptionContextKeys": ["aws:s3vectors:arn", "aws:s3vectors:resource-id"],
        },
      },
    }));
    const vectorIndex = new s3vectors.CfnIndex(this, "VectorIndex", {
      vectorBucketArn: vectorBucket.attrVectorBucketArn,
      indexName: vectorIndexName,
      dataType: "float32",
      dimension: props.dimensions,
      distanceMetric: "cosine",
      metadataConfiguration: { nonFilterableMetadataKeys: ["content_summary"] },
    });

    const cleanup = new lambda_.Function(this, "CleanupFunction", {
      functionName: `${slug}-cleanup`,
      runtime: lambda_.Runtime.PYTHON_3_13,
      handler: "cleanup.handler",
      code: lambda_.Code.fromAsset(join(__dirname, "../lambda")),
      timeout: Duration.minutes(15),
      environment: {
        MEMORY_TABLE: memories.tableName,
        CONTENT_BUCKET: content.bucketName,
        VECTOR_BUCKET: vectorBucket.vectorBucketName ?? `${slug}-vectors`,
        VECTOR_INDEX: vectorIndex.indexName ?? `${slug}-index`,
        VAULT_ID: props.vaultId,
      },
    });
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      sid: "CairnCleanupKmsDecrypt",
      actions: ["kms:Decrypt"],
      resources: [key.keyArn],
      conditions: {
        StringEquals: {
          "kms:ViaService": `dynamodb.${this.region}.amazonaws.com`,
          "kms:EncryptionContext:aws:dynamodb:tableName": memories.tableName,
          "kms:EncryptionContext:aws:dynamodb:subscriberId": this.account,
        },
      },
    }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      sid: "CairnCleanupTombstones",
      actions: ["dynamodb:Query", "dynamodb:UpdateItem"],
      resources: [memories.tableArn, `${memories.tableArn}/index/ByVault`],
      conditions: {
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": [
            `${vaultPartition}#TOMBSTONE`, `${vaultPartition}#MEMORY#*`,
          ],
        },
      },
    }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      sid: "CairnCleanupLease",
      actions: ["dynamodb:DeleteItem", "dynamodb:UpdateItem"],
      resources: [memories.tableArn],
      conditions: {
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": [
            `${vaultPartition}#CLEANUP#LOCK`,
            `${vaultPartition}#CONTENT#LOCK#*`,
          ],
        },
      },
    }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      sid: "CairnCleanupContentObjects",
      actions: ["s3:DeleteObjectVersion"],
      resources: [content.arnForObjects(`${vaultPrefix}*`)],
    }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      sid: "CairnCleanupListContentVersions",
      actions: ["s3:ListBucketVersions"],
      resources: [content.bucketArn],
      conditions: {
        StringLike: { "s3:prefix": [`${vaultPrefix}*`] },
      },
    }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      sid: "CairnCleanupVectors",
      actions: ["s3vectors:DeleteVectors"],
      resources: [vectorIndex.attrIndexArn],
    }));
    const cleanupSchedule = new events.Rule(this, "CleanupSchedule", {
      schedule: events.Schedule.rate(Duration.days(1)),
    });
    cleanupSchedule.addTarget(new targets.LambdaFunction(cleanup));
    const cleanupErrors = new cloudwatch.Alarm(this, "CleanupErrors", {
      metric: cleanup.metricErrors(),
      threshold: 1,
      evaluationPeriods: 1,
    });

    Tags.of(this).add("cairn:vault-id", props.vaultId);
    Tags.of(this).add("cairn:vault-name", props.vaultName.slice(0, 128));
    Tags.of(this).add("cairn:managed", "true");

    const directAccessPolicy = new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          sid: "CairnVaultMetadata",
          actions: [
            "dynamodb:BatchGetItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:DeleteItem",
            "dynamodb:DescribeTable",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:Query",
            "dynamodb:TransactWriteItems",
            "dynamodb:UpdateItem",
          ],
          resources: [memories.tableArn, `${memories.tableArn}/index/*`],
          conditions: {
            "ForAllValues:StringLike": {
              "dynamodb:LeadingKeys": [`${vaultPartition}*`],
            },
          },
        }),
        new iam.PolicyStatement({
          sid: "CairnEmbeddingCache",
          actions: ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
          resources: [embeddings.tableArn],
          conditions: {
            "ForAllValues:StringLike": {
              "dynamodb:LeadingKeys": [`${vaultPartition}*`],
            },
          },
        }),
        new iam.PolicyStatement({
          sid: "CairnContentObjects",
          actions: [
            "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
          ],
          resources: [content.arnForObjects(`${vaultPrefix}*`)],
        }),
        new iam.PolicyStatement({
          sid: "CairnContentPrefixList",
          actions: ["s3:ListBucket"],
          resources: [content.bucketArn],
          conditions: {
            StringLike: { "s3:prefix": [`${vaultPrefix}*`] },
          },
        }),
        new iam.PolicyStatement({
          sid: "CairnContentVersionList",
          actions: ["s3:ListBucketVersions"],
          resources: [content.bucketArn],
          conditions: {
            StringLike: { "s3:prefix": [`${vaultPrefix}*`] },
          },
        }),
        new iam.PolicyStatement({
          sid: "CairnVectorIndex",
          actions: [
            "s3vectors:DeleteVectors",
            "s3vectors:GetVectors",
            "s3vectors:PutVectors",
            "s3vectors:QueryVectors",
          ],
          resources: [vectorIndex.attrIndexArn],
        }),
        new iam.PolicyStatement({
          sid: "CairnVaultKeyUse",
          actions: ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey"],
          resources: [key.keyArn],
        }),
      ],
    });

    if (props.enableSyncEndpoint === true) {
      const authorizerRole = new iam.Role(this, "SyncAuthorizerRole", {
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      });
      authorizerRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName(
        "service-role/AWSLambdaBasicExecutionRole",
      ));
      const authorizer = new lambda_.Function(this, "SyncTokenAuthorizer", {
        functionName: `${slug}-sync-authorizer`,
        runtime: lambda_.Runtime.PYTHON_3_13,
        handler: "authorizer.handler",
        code: lambda_.Code.fromAsset(join(__dirname, "../lambda")),
        timeout: Duration.seconds(5),
        memorySize: 256,
        role: authorizerRole,
        environment: {
          MEMORY_TABLE: memories.tableName,
          VAULT_ID: props.vaultId,
        },
      });
      authorizer.addToRolePolicy(new iam.PolicyStatement({
        sid: "CairnTokenMembershipLookup",
        actions: ["dynamodb:GetItem"],
        resources: [memories.tableArn],
        conditions: {
          "ForAllValues:StringLike": {
            "dynamodb:LeadingKeys": [`${vaultPartition}#TOKEN#*`],
          },
        },
      }));
      authorizer.addToRolePolicy(new iam.PolicyStatement({
        sid: "CairnTokenTableKeyDecrypt",
        actions: ["kms:Decrypt", "kms:DescribeKey"],
        resources: [key.keyArn],
      }));

      const syncRole = new iam.Role(this, "SyncApiRole", {
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      });
      syncRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName(
        "service-role/AWSLambdaBasicExecutionRole",
      ));
      const syncFunction = new lambda_.DockerImageFunction(this, "SyncApiFunction", {
        functionName: `${slug}-sync-api`,
        code: lambda_.DockerImageCode.fromImageAsset(join(__dirname, "../../.."), {
          file: "aws/infra/lambda/Dockerfile",
          exclude: [
            ".git", ".sc", ".coverage", "analyzer.toml", "tests", "aws/infra/cdk.out", "**/node_modules",
            "**/.venv", "**/__pycache__", "**/*.pyc", "dist", "build",
          ],
          platform: ecrAssets.Platform.LINUX_ARM64,
        }),
        architecture: lambda_.Architecture.ARM_64,
        timeout: Duration.seconds(29),
        memorySize: 2048,
        role: syncRole,
        environment: {
          MEMORY_TABLE: memories.tableName,
          CACHE_TABLE: embeddings.tableName,
          CONTENT_BUCKET: content.bucketName,
          VECTOR_BUCKET: vectorBucket.vectorBucketName ?? `${slug}-vectors`,
          VECTOR_INDEX: vectorIndex.indexName ?? `${slug}-index`,
          VECTOR_INDEX_ARN: vectorIndex.attrIndexArn,
          VAULT_ID: props.vaultId,
          VAULT_NAME: props.vaultName,
          EMBED_DIMS: String(props.dimensions),
          EMBED_MODEL: props.embedModel,
          SYNC_ORIGIN_ID: `aws-${this.region}-${props.vaultId}`,
        },
      });
      new iam.Policy(this, "SyncApiDataPolicy", {
        document: directAccessPolicy,
      }).attachToRole(syncRole);

      const authorizerAdapter = new apigwv2Authorizers.HttpLambdaAuthorizer(
        "SyncTokenAuthorizerAdapter", authorizer, {
          identitySource: ["$request.header.Authorization"],
          responseTypes: [apigwv2Authorizers.HttpLambdaResponseType.SIMPLE],
          resultsCacheTtl: Duration.seconds(0),
        },
      );
      const httpApi = new apigwv2.HttpApi(this, "SyncHttpApi", {
        apiName: `${slug}-sync`,
        defaultAuthorizer: authorizerAdapter,
      });
      const syncIntegration = new apigwv2Integrations.HttpLambdaIntegration(
        "SyncApiIntegration", syncFunction, { timeout: Duration.seconds(29) },
      );
      httpApi.addRoutes({
        path: "/health", methods: [apigwv2.HttpMethod.GET], integration: syncIntegration,
      });
      httpApi.addRoutes({
        path: "/push", methods: [apigwv2.HttpMethod.POST], integration: syncIntegration,
      });
      httpApi.addRoutes({
        path: "/pull", methods: [apigwv2.HttpMethod.GET], integration: syncIntegration,
      });
      httpApi.addRoutes({
        path: "/search", methods: [apigwv2.HttpMethod.POST], integration: syncIntegration,
      });
      new CfnOutput(this, "SyncApiUrl", {
        value: httpApi.apiEndpoint,
        description: "Public HTTPS sync API; every route requires an active per-agent Cairn token.",
      });
    }

    new CfnOutput(this, "VaultId", { value: props.vaultId });
    new CfnOutput(this, "VaultName", { value: props.vaultName });
    new CfnOutput(this, "MemoryTableName", { value: memories.tableName });
    new CfnOutput(this, "EmbeddingCacheTableName", { value: embeddings.tableName });
    new CfnOutput(this, "ContentBucketName", { value: content.bucketName });
    new CfnOutput(this, "ContentPrefix", { value: vaultPrefix });
    new CfnOutput(this, "VectorBucketName", { value: vectorBucket.vectorBucketName ?? `${slug}-vectors` });
    new CfnOutput(this, "VectorIndexName", { value: vectorIndex.indexName ?? `${slug}-index` });
    new CfnOutput(this, "VectorIndexArn", { value: vectorIndex.attrIndexArn });
    new CfnOutput(this, "EncryptionKeyArn", { value: key.keyArn });
    new CfnOutput(this, "CleanupFunctionArn", { value: cleanup.functionArn });
    new CfnOutput(this, "CleanupErrorAlarmArn", { value: cleanupErrors.alarmArn });
    new CfnOutput(this, "DirectAccessPolicy", {
      value: Stack.of(this).toJsonString(directAccessPolicy.toJSON()),
      description: "Attach only to identities that should access this one vault.",
    });
  }
}
