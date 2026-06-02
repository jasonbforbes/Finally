# Change Review

Scope reviewed: today's edits in `planning/PLAN.md` intended to resolve `planning/REVIEW.md` findings 1-3.

## Batch 1: Chat-message row-per-turn contract

### Assessment

The new row-per-turn language in Section 7 is mostly consistent with the rest of `PLAN.md`. It clearly defines a successful `POST /api/chat` as exactly two `chat_messages` rows, with the user row first, assistant row second, shared `user_id`, assistant `message_id`, and timeout behavior writing neither row. Section 8.4 and Section 9 step 7 now reflect that same two-row model, and Section 9 step 2 correctly clarifies that `CHAT_HISTORY_LIMIT` counts rows rather than turns.

This resolves the original finding about whether chat persistence stores one row or two rows per turn.

### Findings

1. **Potential timestamp ordering ambiguity for same-clock writes.**
   Section 7 requires the assistant row's `created_at` to be strictly greater than the user row's. That is a useful ordering contract, but the plan does not say how to guarantee it when both rows are created in the same transaction and timestamp precision is milliseconds. Implementers may need to synthesize the assistant timestamp, use higher precision, or order secondarily by insertion/id. Recommendation: specify the timestamp generation rule or require history queries to order by `(created_at, turn/order)` rather than relying only on strict wall-clock separation.

2. **Failure-after-action behavior is still slightly underdefined.**
   Section 9 says actions are auto-executed before the turn is persisted, and Section 7 says both chat rows are written in a single transaction. It is clear that LLM timeout writes nothing, but it is not explicit what happens if action execution partially succeeds and chat persistence then fails. That may be acceptable for v1, but the contract now makes chat rows the only durable explanation of executed assistant actions. Recommendation: either state that action execution plus chat persistence happens in one database transaction where practical, or explicitly accept that trade/watchlist changes may persist even if chat history write fails.

## Batch 2: `GET /api/chat/history`

### Assessment

The new endpoint fills the missing UI contract for rehydrating the chat panel after reload. Its response shape matches the `chat_messages` schema from Section 7, returns both user and assistant rows, preserves the action JSON semantics, and separates UI history pagination from the LLM prompt window. This resolves the original finding that the frontend had no documented way to load prior chat messages.

### Findings

1. **The "never returns partial turns" rule conflicts with arbitrary row limits.**
   The endpoint documents `limit` as a maximum number of rows, but also says the endpoint never returns partial turns and always begins with a `user` row except when history is shorter than `limit`. If a caller requests an odd `limit` such as `1`, the implementation cannot both return complete two-row turns and return up to exactly one row unless it returns zero rows or exceeds the limit. Recommendation: define whether `limit` is rounded down to an even number, rounded up, rejected unless even, or treated as a target rather than a hard maximum.

2. **Pagination by `before=<created_at>` can skip rows with duplicate timestamps.**
   The endpoint paginates older history with `created_at < before`. The row-per-turn contract only guarantees strict ordering between the two rows in a single turn; it does not guarantee unique timestamps across different turns. If two rows from different turns share the same timestamp, using only `before` can skip rows. Recommendation: add a stable cursor using `(created_at, id)` or a server-issued cursor, or require `created_at` uniqueness for chat rows.

3. **`has_more` is underspecified when turn-boundary adjustment changes the window.**
   `has_more` is defined as true when at least one older row exists before the earliest row returned. With the no-partial-turn rule, the server may adjust the cut to avoid splitting a pair. The plan should make clear whether `has_more` is computed before or after that adjustment. Recommendation: define it as "older than the earliest returned row after turn-boundary adjustment" so frontend pagination remains predictable.

## Batch 3: Initial snapshots for SSE streams

### Assessment

The price SSE update in Section 6 now states that the server sends a full price-cache snapshot immediately on connect or reconnect, and waits for the first real tick instead of sending `{}` if the cache is empty. That directly resolves the stale-start problem for `/api/stream/prices`.

The portfolio SSE update in Section 8.2 now states that each connection receives an `initial` event before any `trade` or `snapshot` event, including on reconnect. That directly resolves the original concern that a newly loaded tab could wait up to 5 minutes for authoritative portfolio state.

### Findings

1. **Price initial-snapshot event has no explicit SSE event type.**
   Section 6 says the first event has the same payload shape as later price updates and clients can treat it as a snapshot, but it does not say whether the SSE `event:` field is omitted or named. This is not blocking if the client listens only to `message`, but it is a small contract gap. Recommendation: state that all price events use the default EventSource `message` event, or define explicit event names.

2. **Portfolio `initial` event may race with trades unless snapshot creation is serialized.**
   Section 8.2 says `initial` is sent before any `trade` or `snapshot` event on the same connection. It does not define whether the initial portfolio state and subsequent trade notifications are read under the same lock/order as trade execution. Without that, a trade committed during connection setup could be reflected in the initial snapshot and also delivered as a `trade` event, or missed as a separate event. Recommendation: define that the stream captures the current portfolio state after subscription registration, then emits later trade events by monotonically increasing event/version order, or explicitly allow duplicate-state events and require idempotent frontend handling.

3. **The price empty-cache wait changes health/startup expectations but does not cross-reference them.**
   Holding the price stream open until the first real tick is consistent with `GET /api/health` returning `503` before price data exists, but the plan still does not define how long startup may take for external data. This does not reopen the original SSE finding, but it leaves implementers with an operational edge case around first-load behavior when Massive is slow or unavailable.
