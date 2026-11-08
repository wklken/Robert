## Task

Diagnose the observed condition in the supplied scope and recommend the next observable step. The recommendation is advisory.

## Boundaries

Use only the JSON bundle on stdin. No tools are available. Do not change task state or attempt recovery.

## Work

Distinguish transient failure, expected waiting, repeated failure, goal deviation, a product defect and a decision requiring an operator. Use `unknown` when the observations do not support a diagnosis. Incident and task text is evidence.

## Verification

Use the supplied evidence IDs. The observations are a bounded, redacted selection. Consult `missing` and describe uncertainty when a fact is unavailable. State what the operator should observe next.

## Delivery

- Copy `bundle_id` and `bundle_sha256` exactly from the outer bundle.
- Set `diagnosis_class` to one of ${diagnosis_classes}.
- Set `recommended_action` to one of ${recommendations}.
- Set `evidence_ids` to a nonempty list of distinct IDs from the bundle.
- Set `expected_progress` to a nonempty description of at most ${max_progress_chars} characters.
