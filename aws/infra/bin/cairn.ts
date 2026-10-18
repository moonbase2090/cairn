import { App } from "aws-cdk-lib";
import { CairnVaultStack, parseStackConfig } from "../lib/vault-stack.js";

const app = new App();
const config = parseStackConfig({
  vaultId: app.node.tryGetContext("vaultId"),
  vaultName: app.node.tryGetContext("vaultName"),
  dimensions: app.node.tryGetContext("dimensions"),
  region: app.node.tryGetContext("region"),
  embedModel: app.node.tryGetContext("embedModel"),
  enableSyncEndpoint: app.node.tryGetContext("enableSyncEndpoint"),
});

new CairnVaultStack(app, config.stackName, {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region },
  vaultId: config.vaultId,
  vaultName: config.vaultName,
  dimensions: config.dimensions,
  embedModel: config.embedModel,
  enableSyncEndpoint: config.enableSyncEndpoint,
});
