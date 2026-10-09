# Cairn AWS live experiment (2026-10-08)

Status: completed and torn down. Throwaway vault `cairn-live-exp-20261008` in account `904233124492` (`us-west-2`), tagged `project=cairn`, `purpose=live-experiment`, `owner=mb2090`.

## Deploy

| Item | Value |
| --- | --- |
| Git base | `develop` at merge of PR #61 (`feat/aws-storage-20261004`) |
| Stack | `cairn-vault-3cf6ee788ef4595b0e9bc4e00ace36fe-3cf6ee78` |
| Logical vault ID | `3cf6ee788ef4595b0e9bc4e00ace36fe` |
| CDK context | `embedModel=hash`, `dimensions=8`, `enableSyncEndpoint=false` (sync enabled only for the sync test window) |
| Deploy started (CloudFormation) | 2026-10-08T20:49:48Z |
| Resources | Per-vault DynamoDB (memory + embedding cache), KMS CMK, S3 content bucket, S3 Vectors bucket/index (dim 8), scheduled cleanup Lambda (container), CloudWatch alarm |

**Note on dimensions:** The stack was deployed with `dimensions=8` to keep vector storage small for a same-day throwaway run. Production-style hash vaults should use the embedder’s real width (384 for `hash-v2`). Clients must match the stack dimension or cache rehydration fails.

## What worked

- Opt-in `cairn[aws]` direct storage against the live stack: insert, get, find, FTS (local SQLite cache), kNN, documents, status changes, delete.
- Shared backend contract suite against live AWS with sync HTTP **off**: 49 passed (see fix PR #66 Proof).
- Temporary sync endpoint (token auth): two local vault clients exchanged events; correction/archive propagation; revoked and wrong-vault tokens returned HTTP 403; API removed immediately after the run.
- Cleanup Lambda: tombstone sweep and orphan S3 version removal exercised successfully during the experiment (see PR #66 Proof).

## What broke (bugs → PR #66)

Live testing surfaced races and permission gaps that mocks did not cover. Fix PR: https://github.com/moonbase2090/cairn/pull/66 (`fix/s3vectors-kms-service-access`, tip `64c3e4a` at experiment close).

| Area | Symptom live | Fix summary |
| --- | --- | --- |
| S3 Vectors + KMS | Index could not use the vault CMK | Key policy grants `s3vectors.amazonaws.com` access scoped to the index ARN |
| Cleanup Lambda | Orphan content versions and tombstones lingered | Version-accurate deletes, tombstone table scan permission, serialized cleanup with lease fencing |
| SQLite cache on Lambda | `enable_load_extension` missing in Lambda Python | Fallback when extension loading is unavailable |
| Content cleanup | Concurrent writers could race | Serialized cleanup, lease expiry checks |

CI on #66: build + scorecard green; semver check red intentionally (no version bump in a fix PR).

## Performance (200 synthetic memories, hash offline embed)

Measured after reboot on 2026-10-09 with `CAIRN_EMBEDD=0`. AWS rows used `hash-v2` at **8** dimensions to match the deployed index. VectorVault used Bedrock Titan embeddings (`vv --agent-id cairn-1`, task `cairn-live-exp`, archived after the run).

| Backend | Store p50 | Store p95 | Retrieve p50 | Retrieve p95 |
| --- | ---: | ---: | ---: | ---: |
| Cairn AWS (direct) | 3395 ms | 3929 ms | 1415 ms | 1524 ms |
| VectorVault | 2439 ms | 2704 ms | 2148 ms | 2455 ms |

AWS store latency is dominated by DynamoDB + S3 + S3 Vectors writes per memory. AWS retrieve is faster than VectorVault here because both use a local SQLite FTS/kNN cache while VectorVault still pays Bedrock embed latency on every query.

## Cost

| Metric | Value |
| --- | --- |
| Cost Explorer (`project=cairn` tag, 2026-10-08) | **$0.00** (estimated, billing lag) |
| Experiment spend cap | &lt; $5 (not exceeded) |
| Estimated idle monthly (one vault, no traffic) | ~$3–8: KMS CMK (~$1), DynamoDB on-demand storage for metadata, S3 + S3 Vectors minimums, daily cleanup Lambda invocations, log storage |

## Teardown

1. `cdk destroy` on stack `cairn-vault-3cf6ee788ef4595b0e9bc4e00ace36fe-3cf6ee78` (same CDK context as deploy).
2. **RETAIN cleanup:** the stack uses `RemovalPolicy.RETAIN` on data stores. After destroy, manually deleted the retained DynamoDB tables, emptied versioned S3 content (1,268 object versions), removed the content bucket, and deleted Lambda log groups. S3 Vectors bucket/index were already gone after destroy.
3. **Verification (2026-10-09):** no CloudFormation stack remains; no DynamoDB tables, buckets, Lambdas, or API Gateway resources for this vault. Four KMS CMKs are **scheduled for deletion** (7-day window) — the only tagged `live-experiment` resources left in Cost Explorer.

## Recommendation

Ship the AWS backend behind the existing opt-in flag after **#66** merges. Before the next live run:

1. Deploy with **`dimensions=384`** (or the chosen embedder width) and document the requirement in setup UX.
2. Keep sync endpoint off by default; enable only for bounded sync tests.
3. Run the shared contract suite + a short perf smoke on every infra change; keep cleanup and KMS integration tests in CI (already added in #66).
