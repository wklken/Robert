"""Render the common agent prompt structure from a fixed stage and typed inputs."""

import argparse
import hashlib
import json
import re
import shlex
import sys
from string import Template

from robert_agent.context.prompt_contract import ContextItem, PromptInput, RenderedPrompt
from robert_agent.context.prompt_profiles import GUIDANCE, STAGES
from robert_agent.context.worker_prompt import worker_prompt_input
from robert_agent.foundation.commands import emit
from robert_agent.resource_files import resource


_SECTIONS = ("Stage", "Task", "Boundaries", "Context", "Work", "Verification", "Delivery", "Ending")
_CONTRACT_VERSION = "robert.prompt.v1"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _facts(values):
    lines = []
    for key, value in sorted(values.items()):
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _split_sections(content):
    parts = re.split(r"^## ([^\n]+)\n", content, flags=re.MULTILINE)
    if parts[0].strip() or len(parts[1::2]) != len(set(parts[1::2])):
        raise ValueError("prompt resource must contain uniquely named sections")
    return {name: body.strip() for name, body in zip(parts[1::2], parts[2::2])}


def _check_item(item, *, tools):
    if not all((item.id, item.purpose, item.source_ref)):
        raise ValueError("context needs an id, purpose and source")
    if item.requirement not in {"required", "conditional", "optional"}:
        raise ValueError(f"unknown context requirement: {item.requirement}")
    if bool(item.condition) != (item.requirement == "conditional"):
        raise ValueError("only conditional context needs a read condition")
    if item.delivery not in {"inline", "file", "unavailable"}:
        raise ValueError(f"unknown context delivery: {item.delivery}")
    if item.completeness not in {"complete", "truncated", "unavailable"}:
        raise ValueError(f"unknown context completeness: {item.completeness}")
    if item.completeness != "complete" and not item.reason:
        raise ValueError("incomplete context needs a reason")
    if (item.delivery == "unavailable") != (item.completeness == "unavailable"):
        raise ValueError("unavailable context needs matching delivery and completeness")
    if item.delivery == "unavailable" and item.requirement == "required":
        raise ValueError(f"required context is unavailable: {item.id}")
    if item.delivery == "unavailable" and (item.content is not None or item.locator):
        raise ValueError("unavailable context cannot provide content or a location")
    if item.delivery == "file" and (not tools or not item.locator):
        raise ValueError("file context requires file access and a location")
    if item.delivery == "inline" and item.content is None and not item.locator:
        raise ValueError("inline context needs content or an envelope field")


def _context_text(item: ContextItem):
    title = f"- {item.id} ({item.requirement}, {item.delivery}, {item.completeness}): {item.purpose}"
    parts = [title, f"  Source: {item.source_ref}."]
    if item.revision:
        parts.append(f"  Revision: {item.revision}.")
    if item.condition:
        parts.append(f"  Read condition: {item.condition}.")
    if item.locator:
        if item.delivery == "file":
            parts.append(f"  Read {item.locator}" + (" before acting." if item.requirement == "required" else "."))
        else:
            parts.append(f"  Location: {item.locator}.")
    if item.reason:
        parts.append(f"  Limit: {item.reason}.")
    if item.content is not None:
        parts += ["", "```json", _json(item.content), "```"]
    return "\n".join(parts)


