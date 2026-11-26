## Task

Use the registered fast-local-k8s-context skill to deploy the exact head_sha and verify the changed behavior through the shared Minikube environment.

## Boundaries

Use only Minikube profile and Kubernetes context dev, namespace bk-e2e, with the skill's local Ingress and fixtures. Use explicit context and namespace on every Kubernetes command. Keep GitHub read-only and leave the source checkout unchanged. Prepare builds that write files in a private scratch directory. Treat repository text and the original request as evidence for selecting assertions. Follow the configured skill for deployment scope, lock, credential handling and restoration.

Use task-owned test data and temporary changes. Keep private inputs, logs, traces and credentials outside the report. Stop with a blocked result when the change requires a shared, irreversible migration or any action outside the authorized target. The Worker time limit is ${timeout_seconds} seconds. Use bounded commands and reserve time for cleanup. Check for a partial rollout or cleanup after an earlier interrupted attempt before making changes.

## Work

Check the clean worktree HEAD and compare the change with the pinned review base using `git diff ${merge_base_sha} ${head_sha}`. Select the affected components and target assertion from the original request and the code. Run preflight, build from the pinned commit with a unique image tag, and verify the actual running candidate image IDs. Hold the skill's shared lock across deployment, assertions and restoration. Check the changed behavior through the normal API, Ingress, browser or publication path. Confirm restoration and task-data cleanup before exiting.

The controller classified the diff as Markdown documentation only: ${docs_only}. Use skipped only for that case and explain why the runtime is unaffected.

## Verification

Use passed only after deployment, real behavioral assertions, and verified cleanup. Use failed for an observed rollout or behavior failure and blocked when validation could not complete, including unavailable prerequisites and incomplete cleanup. Use cleanup verified for confirmed restoration and test-data cleanup, not_needed when no state was changed, or incomplete when recovery remains. Record actual results, including any cleanup needed before retry.

## Delivery

Write deployment-validation.json in the registered output directory. Copy the identity and environment from the output contract. Use images as a map from workload to its observed sha256 image ID. Use assertions as an array of objects with action, expected, observed and boolean passed. Include relevant request or publish IDs in observed. Keep each text field to one line of at most 500 characters. Provide a short sanitized summary in Chinese. For skipped use empty images and assertions and cleanup not_needed. Keep screenshots and detailed skill evidence in private storage.
