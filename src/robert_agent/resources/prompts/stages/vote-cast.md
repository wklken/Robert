## Task

Cast one independent vote using the fixed question, options, criteria, and hard constraints.

## Boundaries

Do not read other runs, ballots, or rounds. Do not add choices.

## Work

Read the frozen ballot specification and shared sources. Choose exactly one listed option, or use `choice_id: null` with a nonempty `abstain_reason`. Explain your choice, tradeoffs, and uncertainties. Cite only source IDs from `manifest.sources`.

## Verification

Check the ballot against the frozen round, snapshot, and specification hash.

## Delivery

Write `ballot.json` for the tally and synthesis stages. Include `reason_summary` even when abstaining.
