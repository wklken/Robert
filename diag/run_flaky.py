"""Temporary diagnostic: repeat macOS-flaky termination tests and log every identity decision."""
import collections, json, sys, traceback, unittest
sys.path.insert(0, "src"); sys.path.insert(0, ".")
from robert_agent import worker_identity, model_execution

LOG = []
orig_ps = worker_identity.read_ps_identity
def read_ps_identity(pid, *, ps_runner=None):
    import subprocess
    raw = subprocess.run(["ps", "-p", str(pid), "-o", "pid=,stat=,pgid=,lstart=,command="], capture_output=True, text=True)
    try:
        out = orig_ps(pid, ps_runner=ps_runner)
        LOG.append(("ps", pid, raw.returncode, raw.stdout.strip()[:200], raw.stderr.strip()[:120], out.get("process_state"), out.get("command_fingerprint", "")[:8]))
        return out
    except Exception as exc:
        LOG.append(("ps_err", pid, raw.returncode, raw.stdout.strip()[:200], raw.stderr.strip()[:120], type(exc).__name__))
        raise
worker_identity.read_ps_identity = read_ps_identity

orig_obs = worker_identity._observe_recorded_identity
def observe(recorded, **kw):
    r = orig_obs(recorded, **kw); LOG.append(("inspect", recorded.get("pid"), recorded.get("identity_source"), recorded.get("command_fingerprint", "")[:8], r)); return r
worker_identity._observe_recorded_identity = observe

orig_term = worker_identity.terminate_recorded_identity
def term(recorded, **kw):
    r = orig_term(recorded, **kw); LOG.append(("terminate", recorded.get("pid"), r)); return r
worker_identity.terminate_recorded_identity = term

orig_wait = model_execution._wait_for_group_exit
def wait(recorded, timeout=2):
    import os
    r = orig_wait(recorded, timeout)
    if not r:
        try: os.killpg(recorded.get("process_group_id", recorded.get("pid")), 0); alive = "killpg_ok"
        except Exception as exc: alive = type(exc).__name__
        try: wp = os.waitpid(recorded.get("pid"), os.WNOHANG)
        except Exception as exc: wp = type(exc).__name__
        LOG.append(("group_wait_timeout", recorded.get("pid"), alive, str(wp)))
    else:
        LOG.append(("group_wait_ok", recorded.get("pid")))
    return r
model_execution._wait_for_group_exit = wait

TESTS = [
    "tests.test_vote_processes.VoteProcessTests.test_cancel_stops_both_registered_voters_and_does_not_seal_votes",
    "tests.test_lifecycle_remediation.LifecycleRemediationTests.test_remote_close_stops_registered_native_models",
]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
summary = collections.Counter()
for name in TESTS:
    for i in range(N):
        LOG.clear()
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromName(name).run(result)
        ok = result.wasSuccessful()
        summary[(name.rsplit(".", 1)[1], ok)] += 1
        if not ok:
            print(f"\n===== FAIL {name} iter {i}")
            for _, tb in result.failures + result.errors:
                print(tb.strip().splitlines()[-1])
            for entry in LOG:
                print("  ", entry)
print("\nSUMMARY", json.dumps({f"{k[0]}:{'ok' if k[1] else 'FAIL'}": v for k, v in summary.items()}))
