"""Controlled source coordinator child for real process-lifetime integration tests."""

import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
from hashlib import sha256
from unittest.mock import patch

from robert_agent.adapters.processes.identity import capture_process_identity
from robert_agent.adapters.processes.source_model import SourceModelOutcome, SourceModelProfile
from robert_agent.application.source_cycle import advance_source
from robert_agent.execution.application import source_models
from robert_agent.foundation import values
from robert_agent.foundation.commands import emit
from robert_agent.work import tapd_authority as facts


def controlled_model(profile, prompt, stop, started):
    command = [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(25)"]
    with subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, text=True) as process:
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            if not ready or process.stdout.readline().strip() != "ready":
                raise RuntimeError("Controlled model did not finish interpreter startup")
            identity = capture_process_identity(process.pid)
            if not started(identity):
                raise RuntimeError("Controlled model launch was not recorded")
            while not stop.is_set():
                if process.poll() is not None:
                    raise RuntimeError("Controlled model exited before cancellation")
                stop.wait(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        return SourceModelOutcome("stopped", exit_kind="group_exited", identity=identity)


def main(argv=None):
    root_arg, comment_created = sys.argv[1:] if argv is None else argv
    root = Path(root_arg)
    ticket = facts.TicketIdentity("tapd.example", "001", "bug", "90001")
    body = "@test-helper Please investigate 1"
    version = facts.CommentVersion(ticket, "bug", "1", comment_created, None, sha256(body.encode()).hexdigest())
    comment = facts.CommentObservation(
        version,
        body,
        "requester",
        facts.CreatorEvidence(version, "requester", "creator"),
        facts.OwnBodyEvidence(version, "requester", "body"),
    )

    def provider(client, identity, **kwargs):
        observed = facts.TicketFacts(identity, "Ticket", "Complete frozen ticket", "open", (), (), False, ("closed",))
        return facts.TicketObservation(observed, values.utc_now(), (comment,), "complete")

    profile = SourceModelProfile("mock-model", Path("/controlled/claude"), "model", "high", 10, ())
    with (
        patch.object(source_models, "RECHECK_SECONDS", 0),
        patch.object(source_models, "select_worker", return_value=profile),
    ):
        result = advance_source(
            root / "state.sqlite",
            root,
            json.loads((root / "normalized.json").read_text()),
            owner="test-coordinator",
            ticket=ticket,
            client_factory=lambda **kwargs: object(),
            observation_provider=provider,
            model_runner=controlled_model,
        )
    return emit({"ok": result.status == "draining", "status": result.status})


if __name__ == "__main__":
    raise SystemExit(main())
