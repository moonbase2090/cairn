# Cloud backends

Status: proposal. This document defines one optional cloud interface with three provider adapters. AWS is the first build target. Google Cloud and Azure are design targets for later builds. The [sync update design](sync-updates.md) defines the shared event format and conflict rule. The [AWS backend design](s3-dynamodb-backend.md) maps that contract onto AWS resources.

Use AWS CDK in TypeScript for AWS and Pulumi in TypeScript for Google Cloud and Azure. Do not use Terraform or Bicep.

## Keep SQLite as the default

Cairn continues to use SQLite unless a user selects a cloud backend. The base install has no cloud SDK dependencies. Each adapter is an optional extra such as `cairn[aws]`, `cairn[gcp]`, or `cairn[azure]`.

Use one cloud-provider control-plane interface for credential checks, tool checks, plan, apply, status, and teardown. Each provider's data-plane adapter implements the existing `StorageBackend` contract. A sync transport exposes `push`, `pull`, and `health`. Keep those interfaces separate so direct cloud storage does not require an HTTP sync server.

The provider adapters share one sync rule. Every state change has a separate `state_revision`, `updated_at`, `origin_id`, and `event_id`. Receivers keep the greatest tuple in that order. Each adapter commits a memory change and its durable event together, deduplicates by event ID, and returns an opaque cursor. The provider controls the cursor encoding. A client never depends on an AWS, Google, or Azure sequence format. Preserve every multi-row Cairn operation as one change set and one transaction when provider limits allow it.

## Build AWS first

The AWS adapter follows the existing VectorVault layout: S3 for content, S3 Vectors for semantic search, DynamoDB for memory metadata and an embedding cache, and Lambda for the optional sync endpoint. Cairn's DynamoDB row is authoritative for status and revision. S3 Vectors is a search projection. This differs from the reference layout because sync needs a conditional metadata write and its event record to commit as one operation.

Use a DynamoDB condition expression to compare the incoming revision tuple with the stored tuple. Put the row update and change event in one transaction. Use a separate durable feed with per-writer cursors. Route changes for the same key to the same feed shard. Use DynamoDB's base-table consistent reads for conflict decisions because secondary indexes are eventually consistent. See [DynamoDB read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html).

Deploy the AWS memory and monitoring stacks with AWS CDK in TypeScript. Keep the sync endpoint optional. Clients with AWS credentials can use the shared data store directly. Clients without direct AWS access can use the Lambda endpoint through API Gateway with per-agent token authorization. The detailed resource map, S3 Vectors limits, Lambda limits, caching, migration, and pricing are in [the AWS backend design](s3-dynamodb-backend.md).

