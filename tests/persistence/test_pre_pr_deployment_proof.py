"""Final PR completion requires the registered deployment validation."""

from copy import deepcopy
import json
from pathlib import Path

from robert_agent.persistence.rounds import pre_pr_review as store
from tests.support.pre_pr_review import PrePRReviewFixture


class DeploymentProofTests(PrePRReviewFixture):
    def test_opt_in_requires_sealed_deployment_evidence_for_the_final_sha(self):
        driver = self.start(["claude"], legacy=False)
        self.with_finding = True
        result = driver.run()
        self.assertTrue(result["ok"], result)
        with store.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
        proof = entry["proof"]
        self.assertNotEqual(proof["head_sha"], proof["candidate_sha"])
        self.assertTrue(store.matches_review_completion(entry, proof))
        plan = entry["request"]["plan"]
        plan["deployment_validation"] = {"worker": {"name": "codex"}, "skill_path": "/host/SKILL.md"}
        self.assertFalse(store.matches_review_completion(entry, proof))

        artifact_root = Path(entry["manifest"]["artifact_root"])
        with store.connect(self.db) as conn:
            store.update(conn, driver.round_id, request_json=json.dumps(entry["request"]))
            with self.assertRaisesRegex(ValueError, "deployment validation"):
                store.seal_result(conn, driver.round_id, proof)
            run = store.new_run(
                conn,
                driver.round_id,
                "validate_deployment",
                "codex",
                driver.attempt_id,
                artifact_root,
                "prompt",
            )
        run_id = run["run_id"]
        root = artifact_root / "validate_deployment"
        path = root / "sealed" / run_id / "deployment-validation.json"
        path.parent.mkdir(parents=True)
        identity = {
            "round_id": driver.round_id,
            "snapshot_id": entry["snapshot_id"],
            "head_sha": proof["head_sha"],
            "status": "passed",
        }
        path.write_text(json.dumps(identity))
        artifact = {
            **identity,
            "deployment_validation_path": str(path),
            "deployment_validation_sha256": store.digest(path),
        }
        dispatch = {
            "result_checkpoint": {
                "run_id": run_id,
                "prompt_sha256": "prompt",
                "snapshot_id": entry["snapshot_id"],
                "worktree_head": proof["head_sha"],
            }
        }
        proof["deployment_validation"] = {**artifact, "run_id": run_id, "worker_id": "codex"}
        with store.connect(self.db) as conn:
            store.update_run(
                conn,
                run_id,
                status="succeeded",
                artifact_json=json.dumps(artifact),
                dispatch_json=json.dumps(dispatch),
            )
            store.seal_result(conn, driver.round_id, proof)
            entry = store.session(conn, task_id=driver.task_id)
        run_index = next(index for index, item in enumerate(entry["completion_runs"]) if item["run_id"] == run_id)
        self.assertTrue(store.matches_review_completion(entry, proof))
        self.assertTrue(store.matches_submission(entry, driver.attempt_id, proof["fingerprint"]))

        for changed_field, value in (
            ("status", "failed"),
            ("slot_reserved", 1),
            ("worker_id", "claude"),
            ("output_dir", str(root / "unknown")),
            (
                "dispatch",
                {
                    "result_checkpoint": {
                        **dispatch["result_checkpoint"],
                        "worktree_head": proof["candidate_sha"],
                    }
                },
            ),
        ):
            with self.subTest(field=changed_field):
                changed = deepcopy(entry)
                changed["completion_runs"][run_index][changed_field] = value
                self.assertFalse(store.matches_review_completion(changed, proof))

        for status in ("failed", "blocked", "skipped"):
            with self.subTest(status=status):
                changed = deepcopy(entry)
                path.write_text(json.dumps({**identity, "status": status}))
                changes = {"status": status, "deployment_validation_sha256": store.digest(path)}
                changed["completion_runs"][run_index]["artifact"].update(changes)
                changed["proof"]["deployment_validation"].update(changes)
                self.assertEqual(store.matches_review_completion(changed, changed["proof"]), status == "skipped")

        path.write_text(json.dumps({**identity, "head_sha": proof["candidate_sha"]}))
        wrong_sha = deepcopy(entry)
        for record in (
            wrong_sha["completion_runs"][run_index]["artifact"],
            wrong_sha["proof"]["deployment_validation"],
        ):
            record["deployment_validation_sha256"] = store.digest(path)
        self.assertFalse(store.matches_review_completion(wrong_sha, wrong_sha["proof"]))
        self.assertFalse(store.matches_review_completion(entry, proof))

        other = path.with_name("copy.json")
        other.write_text(json.dumps(identity))
        path.unlink()
        path.symlink_to(other)
        self.assertFalse(store.matches_review_completion(entry, proof))
        path.unlink()
        other.rename(path)
        self.assertTrue(store.matches_review_completion(entry, proof))
        del entry["request"]["plan"]["deployment_validation"]
        self.assertFalse(store.matches_review_completion(entry, proof))
        with store.connect(self.db) as conn:
            store.update(conn, driver.round_id, request_json=json.dumps(entry["request"]))
            with self.assertRaisesRegex(ValueError, "deployment validation"):
                store.seal_result(conn, driver.round_id, proof)
