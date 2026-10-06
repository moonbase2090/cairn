## Pull requests

All PRs follow REVIEW_POLICY.md (leaf vs trunk, proof in every PR, independent review, feature gating).

## Tests

No tautological tests. A test must fail when the behavior it covers breaks. Don't write tests that:

- assert a value against itself, or a constant against the same constant
- compute the expected value with the code under test or a copy of its logic
- mock or stub the unit under test, then assert what the mock returns
- only check that something ran, was called, or didn't raise, with no assertion on the result
- compare against a snapshot or golden file regenerated from current output without review
- still pass when the implementation is deleted or replaced with a stub or default
