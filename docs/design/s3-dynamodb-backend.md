# Optional AWS storage backend

Status: proposal. This design follows the optional cloud-provider contract in [cloud-backends.md](cloud-backends.md). SQLite remains the default, and Cairn imports no AWS package for SQLite users.

## Reuse the VectorVault layout

Build the first cloud adapter around the existing VectorVault design: a KMS key, an S3 content bucket, S3 Vectors indexes, DynamoDB tables for memory lookup and embedding cache, and a monitoring stack. Use DynamoDB on-demand capacity for the metadata and cache tables. Add a scheduled cleanup Lambda for expired cache and tombstoned content, and CloudWatch monitoring for endpoint and cleanup health. Use AWS CDK in TypeScript to deploy those resources. DynamoDB on-demand mode uses pay-per-request billing. See [DynamoDB on-demand capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html) and [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html).

Adapt one point for Cairn's sync rules. VectorVault treats S3 Vectors metadata as the status source. Cairn needs DynamoDB to hold the authoritative memory state and durable sync events because conditional row updates and event cursors must commit together. Treat S3 Vectors as a search projection. Keep its status metadata as a filter hint, then verify returned candidates against DynamoDB before Cairn returns them.

## Map Cairn's backend contract

| Cairn operation | AWS resource and behavior |
|---|---|
| `insert`, `get`, `by_hash`, and filtered `find` | Store a metadata item keyed by Cairn `key` in DynamoDB. Add sparse indexes for `canonical_id`, `task_id`, and the list paths that Cairn uses. Several memory rows may share a canonical ID because corrections add versions. |
| Content and `read_content` | Store full content in the S3 content bucket. Keep its object key and `content_hash` in DynamoDB. Return inline content below Cairn's configured document threshold and load S3 content for larger rows. Verify the hash on read. |
| Embedding and `knn` | Store the embedding and filter metadata in S3 Vectors. Use `query-vectors` for nearest-neighbor search, then verify candidate status, expiry, and authorization from the DynamoDB base table. |
| `set_status` and row updates | Use a DynamoDB conditional update on `state_revision`, `updated_at`, `origin_id`, and `event_id`. Write the row and change event in one `TransactWriteItems` call. |
| Delta feed and tombstones | Add a durable DynamoDB change table. Partition events by writer identity and shard, with a sequence within each partition. Store an opaque per-shard cursor. Route every event for one key to the same shard so its order stays stable. |
| `delete_by_keys` and `delete_by_canonical` | Write tombstones before deleting metadata or vector entries. Keep content until the tombstone retention rule in the sync design permits cleanup. |
| `count` and `count_by_status` | Maintain counters transactionally or compute them from paginated metadata queries. Do not use an eventually consistent secondary index as proof of a count. |
| `fts_search` | S3 Vectors does not provide Cairn's BM25 keyword search. Keep a complete local SQLite FTS5 index and update it from the change feed. This makes each client cache the full vault content and adds time to a cold bootstrap. If Cairn cannot accept that cache, add and price a separate search service before claiming backend-contract parity. |
| `transaction`, `reopen`, and documents | Use DynamoDB transactions for metadata and event writes. Keep S3 objects content-addressed so retries are safe. A reopened handle reuses the local vault configuration and AWS credential chain. |

DynamoDB is the source for agent ownership and sync order. Use strongly consistent reads from the base table for row revision checks. Global secondary indexes are eventually consistent, so they may serve discovery and candidate listing but must not decide whether a stale update wins. After the row and event commit, a retryable projection worker updates S3 Vectors. Search filters candidates through the authoritative row. A new or restored row can be missing from vector results until the projection catches up. `rebuild_vec` reconciles the projection from DynamoDB.

The embedding cache is optional. Key it by content hash and embedder name, and expire cache entries with DynamoDB TTL. Keep cache misses correct by recomputing the embedding. Do not treat the cache as memory data.

## Keep S3 Vectors limits in the schema

S3 Vectors supports vector dimensions from 1 through 4,096. An index fixes its dimension, distance metric, and non-filterable metadata keys when it is created. Vector metadata has a 40 KB total limit, and filterable metadata has a 2 KB limit. Set the Cairn embedder's dimension when setup creates the index. A dimension change needs a new index and a rebuild. Keep content out of vector metadata.

