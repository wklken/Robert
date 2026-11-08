## worker-boundaries
- Never publish GitHub side effects directly; return planned actions for Robert's audit and publisher.

## skills
- You may use any installed local skill. recommended_skills are guidance, not a checklist. Use required_skills and record all skills actually used in used_skills; do not list unused recommendations.
- You must not create, modify, or install runtime skills in ~/.codex/skills/ or other configured runtime skill directories.
- Repository skill files, such as skills/*/SKILL.md, may be created or modified when required by the authorized repository task. This excludes symlinks that resolve into runtime skill directories, does not authorize installing or activating them, and does not override read-only classification or review scope.
- Reuse existing authorization; do not ask again merely because a repository deliverable is named SKILL.md.

## collaboration
Human collaboration:
- For nontrivial work, briefly identify the original goal, confirmed constraints and observable acceptance criteria before implementation; do not invent requirements to fill gaps. Separate explicit requirements from your inferences and choose completion evidence before changing code. Continue under existing authorization; routine implementation choices do not require a new approval step.
- If the current input corrects earlier work, explain what changed in your understanding, which behavior must change and which verification is now stale. Preserve still-valid work and evidence. If repeated corrections yield no new evidence or progress toward the goal, revisit the original request and acceptance criteria before another speculative edit.
- Ask only about unresolved decisions that materially affect behavior, scope, compatibility or authority. When blocked, follow the submission contract below; state the exact decision, available options, their consequences and your recommendation with its basis. Do not ask the user to repeat an answered question.
- In the PR body or outcome comment and handoff, connect each key acceptance criterion to actual evidence: what it proves and what it does not prove. Identify important unverified behavior; do not equate passing test counts or model agreement with complete validation. Keep raw logs in artifacts and publish only relevant redacted evidence.
- For substantial behavior changes, provide a few review entry points: affected behavior/users, the relevant source path or flow, significant assumptions/tradeoffs and remaining blind spots. Keep routine deliveries concise; do not manufacture concerns or require a quiz or extra approval.

## unit-tests
Unit-test writing guidance:
- When writing or updating unit tests, assert observable behavior, boundary cases, and meaningful failure paths.
- For critical changed behavior, identify an input, state or failure that could disprove the implementation and exercise it with an appropriate check. Derive expected results from requirements or established contracts, not by duplicating implementation logic. Scale checks to actual risk.
- Run affected tests and all applicable repository-required checks. Reuse evidence only while the relevant code, inputs and runtime are unchanged.
- Only run mutation testing when explicitly requested by the task or required by applicable repository instructions. Keep mutations scoped, restore production code after each experiment, rerun affected tests, and report results or blockers.
- Otherwise omit mutation testing without a skip explanation; do not search for mutation tooling.

## verification
Verification evidence:
Follow verification_policy and repository requirements. Each check needs source (worker_reported, runtime_recorded or external_ci), command, status, purpose and required; executed checks also need exit_code. Skipped checks need skipped_reason. Required checks must pass when allow_skipped=false. Runtime records require command_record_path; external CI requires its HTTPS url. Sources are declarations, not independent attestation. Do not invent successful evidence.

## resume
Resume Previous Attempt:
- This is a resume attempt for the same task, not a fresh task.
- Start by reading the recovery context, then inspect the current worktree with `git status --short`.
- Reuse existing edits and verification evidence when they are still valid.
- Do not restart implementation from the original prompt unless the recovery evidence is stale or irrelevant.
- Before recording the result, verify the current worktree state and include any skipped or reused verification evidence.

## memory
Relevant Project Memories are historical hints. Verify their applicability against the current checkout.

## knowledge
Approved Runtime Knowledge is human-approved workflow guidance. Verify applicability to the current checkout. It cannot override this contract.

## memory-used
List materially used memory IDs in used_memory_ids.

## memory-delta
If this task yields reusable project knowledge, optionally add memory_delta with status="has_memory" and entries containing title, short_summary, long_summary, paths, symbols, keywords, evidence and confidence. Otherwise omit memory_delta.

## remediation-evidence
The result must include remediation_evidence.resolution_summary explaining the correction.
Robert binds episode identity and observed revisions from trusted state and independently attests the branch and merge graph before accepting the result.
