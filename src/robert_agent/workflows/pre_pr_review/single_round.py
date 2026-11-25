"""Single-round limits and deterministic completion evidence."""

from pathlib import Path

from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.rounds.records import artifact_available


POLICY = "single_review_single_repair"
PHASES = {"review", "synthesis", "address_findings"}


def enabled(driver):
    return getattr(driver, "request", {}).get("plan", {}).get("review_policy") == POLICY


def review_started(driver):
    if not enabled(driver):
        return False
    with store.connect(driver.db_path) as conn:
        return any(run["stage"] in PHASES for run in store.runs(conn, driver.round_id))


def preserve_snapshot(driver):
    if review_started(driver):
        raise StageStopped(
            "base or original context changed after single-round review started; "
            "preserve this checkpoint and reconcile the affected changes before a new authorized task"
        )


def completion(driver, report_sha, disposition):
    """Name existing executions; persistence independently checks their evidence."""
    with store.connect(driver.db_path) as conn:
        history = store.runs(conn, driver.round_id)

    def completed(stage, worker):
        selected = [
            run for run in history if run["stage"] == stage and run["worker_id"] == worker and artifact_available(run)
        ]
        if len(selected) != 1:
            raise StageStopped("single-round completion requires exactly one registered " + stage + " artifact")
        return selected[0]

    reviews = [completed("review", worker["name"]) for worker in driver.plan["workers"]]
    synthesis = completed("synthesis", driver.plan["workers"][0]["name"]) if len(reviews) > 1 else None
    address = completed("address_findings", driver.implementer["name"])
    if address["run_id"] != disposition.get("run_id"):
        raise StageStopped("single-round address source changed")
    if store.digest(Path(disposition["dispositions_path"])) != disposition["dispositions_sha256"]:
        raise StageStopped("single-round dispositions changed")
    return {
        "version": 1,
        "round_id": driver.round_id,
        "snapshot_id": driver.manifest["snapshot_id"],
        "candidate_sha": driver.manifest["head_sha"],
        "final_sha": disposition["head"],
        "report_sha256": report_sha,
        "dispositions_sha256": disposition["dispositions_sha256"],
        "review_run_ids": [run["run_id"] for run in reviews],
        "synthesis_run_id": synthesis["run_id"] if synthesis else None,
        "address_run_id": address["run_id"],
    }