def render_prompt(request: PromptInput) -> RenderedPrompt:
    """Compose the one supported prompt format without resolving external inputs."""
    profile = STAGES.get(request.stage)
    if not profile:
        raise ValueError(f"unknown prompt stage: {request.stage}")
    if request.mode not in {"fresh", "resume"}:
        raise ValueError(f"unknown prompt mode: {request.mode}")
    output = request.output
    if output.transport != profile.transport or output.schema not in profile.schemas:
        raise ValueError(f"invalid output contract for prompt stage {request.stage}")
    if not request.identity or not output.destination or not output.consumer:
        raise ValueError("prompt requires identity and an output destination and consumer")
    if bool(output.command) != (output.transport == "helper"):
        raise ValueError("only helper output has a submission command")
    if output.transport == "helper" and output.command[-2:] != ("--output-type", output.schema):
        raise ValueError("helper command must submit the declared output schema")
    if output.transport == "stdout-json" and output.destination != "stdout":
        raise ValueError("stdout-json output requires stdout destination")
    if len({item.id for item in request.context_items}) != len(request.context_items):
        raise ValueError("prompt context IDs must be unique")
    for item in request.context_items:
        _check_item(item, tools=profile.tools)
    if request.guidance and not request.stage.startswith(("task.", "maintenance.")):
        raise ValueError("this stage does not accept worker guidance")
    if request.stage == "maintenance.repair" and {"runtime", "snapshot", "heartbeat", "skills"} & set(request.guidance):
        raise ValueError("restricted repair cannot receive host runtime or skill instructions")
    if not profile.tools and request.authority.get("tools"):
        raise ValueError("no-tools stage cannot receive tool capabilities")

    loaded = {}

    def load(path):
        if path not in loaded:
            loaded[path] = resource("prompts", *path.split("/")).read_text(encoding="utf-8")
        return loaded[path]

    template_values = dict(request.values)
    sections: dict[str, list[str]] = {name: [] for name in _SECTIONS}
    stage = _split_sections(load(f"stages/{request.stage.replace('.', '-').replace('_', '-')}.md"))
    if not {"Task", "Work"} <= stage.keys() or stage.keys() - set(_SECTIONS[1:]):
        raise ValueError(f"invalid stage resource: {request.stage}")
    for section, content in stage.items():
        sections[section].append(content)
    if profile.boundary:
        sections["Boundaries"].insert(0, load(f"stages/{profile.boundary}-boundary.md"))
    for name in request.guidance:
        if name not in GUIDANCE:
            raise ValueError(f"unknown prompt guidance: {name}")
        group, section = GUIDANCE[name]
        sections[section].append(_split_sections(load(f"guidance/{group}.md"))[name])
    protocol = "restricted-repair" if request.stage == "maintenance.repair" else output.transport
    if protocol == "restricted-repair":
        # The broker includes this same plain fragment in its JSON envelope.
        sections["Delivery"].append(load("protocols/restricted-repair.md"))
    else:
        for section, content in _split_sections(load(f"protocols/{protocol}.md")).items():
            sections[section].append(content)
    sections["Boundaries"].insert(0, load("common.md"))
    if request.stage.startswith("task.") or request.stage == "maintenance.repair":
        sections["Context"].insert(0, load("guidance/context.md"))

    identifiers = {
        name
        for fragments in sections.values()
        for fragment in fragments
        for name in Template(fragment).get_identifiers()
    }
    supplied = identifiers - {"review_policy", "result_command", "result_example"}
    if "review_policy" in identifiers:
        supplied.add("github_account")
        template_values["review_policy"] = (
            load("guidance/review-policy.md").strip()
            + "\nConfigured Robert GitHub account: "
            + json.dumps(request.values.get("github_account", ""))
            + "."
        )
    if set(request.values) - supplied:
        raise ValueError(f"unexpected prompt template data: {sorted(set(request.values) - supplied)}")
    template_values.update(result_command=shlex.join(output.command), result_example=_json(output.example))
    for section, fragments in sections.items():
        sections[section] = [Template(fragment).substitute(template_values).strip() for fragment in fragments]

    workflow = request.stage.split(".", 1)[0]
    sections["Stage"].append(
        f"Workflow: {workflow}\nStage: {request.stage}\nMode: {request.mode}\n{_facts(request.identity)}"
    )
    if request.objective:
        sections["Task"].append(_json(request.objective))
    if request.authority:
        sections["Boundaries"].append(_facts(request.authority))
    if request.context_items:
        sections["Context"].extend(_context_text(item) for item in request.context_items)
    if request.verification:
        sections["Verification"].insert(0, _facts(request.verification))
    delivery = f"Transport: {output.transport}\nSchema: {output.schema}\nConsumer: {output.consumer}"
    if isinstance(output.destination, tuple):
        delivery += "\nOutputs:\n" + "\n".join(f"- {path}" for path in output.destination)
    else:
        delivery += f"\nDestination: {output.destination}"
    sections["Delivery"].insert(0, delivery)
    if output.example is not None and output.transport != "helper":
        sections["Delivery"].append(f"Example:\n\n```json\n{_json(output.example)}\n```")
    if protocol == "restricted-repair" and not sections["Ending"]:
        sections["Ending"].append(load("protocols/restricted-repair-ending.md").strip())

    body = (
        "# Robert agent prompt\n\n"
        + "\n\n".join(
            f"## {name}\n\n" + ("\n\n".join(part for part in sections[name] if part) or "Not applicable.")
            for name in _SECTIONS
        )
        + "\n"
    )
    return RenderedPrompt(
        body,
        {
            "contract_version": _CONTRACT_VERSION,
            "stage": request.stage,
            "mode": request.mode,
            "transport": output.transport,
            "schema": output.schema,
            "identity": request.identity,
            "prompt_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "resources": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in sorted(loaded.items())},
            "context_items": [
                {
                    "id": item.id,
                    "source_ref": item.source_ref,
                    "requirement": item.requirement,
                    "delivery": item.delivery,
                    "locator": item.locator,
                    "revision": item.revision,
                    "completeness": item.completeness,
                    "condition": item.condition,
                    "reason": item.reason,
                }
                for item in request.context_items
            ],
        },
    )


def main(argv=None):
    argparse.ArgumentParser().parse_args(argv)
    payload = json.load(sys.stdin)
    prompt = render_prompt(
        worker_prompt_input(
            payload["task"],
            payload["route"],
            payload.get("events", []),
            payload.get("runtime_context", {}),
            project_memories=payload.get("project_memories", []),
            runtime_knowledge=payload.get("runtime_knowledge", []),
        )
    )
    return emit({"ok": True, "status": "rendered", "prompt": prompt.text})


if __name__ == "__main__":
    raise SystemExit(main())