AWS setup uses the selected AWS CLI profile or the normal credential chain. The [AWS SDK credential guide](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html) describes profile, environment, web identity, and role credentials. The [DynamoDB conditional-write guide](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/BestPractices_OptimisticLocking.html) documents conditional updates. See the [AWS CDK TypeScript guide](https://docs.aws.amazon.com/cdk/v2/guide/work-with-cdk-typescript.html) for the supported IaC language.

## Design Google Cloud for the same data contract

Store full memory content in Cloud Storage. Store memory rows, ownership, revisions, token digests, and change events in Firestore. Use a Firestore transaction to read the current row, compare the sync tuple, write the winner, and append the event. Store each content object under a content-hash key. Write the object with a generation precondition so a retry cannot replace different content. Cloud Storage provides strong object read-after-write and read-after-delete consistency. Firestore and Cloud Storage do not share a transaction. Write immutable content first, commit metadata second, and remove unreferenced objects after metadata confirms they are unused. See [Cloud Storage consistency](https://cloud.google.com/storage/docs/consistency) and [request preconditions](https://cloud.google.com/storage/docs/request-preconditions).

Firestore transactions retry when a read document changes. They allow a 10 MiB request and run for up to 270 seconds. Firestore also limits a single transaction's field transformations on one document to 500. Keep sync pages within the documented limits. See [Firestore transactions](https://cloud.google.com/firestore/native/docs/manage-data/transactions) and [Firestore quotas](https://cloud.google.com/firestore/quotas).

Use Firestore native vector search for the first GCP search adapter. It keeps metadata and vectors in one document store. Standard edition returns at most 1,000 nearest-neighbor results and supports vectors up to 2,048 dimensions. Firestore does not create embeddings. Cairn supplies the same embedder it uses locally. See [Firestore vector search](https://cloud.google.com/firestore/native/docs/vector-search).

Use [Vertex AI Vector Search](https://cloud.google.com/vertex-ai/docs/vector-search/overview) when a workload needs a separate managed index or exceeds Firestore's documented search limits. It supports streaming index updates, but adds deployed index nodes and separate build and update charges. Check the [Vertex AI quotas](https://cloud.google.com/vertex-ai/docs/quotas) and [Vertex AI pricing](https://cloud.google.com/vertex-ai/pricing) during setup.

The quota page lists 5 concurrent index creations and 5 concurrent index updates. It allows 100 indexes and 50 deployed index nodes per project and region. It also allows 6,000 streaming update requests or 120,000 KB of streaming update throughput per minute. Check the live quota page before deployment because quotas can change.

Run the sync endpoint on Cloud Run. It serves `push`, `pull`, `health`, and search with per-agent token checks. Cloud Run allows request timeouts up to 60 minutes and up to 32 GiB per instance. HTTP/1 requests and responses can be up to 32 MiB. Keep pack pages below that limit and use continuation cursors. See [Cloud Run quotas and limits](https://cloud.google.com/run/quotas).

Cloud Run functions can host a smaller endpoint. Request and response limits differ by generation. Check the selected runtime's [function quotas](https://cloud.google.com/functions/quotas).

Use Pulumi in TypeScript for GCP infrastructure. The [Pulumi TypeScript guide](https://www.pulumi.com/docs/iac/languages-sdks/javascript/) documents that runtime. Install the Google Cloud CLI from Google's supported package or archive. Verify the archive against Google's published SHA-256 checksum before use. The [Google Cloud CLI install guide](https://cloud.google.com/sdk/docs/install) lists the supported packages and checksums.

Firestore bills for reads, writes, index reads, storage, and network use. Cloud Storage bills for storage, operations, and transfers. Cloud Run charges by request and allocated CPU and memory. Vertex AI adds index serving and build or update charges. Link these current prices in each plan:

- [Firestore pricing](https://cloud.google.com/firestore/pricing)
- [Cloud Storage pricing](https://cloud.google.com/storage/pricing)
- [Cloud Run pricing](https://cloud.google.com/run/pricing)
- [Vertex AI pricing](https://cloud.google.com/vertex-ai/pricing)

## Design Azure for the same data contract

Store full memory content in Blob Storage. Store memory metadata, ownership, revisions, token digests, and events in Cosmos DB for NoSQL. Put the memory row and its event in the same logical partition and use a transactional batch. Use `_etag` with `If-Match` to reject a stale replacement, then compare Cairn's sync tuple before retrying. Blob Storage uses strong consistency after successful writes. Store immutable content objects before the metadata transaction and use the Blob ETag as a conditional-write guard. Blob Storage and Cosmos DB do not share a transaction, so clean up unreferenced objects after metadata commits. See [Cosmos DB optimistic concurrency](https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency) and [Blob Storage concurrency](https://learn.microsoft.com/en-us/azure/storage/blobs/concurrency-manage).

A Cosmos transactional batch is limited to 100 operations, a 2 MB request, and 5 seconds. Each logical partition is limited to 20 GB. Keep a canonical memory group and its change events together only while that size limit fits. Validate the partition key before deployment. See [Cosmos DB transactional batch limits](https://learn.microsoft.com/en-us/azure/cosmos-db/transactional-batch) and [Cosmos DB service limits](https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits).

Cosmos DB for NoSQL has built-in vector search. The `flat` index supports at most 505 dimensions. `quantizedFlat` and DiskANN support up to 4,096 dimensions. Those two index types need at least 1,000 vectors for indexed search; smaller sets use a full scan. DiskANN is approximate. Use the native index first when its dimension and recall behavior fit the configured Cairn embedder. Evaluate [Azure AI Search](https://learn.microsoft.com/en-us/azure/search/vector-search-overview) when Cairn needs its search features or a separately managed index. Azure AI Search supports up to 4,096 dimensions per vector field, with capacity and vector-index limits that vary by service tier. Compare the [Cosmos DB vector limits](https://learn.microsoft.com/en-us/azure/cosmos-db/vector-search) with the [Azure AI Search service limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity) in the plan.

Use Azure Functions for the sync endpoint. It serves `push`, `pull`, `health`, and search with per-agent token checks. The HTTP response path times out after 230 seconds, even when a function can keep running. Use bounded pages and return promptly. Flex Consumption offers 512 MB, 2,048 MB, or 4,096 MB instance sizes. Always-ready instances reduce cold starts but add a baseline charge. See [Functions scale and hosting](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale) and [HTTP trigger limits](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger). Flex Consumption costs are described in [its billing guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-consumption-costs).

Use Pulumi in TypeScript for Azure. Install or update `az` from Microsoft's supported platform package source. Verify signed repositories or package-manager checksums where the platform provides them. Install Pulumi from its official release and verify its published checksum. The [Azure CLI install guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) and [Pulumi release checksums](https://www.pulumi.com/docs/install/versions/) describe the supported sources.

Cosmos DB bills for request units, storage, and network use. Blob Storage bills for capacity, operations, and transfers. Functions Flex Consumption bills for executions and memory-time. Always-ready instances add a baseline charge. Azure AI Search has tier-specific capacity limits. Link these current prices in each plan:

- [Cosmos DB pricing](https://azure.microsoft.com/pricing/details/cosmos-db/)
- [Blob Storage pricing](https://azure.microsoft.com/pricing/details/storage/blobs/)
- [Functions pricing](https://azure.microsoft.com/pricing/details/functions/)
- [Azure AI Search pricing](https://azure.microsoft.com/pricing/details/search/)

## Keep the setup agent-first

Every sync-server and cloud operation is available through both the MCP server and the installed Cairn skill. Install and update that skill with `cairn skills install`. The CLI remains available for scripts and manual use, but the skill does not ask an agent to bypass MCP for normal operations.

Add these MCP tools:

| Tool | Actions |
|---|---|
| `cairn_sync` | `status`, `push`, `pull` |
| `cairn_serve` | `start`, `status`, `health`, `stop` |
| `cairn_token` | `create`, `list`, `revoke` |
| `cairn_cloud` | `setup`, `plan`, `apply`, `status`, `teardown` |
| `cairn_cloud_tools` | `status`, `install`, `update` for one selected provider |

Keep server start non-blocking. Return its address and process state. Keep existing export and import tools for portable packs. A sync tool uses the configured provider or asks only for a missing peer URL.

Add skill sections for sync status and deltas, server lifecycle, token ownership, cloud setup, tool installation and updates, plan review, deployment, teardown, and credential troubleshooting. The skill calls the matching MCP tool for each operation and tells the agent when a human must approve an install or deployment.

## Ask only for missing setup values

For `cairn_cloud setup <provider>`, inspect only the chosen provider. Check its command versions and active identity without changing the account. Reuse configured values. Derive the vault name from the Cairn project slug when one exists. Ask only for required values that cannot be derived. Never ask for a cloud secret.

| Provider | Check | Ask only when missing |
|---|---|---|
| AWS | Run `aws sts get-caller-identity` with the selected profile. Read its default region. | Profile if several are available, region, or vault name if Cairn has no project slug. |
| Google Cloud | Read the active account and project with `gcloud`. Check Application Default Credentials for Pulumi. | Project or deployment region. If credentials are missing, show `gcloud auth application-default login`. |
| Azure | Read the active account and subscription with `az account show`. | Subscription, region, or resource group when no existing group is selected. |

The plan tool returns a `plan_id`. The apply tool requires that ID and an explicit confirmation value. It rejects a plan whose inputs changed after preview.

Use the provider CLIs the user selected: `aws`, `gcloud`, or `az`. Run commands with an argument array and no shell. Allow only known commands and options. Do not log credential output or copy credentials into tool arguments. If an identity is missing or expired, show the provider's login command and wait for the user to finish it. Use the CLI's normal credential chain for CDK and Pulumi. If the official source has no verifiable signature or checksum for the host platform, do not install it automatically. Show the vendor's install instructions and let the user install it.

Install tools only for the selected provider. Reuse a compatible version already on the machine. If a required tool is missing or too old, show the source and verification step and ask before installing or updating it. Prefer a Cairn-managed user-level directory and do not use `sudo` by default. If the vendor's supported install path requires system access, stop and give the user its official instructions. Keep Node.js as a checked prerequisite for CDK and Pulumi TypeScript programs; ask before installing it.

| Provider | Tools installed on demand | Integrity check |
|---|---|---|
| AWS | AWS CLI and AWS CDK CLI | Verify the AWS CLI archive's PGP signature. Pin the CDK CLI in a lockfile and verify package integrity metadata. |
| Google Cloud | Google Cloud CLI and Pulumi CLI | Verify Google's published SHA-256 for the selected CLI archive and Pulumi's published release checksum. |
| Azure | Azure CLI and Pulumi CLI | Use Microsoft's supported signed package source or package manager checksum. Verify Pulumi's published release checksum. |

The [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) documents PGP verification. The [Google Cloud CLI install guide](https://cloud.google.com/sdk/docs/install) publishes archive checksums. The [Pulumi CLI versions page](https://www.pulumi.com/docs/install/versions/) publishes release checksums. Microsoft documents signed Azure CLI repositories and supported package managers in the [Azure CLI install guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli).

Expose `cairn cloud tools update <provider>` through both the CLI and `cairn_cloud_tools`. Update only the selected provider's tools. Pulumi is shared by GCP and Azure, so reuse an existing compatible Pulumi install instead of installing a second copy.

## Preview before creating resources

Setup first checks identity and tools, then collects only missing deployment inputs. It renders the infrastructure and runs a provider preview. The plan lists every resource, permissions, data-retention behavior, and billable service. Show a numeric cost estimate only when vault size and request assumptions support one. Otherwise show the pricing unit and current calculator link for each variable charge.

Installing or updating tools requires a separate user confirmation. Creating or changing billable cloud resources also requires explicit confirmation after the user reviews the plan. Bind `apply` to the exact plan that was reviewed. A non-interactive call may inspect tools, credentials, or render a plan. It must not install tools or apply a paid plan without explicit confirmation flags supplied after user approval.

Teardown previews the resources it will remove. Preserve memory data by default. Require a separate explicit confirmation before deleting a bucket, vector index, database, or backup. The plan must name the affected resources and the data that will be lost.

## Keep cloud dependencies and state optional

The local SQLite path imports no provider SDK, installs no provider CLI, and creates no cloud resource. A user who selects a cloud backend installs only that provider extra and the tools that provider needs. Cloud configuration stores resource identifiers and profile references. Credential material stays in the provider's credential store.

Provider CI later runs the shared `StorageBackend` contract and sync tests against the provider's native conditional-write behavior. It checks stale updates, concurrent updates, duplicate events, tombstones, cursor retry, content integrity, vector filters, and ownership restrictions. AWS ships first. GCP and Azure remain design-only until their adapters pass the same semantics.
