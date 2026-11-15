"""Pure scope and command rules for controller-issued TAPD confirmations."""

from dataclasses import dataclass
import re

from robert_agent.foundation.tapd_identity import comment_name
from robert_agent.security.redaction import redact_text
from robert_agent.work import tapd_authority
from robert_agent.work.tapd_policy import normalize_comment_text, parse_comment

_COMMAND_START = re.compile(r"(?i)^(?:@\S+[ \t]+)?robert-choice\b")
_COMMAND_TOKEN = re.compile(r"(?i)\brobert-choice[ \t]+q-[a-z0-9_-]+")
_TOKENS = re.compile(r"q-[0-9a-f]{32} v([1-9][0-9]{0,8}) (resume|switch)", re.ASCII)


@dataclass(frozen=True, slots=True)
class Choice:
    question_id: str
    version: int
    option: str


def command(payload, bot_name, reply_name):
    """Return (reserved, choice); a reserved malformed command stays controller context."""
    body = payload.get("body_html")
    if not isinstance(body, str):
        return False, None
    parsed = parse_comment(body, reply_id=payload.get("reply_id"), bot_name=bot_name, reply_name=reply_name)
    lines = (line.strip() for line in parsed.own_text.splitlines())
    attempts = [line for line in lines if _COMMAND_START.match(line) or _COMMAND_TOKEN.search(line)]
    if not attempts:
        return False, None
    if not parsed.has_trigger or len(attempts) != 1:
        return True, None
    mention = re.escape(comment_name(bot_name, "bot_name"))
    prefix = re.compile(rf"@(?ai:{mention})[ \t]+(?ai:ROBERT-CHOICE)[ \t]+")
    line = attempts[0]
    found = prefix.match(line)
    if found is not None:
        tail = line[found.end() :]
        result = _TOKENS.fullmatch(tail)
        if result:
            return True, Choice(tail.split()[0], int(result[1]), result[2])
    return True, None


def supported_scope(config, project, facts, binding):
    if not binding or binding.get("state") != "paused":
        return None
    decision = tapd_authority.label_authority(facts, project)
    old = binding.get("rule")
    if (
        decision.reason != "allowed"
        or decision.rule is None
        or not isinstance(old, dict)
        or not binding.get("request_id")
        or decision.rule["repo"] not in project["repositories"]
    ):
        return None
    new = decision.rule
    return {
        "source": list(facts.identity.key),
        "binding_request_id": binding["request_id"],
        "old_rule": old,
        "current_rule": new,
        "option": "resume" if old == new else "switch",
        "bot_name": config["bot_name"],
        "reply_name": config["reply_name"],
        "trusted_author": project["trusted_author"],
    }


def _display(value):
    text = " ".join(normalize_comment_text(value).split())
    safe = redact_text(text, strict_assignments=True).get("text", "已隐藏")
    return safe.translate(str.maketrans({"@": " at ", "<": "‹", ">": "›", "&": "＆"}))[:160]


def question_body(question_id, version, scope):
    action = "恢复原仓库授权" if scope["option"] == "resume" else "确认当前仓库切换"
    original = _display(scope["old_rule"]["repo"])
    current = scope["current_rule"]
    repository, label, rule = (_display(current[key]) for key in ("repo", "label", "id"))
    return (
        f"单据的仓库授权已暂停。待确认操作：{action}。\n"
        f"原仓库：{original}。当前仓库：{repository}。标签：{label}。规则：{rule}。\n"
        f"在一条新评论中先提及机器人 {scope['bot_name']}，然后在同一行输入以下选项：\n"
        f"ROBERT-CHOICE {question_id} v{version} {scope['option']}"
    )
