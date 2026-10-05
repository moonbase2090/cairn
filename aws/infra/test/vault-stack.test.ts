import assert from "node:assert/strict";
import test from "node:test";
import { App, assertions } from "aws-cdk-lib";
import { CairnVaultStack, parseStackConfig } from "../lib/vault-stack.js";

const vaultA = "0123456789abcdef0123456789abcdef";
const vaultB = "fedcba9876543210fedcba9876543210";

function synth(vaultId: string, enableSyncEndpoint = false) {
  const config = parseStackConfig({
    vaultId,
    vaultName: `vault-${vaultId.slice(0, 4)}`,
    dimensions: "384",
    region: "us-west-2",
    embedModel: "hash",
    enableSyncEndpoint,
  });
  const app = new App();
  const stack = new CairnVaultStack(app, config.stackName, {
    env: { account: "123456789012", region: config.region },
    vaultId: config.vaultId,
    vaultName: config.vaultName,
    dimensions: config.dimensions,
    embedModel: config.embedModel,
    enableSyncEndpoint: config.enableSyncEndpoint,
  });
  return assertions.Template.fromStack(stack);
}

test("creates isolated on-demand metadata, cache, content, and vector resources", () => {
  const template = synth(vaultA);

  template.resourceCountIs("AWS::DynamoDB::Table", 2);
  template.resourceCountIs("AWS::S3::Bucket", 1);
  template.resourceCountIs("AWS::S3Vectors::VectorBucket", 1);
  template.resourceCountIs("AWS::S3Vectors::Index", 1);
  template.resourceCountIs("AWS::Lambda::Function", 1);
  template.resourceCountIs("AWS::ApiGatewayV2::Api", 0);
  template.resourceCountIs("AWS::Events::Rule", 1);
  template.resourceCountIs("AWS::CloudWatch::Alarm", 1);
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    BillingMode: "PAY_PER_REQUEST",
    KeySchema: [
      { AttributeName: "PK", KeyType: "HASH" },
      { AttributeName: "SK", KeyType: "RANGE" },
    ],
  });
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    GlobalSecondaryIndexes: assertions.Match.arrayWith([
      assertions.Match.objectLike({ IndexName: "ByVault" }),
      assertions.Match.objectLike({ IndexName: "ByContentHash" }),
      assertions.Match.objectLike({ IndexName: "ByTokenId" }),
      assertions.Match.objectLike({ IndexName: "ByEventId" }),
    ]),
  });
  template.hasResourceProperties("AWS::S3Vectors::Index", {
    DataType: "float32",
    Dimension: 384,
    DistanceMetric: "cosine",
  });
  template.hasResourceProperties("AWS::S3::Bucket", {
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  });
});

test("rejects unsafe embed model context before synthesis", () => {
  assert.throws(() => parseStackConfig({
    vaultId: vaultA,
    vaultName: "test-vault",
    dimensions: 384,
    region: "us-west-2",
    embedModel: "local\nmalicious",
  }), /at most 256 printable characters/);
});

test("adds an authenticated sync API only when explicitly enabled", () => {
  const template = synth(vaultA, true);

  template.resourceCountIs("AWS::Lambda::Function", 3);
  template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
  template.resourceCountIs("AWS::ApiGatewayV2::Route", 4);
  template.hasResourceProperties("AWS::ApiGatewayV2::Authorizer", {
    AuthorizerType: "REQUEST",
    AuthorizerPayloadFormatVersion: "2.0",
    EnableSimpleResponses: true,
    IdentitySource: ["$request.header.Authorization"],
    AuthorizerResultTtlInSeconds: 0,
  });
  template.hasOutput("SyncApiUrl", {});

  const synthesized = JSON.stringify(template.toJSON());
  assert.match(synthesized, /CairnTokenMembershipLookup/);
  assert.match(synthesized, /dynamodb:GetItem/);
  assert.match(synthesized, /VAULT#0123456789abcdef0123456789abcdef#TOKEN#/);
  assert.match(synthesized, /CairnTokenTableKeyDecrypt/);
});

test("qualifies physical data resources and policy partition keys by vault ID", () => {
  const templateA = synth(vaultA).toJSON();
  const templateB = synth(vaultB).toJSON();
  const serializedA = JSON.stringify(templateA);
  const serializedB = JSON.stringify(templateB);

  assert.match(serializedA, new RegExp(vaultA));
  assert.match(serializedB, new RegExp(vaultB));
  assert.notEqual(serializedA, serializedB);
  assert.match(serializedA, /VAULT#0123456789abcdef0123456789abcdef/);
});

test("rejects malformed vault IDs and unsupported vector dimensions", () => {
  assert.throws(
    () => parseStackConfig({ vaultId: "../../other", vaultName: "team", dimensions: 384, region: "us-west-2" }),
    /32-character Cairn logical vault ID/,
  );
  assert.throws(
    () => parseStackConfig({ vaultId: vaultA, vaultName: "team", dimensions: 4097, region: "us-west-2" }),
    /dimensions must be an integer/,
  );
  assert.throws(
    () => parseStackConfig({ vaultId: vaultA, vaultName: "team", dimensions: 384,
      region: "us-west-2", enableSyncEndpoint: "yes" }),
    /enableSyncEndpoint must be true or false/,
  );
});
