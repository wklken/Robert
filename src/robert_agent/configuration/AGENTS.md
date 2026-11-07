# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Own the v2 load, schema, normalization and validation pipeline here. Preserve
  rejection of duplicate and non-string YAML keys. Update packaged schemas and
  examples together with changes to the contract.
- Load fixed route policy from `resources/routes.yml`. Apply only permitted
  overrides in their current order. Preserve defaults, units and worker order.
  Configuration must not widen route action or verification permissions.
- Use packaged resources and Worker adapter configuration capabilities. Keep
  runtime, workflows and application services out of the import graph. Preserve
  `skip_external` for callers that validate without external checks.
- Test defaults and override precedence alongside invalid inputs, including
  unsupported options and unknown workers.
