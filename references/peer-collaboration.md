# Bounded Pi peer collaboration

Peer collaboration lets multiple focused Pi agents exchange typed requests and
responses during one workflow stage. It is useful for independent review,
cross-device evidence access, heterogeneous-model comparison, and specialist
consultation. It is not a scheduler, durable queue, verifier, or source of
workflow authority.

```text
controller stage -> typed request -> peer agent -> typed response
                 -> exchange validator -> receipt -> stage aggregation
                 -> mechanical verifier -> deterministic transition
```

## Pi capability and adopted primitive

Pi extensions can register LLM-callable tools, inject follow-up messages,
observe lifecycle events, persist session entries, manage long-lived sockets,
and expose UI. A peer adapter can therefore offer four small tools:

- `list`: discover peers and their declared identity/capability card;
- `send`: submit one bounded request and return a message ID;
- `get`: poll without blocking;
- `await`: wait with a deadline and cancellation signal.

The IndyDevDan Pi-to-Pi implementation demonstrates local sockets and an
HTTP/SSE hub with agent discovery, message IDs, hop limits, timeouts, inbox
limits, heartbeats, and audit events. Adopt the primitive, not its trust model.
The reviewed implementation parses JSON when a `response_schema` is supplied
but does not validate that schema, uses a shared network bearer rather than
per-agent identity, previews prompt content in server logs, keeps hub messages
in memory, and binds inbound responses through one mutable active context.
Those shortcuts are acceptable for a demonstration but not for a certified
workflow boundary.

Primary references:

- [Pi extensions](https://pi.dev/docs/latest/extensions)
- [Pi RPC](https://pi.dev/docs/latest/rpc)
- [Pi SDK](https://pi.dev/docs/latest/sdk)
- [Pi-to-Pi video](https://www.youtube.com/watch?v=PIdETjcXNIk)
- [Pi-to-Pi reference implementation](https://github.com/disler/pi-vs-claude-code#pi-to-pi-agent-to-agent-communication)

## Authority split

The controller owns job identity, stage, allowed peers, tool policy, transport
credentials, timeouts, message/hop/byte budgets, cancellation, retries,
receipts, quorum, checkpoints, and terminal state. Peers may investigate and
return candidate findings. Deterministic scripts own schema validation,
permissions, side effects, acceptance criteria, and completion. A calibrated
judge may score semantic quality after mechanical gates.

Never allow a peer to emit `next_stage`, grant approval, mark the job complete,
or turn peer consensus into truth. A peer response is prior model output and
therefore untrusted data until its envelope, evidence, and domain claims pass
the relevant validators.

## Compiled protocol

Schema `1.2` requires a specialized runtime and an explicit
`peer_collaboration` block. Keep the first topology small: two or three peers,
one peer-enabled stage, and one request per peer. Pin every model and tool
profile. The compiler emits the normalized contract, schemas, and a
digest-bound exchange validator.

Each request carries:

- `message_id` and job-scoped `correlation_id`;
- controller-owned `stage`, `sender`, `recipient`, and `hop`;
- an ISO timestamp;
- a bounded object payload whose retrieved or user-authored content is data.

Each response reverses the sender/recipient route, binds `in_reply_to`, repeats
the correlation ID and stage, and returns `completed`,
`insufficient_evidence`, or `error`. Findings require stable IDs, severity,
claims, and source locators. Optional source digests bind evidence to immutable
artifacts.

The exchange receipt stores no prompt or response body. It records IDs, route,
stage, hop, status, finding count, and SHA-256 digests for the contract, request,
and response. Content retention belongs in access-controlled run artifacts and
must follow the workflow's redaction and retention policy.

## Transport requirements

For local sockets, restrict the directory and endpoint to the owning OS user,
use unpredictable session IDs, validate the peer card against the compiled
contract, and reject messages larger than the configured limit.

For network transport, require TLS plus per-agent credentials or mTLS. Bind the
authenticated principal to one peer ID; never trust a body-supplied session ID.
Rate-limit requests, cap request bodies and inbox depth, and prevent one peer
from deleting, heartbeating, responding for, or impersonating another. Do not
log message previews.

The transport may be ephemeral. The workflow ledger may not be. On reconnect,
reconcile by correlation ID and receipt digest. Retries must reuse the workflow
job key and must not duplicate side effects.

## Failure and concurrency rules

- Register the pending correlation before dispatch so a fast response cannot
  become orphaned.
- Bind the active turn to one inbound message ID; never infer the reply target
  by selecting the newest unfinished request.
- Reject duplicate responses unless their digest exactly matches the first
  accepted receipt.
- Timeout ends the wait and propagates cancellation. It does not by itself
  prove the remote work stopped.
- Enforce a global message budget in addition to hop limits; peers can create a
  costly loop without exceeding a shallow hop count if they start new threads.
- Below the required response count, fail the stage. Optional unavailable peers
  may continue only when the compiled policy says
  `continue_with_degraded_evidence`, and the degraded state must be visible.
- Truncate tool-visible output while retaining the full access-controlled
  artifact and digest outside model context.

## Certification cases

Before promotion, prove at least:

1. happy-path correlated response and metadata-only receipt;
2. undeclared peer, wrong stage, spoofed sender, and wrong `in_reply_to` fail;
3. malformed JSON and schema-invalid findings fail without model repair loops;
4. response byte, message, hop, deadline, and required-response limits fail;
5. late, duplicate, orphaned, and out-of-order responses reconcile correctly;
6. transport disconnect and controller restart resume from durable receipts;
7. cancellation reaches live peers and leaves a sealed partial run;
8. message bodies and credentials do not appear in console or audit logs;
9. network principals cannot impersonate, heartbeat, delete, or answer for a
   different peer;
10. peer consensus cannot override a failed verifier or authorize a side effect.
