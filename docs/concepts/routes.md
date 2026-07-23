# Routes

Routes define expected worker output, allowed GitHub actions, verification
policy, workspace mode, worker selection, and skill guidance.

Packaged routes include analysis, new pull request, existing pull-request
update, source review, review comment, classification, local result, and
waiting-for-user flows.

Immutable fields such as `allowed_github_actions`, `expected_output`,
`verification_policy`, and `workspace_mode` cannot be changed in
`~/.config/robert/config.yml`. Global and repository overrides may set only:

```yaml
routes:
  new-pr:
    worker: default
    required_skills: []
    recommended_skills:
      - fast-add-tests
```

Repository overrides replace only the fields they explicitly provide.
