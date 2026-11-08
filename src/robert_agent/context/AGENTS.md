# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Own prompt and skill rendering, attempt context artifacts, project memory
  and runtime knowledge. Consume authorized route and task inputs from the
  caller and preserve their scope when rendering.
- Keep the human review transaction in `application/knowledge_review.py`.
  Retrieve active approved knowledge for the repository and matching scope.
  Preserve attempt-specific artifacts and verification evidence provenance.
- Generate Worker and status paths from the canonical installed entrypoints.
  Keep context preparation independent of executable CLI imports.
- Test generated context, path validity and skill or knowledge selection without
  running a model.
- Keep runtime instruction text in `resources/prompts/` and use the shared
  `prompts` renderer with fixed stage profiles. Stage input builders supply
  facts, references and existing output contracts, not free-form instruction tails.
  Preserve helper, atomic-file and stdout completion semantics separately.
