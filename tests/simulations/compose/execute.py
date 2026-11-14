"""Run a catalog entry inside the installed-wheel container."""

import argparse
import json
import time
import traceback

from bootstrap import STATE, prepare, write_json
from catalog import CASES
from client import fixture
from driver import HarnessError, World
from evidence import abort_barriers, capture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id", choices=[case.id for case in CASES])
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    case = next(case for case in CASES if case.id == args.case_id)
    started = time.monotonic()
    outcome = "PASS"
    detail = None
    transition = None
    try:
        world = World(case.id)
        world.wait("fixture HTTP readiness", lambda: fixture("/health"), timeout=15, error=HarnessError)
        if args.resume:
            world.scenario_id = world.remote()["scenario_id"]
            if case.resume is None:
                raise HarnessError("this case cannot resume")
            case.resume(world)
        else:
            prepare(case.id)
            transition = case.run(world)
        state = world.remote()
        missed = [row["rule_id"] for row in state["faults"] if row["rule_id"] in world.rule_ids and not row["hits"]]
        if missed:
            raise HarnessError(f"armed faults did not fire: {missed}")
    except AssertionError:
        outcome = "PRODUCT_FAILURE"
        detail = traceback.format_exc()
    except Exception:
        outcome = "HARNESS_FAILURE"
        detail = traceback.format_exc()
    try:
        failures = capture()
        if transition is None or outcome != "PASS":
            abort_barriers()
        if failures:
            raise HarnessError(f"fixture or evidence failures: {failures}")
    except Exception:
        detail = (detail or "") + traceback.format_exc()
        outcome = "HARNESS_FAILURE"
    result = {"case_id": case.id, "outcome": outcome, "detail": detail, "seconds": round(time.monotonic() - started, 3)}
    if transition is not None and outcome == "PASS":
        result.update(outcome="HOST_TRANSITION", transition=transition)
    write_json(STATE / "result.json", result)
    print(json.dumps(result))
    return 0 if outcome == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
