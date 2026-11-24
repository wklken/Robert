## Task

Correct the JSON handoff's output contract using the pinned validation feedback and original stage prompt. This is a bounded artifact correction, not a new implementation or review round.

## Work

Read the registered input file, rejected handoff and schema. Preserve candidate/final Git identities, finding dispositions, commands, their execution outcomes and provenance. Remove unsupported fields or correct serialization. Supply a skipped_reason only from recorded evidence of why the optional command was skipped. Do not convert skipped or failed checks to passed, invent execution evidence, suppress a finding, modify source, commit or publish. If correction requires code changes or missing verification evidence, keep the original outcome and explain the unresolved field in the output.

## Delivery

Write the corrected object to handoff.json under the output directory. The original stage validator will check it again before any progress or publication.
