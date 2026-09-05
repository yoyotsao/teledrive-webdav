# Task 8 — Exact Deduplication Coverage and Batch-Local Claims

## Fix round: follow-up review findings

### Regression tests first (RED)

Before changing production code, expanded `tests/test_upload_dedup.py` with:

- a failed-claim test that observes `FingerprintClaims._claim` and waits until
  the follower has joined the owner's existing future before releasing the
  failing producer; and
- a deduplicated-registration test using a reused message on storage account
  `42`.

The deterministic claim assertion already passed against the existing future
implementation. The new routing regression failed as expected before the fix:

```text
tests/test_upload_dedup.py -q
10 passed, 1 failed
KeyError: 'telegram_user_id'
```

This proved the registration path discarded the canonical `UploadedPart`'s
account identity.

### Implementation (GREEN)

`gamestage.upload_and_register` now passes
`telegram_user_id=part.telegram_user_id` to every `api.register` call. That
includes deduplicated parts, so a reused secondary-account Telegram message is
registered with the account required to route later reads.

The failure-path claim test now releases the owner only after its follower has
claimed the existing future. It therefore verifies the intended propagation
instead of depending on executor scheduling.

### Verification

- Targeted RED command:
  `D:/python/teledrive-webdav/.venv/Scripts/python.exe -m pytest tests/test_upload_dedup.py -q`
  - `10 passed, 1 failed` (the new missing-routing regression)
- Targeted GREEN command:
  `D:/python/teledrive-webdav/.venv/Scripts/python.exe -m pytest tests/test_upload_dedup.py -q`
  - `11 passed in 0.27s`
- Task-focused command:
  `D:/python/teledrive-webdav/.venv/Scripts/python.exe -m pytest tests/test_upload_dedup.py tests/test_upload_preview.py -q`
  - `26 passed in 0.78s`
- Full offline suite:
  `D:/python/teledrive-webdav/.venv/Scripts/python.exe -m pytest tests -q`
  - `376 passed in 14.73s`

### Self-review

- The test gate observes the actual private claim operation and waits on a
  condition, so the owner cannot be released before the follower receives the
  same future.
- The forwarding is per `UploadedPart`, rather than a global/default account,
  preserving a different account for every split or reused part.
- The change is limited to the registration keyword and Task 8 regression
  coverage; no upload scheduling or canonicalization behavior changed.
- `git diff --check` reported no whitespace errors.