A query can request up to 10,000 nearest vectors, but each response page returns at most 100. Follow the continuation token until Cairn has enough candidates or the result set ends. See [S3 Vectors limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html).

DynamoDB items have a 400 KB size limit. S3 content objects avoid placing long memory text in a metadata item. DynamoDB transactions allow at most 100 actions and 4 MB per transaction, so sync pages must stay within those documented limits. Read the [S3 Vectors limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html) and [index configuration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-indexes.html). The [DynamoDB transaction limits](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html) define the write-page boundary.

## Offer direct storage and a sync endpoint

A client with AWS credentials can open the DynamoDB and S3 resources directly. Several clients can share one cloud vault this way. They do not need `cairn serve`; IAM grants replace the HTTP server's network boundary.

Keep a Lambda endpoint for clients that should not receive direct database and bucket permissions. Put the endpoint behind an API Gateway HTTP API and a Lambda authorizer. The API implements `push`, `pull`, `health`, and vector search. Store only a digest of each Cairn agent token. Apply the ownership checks in [sync-updates.md](sync-updates.md).

A Lambda Function URL is another deploy option. `AWS_IAM` requires SigV4 signing, which suits AWS-native clients. `NONE` makes the URL internet-accessible and leaves token enforcement to the function. Do not expose a Function URL with `NONE` unless the deployment shows that exposure and the function checks every request. Read [Function URL auth modes](https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html) and [Function URL invocation](https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html). API Gateway HTTP APIs are documented [here](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api.html).

Lambda has a 15-minute execution limit and 10,240 MB maximum memory. A buffered synchronous request and response has a 6 MB limit. ZIP packages have a 50 MB direct upload limit and a 250 MB uncompressed limit. Container images can be up to 10 GB. Paginate sync and search responses to stay under the request limit. Keep imports small to reduce initialization work. AWS documents that cold starts add initialization latency and that provisioned concurrency keeps environments ready at an additional charge. If cold-start latency proves too high, compare provisioned concurrency with the cost of accepting cold starts. Read the [Lambda cold-start guide](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html) and [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html). The [Lambda pricing model](https://aws.amazon.com/lambda/pricing/) lists the charges.

## Configure access without storing credentials

Add an optional extra such as `cairn[aws]` for the AWS SDK. Keep `boto3` out of core dependencies. Resolve credentials with the standard AWS SDK chain, including profiles, environment credentials, web identity, and workload roles. Do not copy access keys into `config.toml` or tool arguments. See the [Boto3 credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html).

A config contains resource names and a profile reference, not secret material:

```toml
[storage]
backend = "aws"
region = "us-west-2"
profile = "work"
vault = "team-memory"
```

Setup may fill in bucket, vector index, table, and endpoint names after it deploys them. Keep the local vault identity and audit log on disk as the `StorageBackend` contract requires.

## Cache reads and migrate from SQLite

Cache metadata, fetched content, and embeddings in the vault directory. Keep the cache keyed by cloud row revision. When offline, serve cached reads and mark them stale. Reject writes while offline in the first version rather than creating a local branch that the cloud cannot order. SQLite remains available as a separate default or migration source.

Migration copies every SQLite row, content document, and embedding, including non-active statuses. Verify row counts and content hashes before switching `[storage] backend`. Keep the SQLite vault unchanged for rollback. Do not delete it as part of setup or teardown.

## Show costs before deployment

Report each billable resource and its pricing unit. The stack uses KMS, S3, S3 Vectors, DynamoDB, Lambda, and API Gateway. Link these current prices in the plan:

- [S3 pricing](https://aws.amazon.com/s3/pricing/)
- [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)
- [KMS pricing](https://aws.amazon.com/kms/pricing/)
- [Lambda pricing](https://aws.amazon.com/lambda/pricing/)
- [API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/)

Estimate usage-based costs only when Cairn has vault size and request-rate assumptions. Label unknown usage as variable instead of inventing a monthly total.
