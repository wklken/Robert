"""Build the wheel image and run selected lifecycle tests in isolated projects."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

from catalog import CASES


DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[2]


def select(args):
    if args.scenario:
        result = [case for case in CASES if case.id == args.scenario or case.id.startswith(args.scenario + ".")]
    elif args.suite == "smoke":
        result = [case for case in CASES if case.smoke]
    else:
        result = [case for case in CASES if case.suite == args.suite]
    if not result:
        raise ValueError("no executable scenarios match the selection")
    return [
        case
        for case in result
        if int(hashlib.sha256(case.id.encode()).hexdigest(), 16) % args.shard_count == args.shard_index
    ]


def project_command(args, image, project):
    command = [*shlex.split(args.compose), "-f", str(DIRECTORY / "compose.yml"), "-p", project]
    environment = {**os.environ, "ROBERT_COMPOSE_IMAGE": image}

    def run(*argv, timeout=60, check=True, **kwargs):
        return subprocess.run(
            [*command, *argv], cwd=ROOT, env=environment, timeout=timeout, check=check, text=True, **kwargs
        )

    return run


def run_case(args, image, case, artifact):
    project = "robert-compose-" + uuid4().hex[:16]
    artifact.mkdir(parents=True)
    run = project_command(args, image, project)
    outcome = {"case_id": case.id, "outcome": "HARNESS_FAILURE", "project": project}
    deadline = time.monotonic() + case.timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise subprocess.TimeoutExpired(case.id, case.timeout)
        return budget

    try:
        run("up", "-d", "--no-build", timeout=min(90, remaining()))
        child = run(
            "exec",
            "-T",
            "sut",
            "python",
            "/harness/execute.py",
            case.id,
            timeout=remaining(),
            check=False,
            capture_output=True,
        )
        (artifact / "driver.log").write_text(child.stdout + child.stderr)
        try:
            outcome.update(json.loads(child.stdout.splitlines()[-1]))
        except (IndexError, ValueError):
            outcome["detail"] = f"driver exited {child.returncode} without a result"
        if outcome["outcome"] == "HOST_TRANSITION":
            transition = outcome["transition"]
            if transition not in {"kill-recreate", "stop-recreate", "stop-stack"}:
                raise RuntimeError(f"unsupported host transition {transition}")
            old = run("ps", "--all", "--quiet", "sut", capture_output=True).stdout.strip()
            if not old:
                raise RuntimeError("could not identify container to stop")
            with (artifact / "pre-transition.tar.gz").open("wb") as evidence:
                run("exec", "-T", "sut", "python", "/harness/export.py", stdout=evidence, timeout=20)
            if transition == "kill-recreate":
                run("kill", "-s", "SIGKILL", "sut")
            else:
                run("stop", "--timeout", "25", "sut")
            stopped = run("ps", "--all", "--status", "exited", "--quiet", "sut", capture_output=True).stdout.strip()
            if stopped != old:
                raise RuntimeError(f"old container did not exit: {old}")
            if transition == "stop-stack":
                run("stop", "--timeout", "10", "fixtures")
                description = run("ps", "--all", "--format", "json", capture_output=True).stdout.strip()
                if description.startswith("["):
                    services = json.loads(description)
                else:
                    services = [json.loads(line) for line in description.splitlines()]
                if {service["Service"] for service in services} != {"sut", "fixtures"} or any(
                    service["State"] != "exited" or service["ExitCode"] not in {0, 143} for service in services
                ):
                    raise RuntimeError(f"stack did not exit on TERM: {services}")
                outcome["service_exit"] = {service["Service"]: service["ExitCode"] for service in services}
                outcome["outcome"] = "PASS"
            else:
                run("up", "-d", "--no-deps", "--no-build", "--force-recreate", "sut")
                new = run("ps", "--quiet", "sut", capture_output=True).stdout.strip()
                if not new or new == old:
                    raise RuntimeError("Compose did not create a distinct SUT container")
                resumed = run(
                    "exec",
                    "-T",
                    "sut",
                    "python",
                    "/harness/execute.py",
                    case.id,
                    "--resume",
                    timeout=remaining(),
                    check=False,
                    capture_output=True,
                )
                (artifact / "resume.log").write_text(resumed.stdout + resumed.stderr)
                outcome.update(json.loads(resumed.stdout.splitlines()[-1]))
                if resumed.returncode != 0 and outcome["outcome"] == "PASS":
                    outcome["outcome"] = "HARNESS_FAILURE"
        elif child.returncode != 0 and outcome["outcome"] == "PASS":
            outcome["outcome"] = "HARNESS_FAILURE"
    except subprocess.TimeoutExpired:
        outcome.update(outcome="HARNESS_FAILURE", detail=f"execution exceeded its {case.timeout}s deadline")
    except (OSError, ValueError, IndexError, RuntimeError, subprocess.CalledProcessError) as exc:
        outcome["outcome"] = "HARNESS_FAILURE"
        outcome["detail"] = str(exc)
    finally:
        try:
            if not (artifact / "driver.log").exists():
                run("exec", "-T", "sut", "python", "/harness/evidence.py", check=False, timeout=12, capture_output=True)
            with (artifact / "compose.log").open("w") as logs:
                run("logs", "--no-color", stdout=logs, stderr=subprocess.STDOUT, check=False, timeout=12)
            if outcome.get("transition") != "stop-stack" or not (artifact / "pre-transition.tar.gz").exists():
                with (artifact / "state.tar.gz").open("wb") as evidence:
                    run("exec", "-T", "sut", "python", "/harness/export.py", stdout=evidence, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            outcome["evidence_error"] = str(exc)
            if outcome["outcome"] == "PASS":
                outcome["outcome"] = "HARNESS_FAILURE"
        if args.keep_on_failure and outcome["outcome"] != "PASS":
            print(
                f"Retained {project}. Cleanup: ROBERT_COMPOSE_IMAGE={image} {args.compose} -f {DIRECTORY / 'compose.yml'} -p {project} down -v"
            )
        else:
            try:
                run("down", "-v", "--remove-orphans", timeout=35)
                remaining = run("ps", "--all", "--quiet", capture_output=True).stdout.strip()
                if remaining:
                    raise RuntimeError(f"project still has containers: {remaining}")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                outcome.update(outcome="HARNESS_FAILURE", cleanup_error=str(exc))
    (artifact / "result.json").write_text(json.dumps(outcome, indent=2))
    print(f"{case.id} {outcome['outcome']} ({artifact})", flush=True)
    return outcome


def report(root, results):
    (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    suite = ET.Element(
        "testsuite",
        name="robert-compose",
        tests=str(len(results)),
        failures=str(sum(r["outcome"] != "PASS" for r in results)),
    )
    for result in results:
        case = ET.SubElement(suite, "testcase", name=result["case_id"], time=str(result.get("seconds", 0)))
        if result["outcome"] != "PASS":
            ET.SubElement(case, "failure", type=result["outcome"]).text = result.get("detail", "")
    ET.ElementTree(suite).write(root / "junit.xml", encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--list", action="store_true")
    selection.add_argument("--suite", choices=["smoke", "core", "extended", "slow"], default="smoke")
    selection.add_argument("--scenario")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--keep-on-failure", action="store_true")
    parser.add_argument("--compose", default="docker compose")
    parser.add_argument("--image", help="Use an existing locally built wheel image")
    args = parser.parse_args()
    if not 0 <= args.shard_index < args.shard_count or args.repeat < 1:
        parser.error("require positive repeat and 0 <= shard-index < shard-count")
    if args.list:
        for case in CASES:
            print(f"{case.id}\t{case.suite}\t{case.description}")
        return 0
    try:
        cases = select(args)
    except ValueError as exc:
        parser.error(str(exc))
    image = args.image or "robert-compose:" + uuid4().hex[:16]
    root = (args.artifacts or Path(tempfile.mkdtemp(prefix="robert-compose-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        parser.error(f"artifact directory must be empty: {root}")
    print(f"Artifacts: {root}", flush=True)
    bootstrap = project_command(args, image, "robert-compose-build")
    try:
        bootstrap("version")
        bootstrap("config", "--quiet")
        if not args.image:
            bootstrap("build", "sut", timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Compose infrastructure precondition failed: {exc}\n")
    manifest = {
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "image": image,
        "suite": None if args.scenario else args.suite,
        "cases": [case.id for case in cases],
        "shard": [args.shard_index, args.shard_count],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    results = []
    for repetition in range(args.repeat):
        for case in cases:
            results.append(run_case(args, image, case, root / f"{repetition + 1:02d}-{case.id}"))
            report(root, results)
    return int(any(result["outcome"] != "PASS" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
