# Exit Codes

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | success | Command completed and was not blocked. |
| `2` | retryable | A caller may retry after an external or temporary condition changes. |
| `3` | invalid input | Configuration, arguments, identifiers, or local state are invalid. |
| `4` | security refusal | A trust or security boundary refused the operation. |
| `5` | state failure | Durable state or command output could not be read safely. |

Commands also print a structured `status` and, on failure, a redacted
`safe_error`. Use JSON output in automation:

```bash
robert doctor \
  --config ~/.config/robert/config.yml \
  --output json
```

State used by commands is stored under `~/.local/share/robert/`.
