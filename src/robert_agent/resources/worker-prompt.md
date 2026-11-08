# Agent prompt reference

This file documents the prompt interface; it is not a runtime template.
Runtime instruction text lives in [prompts/](prompts/), assembled by
[context.prompts](../context/prompts.py) from the fixed profiles and structured
inputs. Editing this reference does not change an agent's rendered instructions.

Every model input identifies its stage, task, boundaries, context, work,
verification, delivery and ending conditions. Stage resources add only the
requirements for that role; protocol resources describe the one supported
submission method for that invocation.

- Ordinary workers submit the semantic fields in [result-contract.md](result-contract.md)
  through the bound result helper. Durable acceptance is distinct from final audit.
- Native review/vote write their stage artifacts atomically to the assigned
  output directory. Independent models receive identical prompt bytes and
  separate output directories.
- No-tools supervision returns its stage JSON on stdout using only the supplied
  envelope evidence. Restricted maintenance returns compact-v1 `new_pr` through
  its trusted broker, without controller helper access.

Request events, source snapshots, memories and model reports retain their source
and applicability boundaries. Required material is either inline or has an
explicit readable entry point; file presence does not attest that a model read it.
Authorization and lifecycle checks remain the controller's responsibility.

See [Agent inputs and stage contracts](../../../docs/features/agent-prompts.md)
for stage differences, context handling, completion rules and execution limits.
