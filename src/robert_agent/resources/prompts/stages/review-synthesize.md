## Task

Verify and consolidate the findings from the selected reviewers against the pinned source and diff.

## Boundaries

Read reviewer artifacts only from the selected input list. Do not conduct an additional independent review or fill in failed models.

## Work

Read each listed `result.json` and `report.md`. Apply adversarial review to the supplied findings by seeking counterexamples and counterevidence. Merge findings by root cause, location, and trigger while retaining every source. A finding reported by one reviewer must not be dismissed by majority vote. Unreported and explicitly refuted are distinct.

${review_policy}

Apply the judgment policy only to verifying supplied claims and their merge implications. Reviewer confidence and prior verdicts are not evidence. Separate nonblocking improvements from blockers in `reason`, `suggestion`, and `merge_reason`.

Use `pr-info.json` `user.login` for the PR author identity. Preserve unresolved decision requests from selected reviewers' `coverage.notes` in `merge_reason` even when items is empty. Verify their evidence without inventing findings. Include the decision-owner mention, revision, evidence, options, and recommendation in `merge_reason` and relevant existing item reasons. Do not recommend ready to merge while a material risk awaits a decision.

## Verification

Verify dependency, contract, and scope claims against actual added or deleted lines. Use the resolved project interpreter for Python syntax claims. Use `needs_confirmation` when evidence is insufficient. Dismissal requires counterevidence.

Cover every finding and dismissed finding exactly once by `worker_id` and `finding_id`, and never add an unsourced finding. Read each reviewer's coverage limitations and qualify `merge_reason` accordingly. No findings or model agreement does not establish complete validation or human approval.

## Delivery

Write `evaluation.json` for the controller. Use an empty `items` array only when every selected reviewer has neither findings nor dismissed findings.

`severity` must be one of ${severities}. `disposition` must be one of ${dispositions}. `merge_recommendation` must be one of ${recommendations}.
