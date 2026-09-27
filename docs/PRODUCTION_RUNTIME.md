# Production runtime contract

This describes implemented behavior, not a claim of completed video delivery.

## Routing

`image_provider` defaults to `imagegen`. With `stage: images`, the worker records a private prompt and `awaiting_imagegen`, without making an Agnes request. The conversation agent must actually invoke its native imagegen tool. Actions does not implement or emulate that tool.

`stage: import_images` imports `external_keyframes[shot_id]` with `provider: imagegen`, a nonempty `provenance`, `transfer_authorized: true`, `source_url`, `video_input_url` and the original `sha256`. Both URLs must yield identical validated image bytes. It records source provenance and privately stores the media. A hash-checked imported frame still needs visual review and inclusion in `approved_images` before video submission. Use only authorized, sufficiently long-lived HTTPS delivery URLs; no new third-party hosting is implicitly authorized.

Agnes fallback requires `image_provider: agnes` and `image_fallback` with `reason` in `primary_unavailable`, `primary_rate_limited`, `primary_transport_failure`, `user_requested`; nonempty factual `evidence`; and `safety_rejected: false`. The caller must supply truthful evidence. Actions lacking imagegen is not primary-tool failure. Historical completed images are preserved and reused with their original provenance.

## Shared queue and limits

All authenticated Agnes calls from this worker use `production_runtime.Provider`. The shared private `.production-state/agnes-account-queue.json` uses GitHub content SHA compare-and-swap. FIFO waiters renew 180-second leases; an in-flight request holds a 600-second lease. Calls are serialized and spaced at least 65 seconds after the preceding call completes. GET polling and fallback image POSTs share the limit; this is deliberately conservative until endpoint-specific RPM scope is verified.

The queue survives separate invocations and shares limits across tasks using the SAME private storage. A failed lease release leaves a conservative expiring lease; it never causes API resubmission. GitHub `concurrency` adds protection but does not replace the queue. Other applications and historical private workflows do not magically participate: do not run them concurrently with this account unless they adopt this queue.

Only the explicitly selected private request is processed. Its configured shot list and per-shot records persist. The workflow is not a background scheduler that automatically drains all private requests; GitHub can replace pending concurrency jobs. Resume unprocessed requests explicitly from private state. No production is triggered by code or documentation changes alone.

## Retry matrix

| Operation | Handling |
|---|---|
| POST 429 | Up to four total attempts across resumes; Retry-After seconds/date, exponential delay 65/130/260 seconds plus 0–5s jitter; shared account cooldown |
| POST 400/401/402/403 or other 4xx except 429 | Persist rejected; no automatic retry |
| POST 5xx, timeout, transport/non-JSON uncertainty | Persist outcome_unknown; no blind POST retry; response task ID permits polling |
| Success response parsing | Persist private response before extracting video_id; task id and video_id are not assumed interchangeable |
| GET 408/429/500/502/503/504 or transport | At most five consecutive attempts across restarts, bounded exponential backoff, queue and deadline; success resets consecutive errors |
| Media GET temporary error | Four attempts per invocation; Retry-After honored; defer if wait exceeds 300s; never create new generation to recover a download |
| State write failure | Stop before new submission; conflicting task checkpoints are not overwritten |
| Queue conflict | CAS retry with bounded waiting; expired waiter recovery |

Poll deadline is persisted for 2400 seconds and is not silently reset on resume. The provider has a 45-minute per-invocation budget, leaving room before the 55-minute job limit. Exhausted retry/deadline budgets require explicit reconciliation or a reviewed budget change. A provider-side content refusal never triggers a provider switch.

The old `/v1/videos` GET history probe returned 404. `reconcile` now records `provider_history_required` and stops; it does not pretend that an undocumented history API works or that no task exists. Known IDs are resumed through the documented `/agnesapi` query.

## Privacy and tests

Queue data, prompts, response bodies, task IDs, source URLs and delivery URLs are private. Public logs expose codes only. One-off media use private draft Release assets; source Git contains only small state documents and configuration. Existing source/blob identities and uncertain production records are not rewritten by this change.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and `python3 scripts/public_boundary.py` after staging reviewed files. Tests use fake clocks, fake GitHub CAS and fake provider responses, covering routing, FIFO expiry/conflicts, cross-worker throttling, 429 backoff/exhaustion, 503 uncertainty, task-ID recovery, permanent rejection and GET retries. They do not invoke generation or establish artistic quality.
