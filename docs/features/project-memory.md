# Project memory and evidence stages

[Feature index](README.md)

An accepted result may add reusable technical hints through `memory_delta` before
its external actions finish. Pending or failed publication does not discard those
hints. Retrieved memory remains historical context that a Worker must verify
against its current checkout, not a source of execution or publication authority.

Each retrieved entry and rendered prompt includes controller-owned `provenance`
with its task, attempt and result IDs, plus an evidence stage:

- `unknown`: no matching accepted source result can be established.
- `audit_accepted`: the source result passed audit; external delivery is unconfirmed.
- `publication_confirmed`: every planned external action has a matching durable
  publisher receipt. Receipts identify actions, targets, commits and observation times.
- `deployment_confirmed`: the maintenance verifier additionally recorded a matching
  source-result/revision deployment proof, with build, daemon run and observed time.

Publication does not prove deployment. A deployment observation does not prove
that the original incident recovered, or that the same build remains deployed.
Model-written summaries and confidence fields cannot promote these stages. Text
claiming “published” or “deployed” is still only a model claim unless the matching
controller evidence is present. Stages are recalculated from current persisted
proof when a memory is retrieved; no model field supplies a receipt.

Memory retrieval remains repository-scoped. Human approval of runtime knowledge
is a separate path; accepting a result or publishing it does not approve runtime
knowledge or expand an operator's permissions.

Worker memory proposals are limited to 32 entries and 64 KiB of UTF-8 JSON.
Submission, audit and storage share the pure policy in
[`context/memory_policy.py`](../../src/robert_agent/context/memory_policy.py).
Secrets and text requiring redaction are rejected, including nested evidence;
rejected content is not copied into diagnostics or project memory.
