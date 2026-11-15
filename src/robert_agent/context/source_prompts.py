"""Assemble immutable inline source model prompts and their transport binding."""

from dataclasses import dataclass, field
import json

from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput
from robert_agent.context.prompts import render_prompt
from robert_agent.foundation.contracts.execution import SourceModelStage

MAX_INPUT_BYTES = 49152


class SourcePromptCapacity(ValueError):
    """Required inline context exceeds the source model capacity."""


@dataclass(frozen=True, slots=True)
class SourceModelPrompt:
    payload: str = field(repr=False)
    round_id: str
    run_id: str
    input_version: str
    prompt_sha256: str
    stage: str

    @property
    def binding(self):
        return {
            "round_id": self.round_id,
            "run_id": self.run_id,
            "input_version": self.input_version,
            "prompt_sha256": self.prompt_sha256,
        }


def build(
    *,
    stage,
    round_id,
    run_id,
    input_version,
    snapshot,
    candidates,
    evidence,
    omissions=(),
    child_results=(),
    members=None,
    context_sha256=None,
):
    if stage not in (SourceModelStage.IDENTIFY, SourceModelStage.SYNTHESIS):
        raise ValueError("invalid source model stage")
    if not isinstance(snapshot, dict) or not all(
        isinstance(value, str) and value.strip() and len(value) <= 256 for value in (round_id, run_id, input_version)
    ):
        raise ValueError("source model prompt requires a frozen identity and snapshot")
    identity = {"round_id": round_id, "run_id": run_id, "input_version": input_version}
    fields = [
        ("ticket_context", "Complete frozen source context", snapshot),
        ("candidates", "Repository IDs authorized by the source round", candidates),
        ("evidence", "Scoped file text and byte-revision proofs", evidence),
        ("evidence_omissions", "Explicit missing or limited file evidence", omissions),
    ]
    if stage == SourceModelStage.SYNTHESIS:
        fields.append(("child_results", "Accepted local results from this source round", child_results))
    elif child_results:
        raise ValueError("identification does not take child results")
    example = (
        {"kind": "question", "question": "Which repository owns this component?"}
        if stage == SourceModelStage.IDENTIFY
        else {
            "kind": "answer",
            "answer": "<scoped answer>",
            "references": [{"repo_id": "<offered ID>", "path": "<offered path>", "revision": "<offered revision>"}],
        }
    )
    rendered_identity = dict(identity)
    if members is not None:
        if not context_sha256:
            raise ValueError("versioned source prompt requires a context digest")
        rendered_identity["source_context_sha256"] = context_sha256
        fields.append(("request_members", "Exact frozen members and handled event fingerprint options", members))
        example["handled_event_fingerprints"] = ["<fingerprint from request_members>"]
    rendered = render_prompt(
        PromptInput(
            f"source.{stage}",
            rendered_identity,
            OutputContract(
                "stdout-json",
                f"source-{stage}-v{2 if members is not None else 1}",
                "stdout",
                "Robert source model validator",
                example={**identity, "prompt_sha256": "<copy from outer JSON>", **example},
            ),
            authority={"tools": []},
            context_items=tuple(
                ContextItem(
                    name,
                    purpose,
                    "controller-projected frozen facts",
                    requirement="required",
                    content=content,
                    revision=input_version,
                )
                for name, purpose, content in fields
            ),
        )
    )
    prompt_digest = rendered.metadata["prompt_sha256"]
    payload = json.dumps(
        {
            **identity,
            "prompt_sha256": prompt_digest,
            "prompt": rendered.text,
        },
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    if len(payload.encode()) > MAX_INPUT_BYTES:
        raise SourcePromptCapacity("required_source_context_exceeds_capacity")
    return SourceModelPrompt(payload, round_id, run_id, input_version, prompt_digest, stage)
