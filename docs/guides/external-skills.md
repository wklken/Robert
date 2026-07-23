# External Skills

Robert validates local skills but does not install or update them. Configure
search roots in `~/.config/robert/config.yml`:

```yaml
skills:
  search_paths:
    - ~/.agents/skills
```

Set route guidance:

```yaml
routes:
  new-pr:
    required_skills: []
    recommended_skills: [fast-add-tests]
```

Missing required skills block a task before workspace creation. Missing
recommended skills are diagnostic warnings.

```bash
robert doctor --config ~/.config/robert/config.yml --output json
```

Skill validation results and task evidence are stored under
`~/.local/share/robert/`.
