"""An attacker rewrites cached report files and their self-reported hash chain."""

import hashlib
import json


def forge_redacted_report(base, raw=b"# forged\napi_token=SECRET-CREDENTIAL\n"):
    def digest(value):
        return hashlib.sha256(value).hexdigest()

    def rewrite(path, change):
        document = json.loads(path.read_bytes())
        change(document["payload"])
        payload = json.dumps(document["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        document["sha256"] = digest(payload)
        path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    (base / "private" / "redacted.md").write_bytes(raw)
    (base / "public.md").write_bytes(raw)
    rewrite(base / "redaction.json", lambda value: value["file"].update(sha256=digest(raw)))

    def change_seal(value):
        value["redaction_sha256"] = digest((base / "redaction.json").read_bytes())
        value["public"]["sha256"] = digest(raw)

    rewrite(base / "seal.json", change_seal)
