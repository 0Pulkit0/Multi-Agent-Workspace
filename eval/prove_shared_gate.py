"""Report-item-6 demonstration: one suite, one Test Writer call, every arm.

Runs the vacuity path offline -- the Planner's TESTS block is rejected by the
audit, so `_resolve_tests` regenerates -- and prints, for one task:

  * the Test Writer call count for the whole task,
  * the gate suite hash recorded by `a_prime`, `a_prime3` and `b`,
  * the same numbers for the pre-fix path (plan text only, no injected suite),
    which is what arm B used to do.

    python3 eval/prove_shared_gate.py [runs_dir]

No keys, no network. `runs_dir` defaults to a scratch directory that is left in
place so the run JSON can be inspected; nothing is written to the app's `runs/`.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import agents_core                                              # noqa: E402
import gen_tasks                                                # noqa: E402
import run_eval                                                 # noqa: E402


class Counting(object):
    """`call_model`, with the role of every call recorded.

    ``drift`` makes the Test Writer's second and later answers differ from its
    first by a comment line. A real Test Writer at temperature 0.2 does not
    return byte-identical suites twice; the stub does, which would make the
    pre-fix path look harmless here for a reason that has nothing to do with the
    defect. Any difference at all -- a comment, a reordered assert, one more
    case -- is enough to put two arms on two different gates.
    """

    def __init__(self, stub, drift=False):
        self.stub = stub
        self.drift = drift
        self.roles = []

    def __call__(self, provider, api_key, system, user):
        role = self.stub._role(system)
        self.roles.append(role)
        reply = self.stub(provider, system, user)
        if self.drift and role == "test_writer":
            nth = self.roles.count("test_writer")
            if nth > 1:
                reply = reply.replace(
                    "```\n", "# sampled again (call %d)\n```\n" % nth, 1)
        return reply

    def count(self, role, since=0):
        return self.roles[since:].count(role)


def main(argv):
    runs_dir = argv[0] if argv else tempfile.mkdtemp(prefix="shared-gate-")
    memory_original = agents_core.Memory

    class ScopedMemory(memory_original):
        def __init__(self, *args, **kwargs):
            kwargs["dirpath"] = runs_dir
            memory_original.__init__(self, *args, **kwargs)

    agents_core.Memory = ScopedMemory
    agents_core.set_measurement_mode(True)
    task = gen_tasks.generate(seed=0, per_family=1, families={"aggregation"},
                              tiers=set(), limit=1)[0]
    keys = {"gemini": "stub", "groq": "stub"}

    stub = run_eval.StubModel("perfect", vacuous_plan=True)
    stub.task = task
    counter = Counting(stub)
    agents_core.call_model = counter

    print("task                    : %s (%s, tier %d)"
          % (task.task_id, task.family, task.tier))
    plan = run_eval.make_plan(task, keys)
    print("planner's own suite     : rejected by the audit (vacuous)")
    print("plan tests_status       : %s" % plan["tests_status"])
    print("shared suite sha256     : %s" % plan["tests_sha256"])
    print("calls to make the plan  : %s" % (counter.roles,))

    hashes = {}
    for arm in run_eval.PLAN_ARMS:
        start = len(counter.roles)
        outcome = run_eval.ARMS[arm][0](task, keys, plan)
        hashes[arm] = outcome["gate_tests_sha256"]
        print("%-9s gate sha256   : %s   (its own calls: %s)"
              % (arm, outcome["gate_tests_sha256"], counter.roles[start:]))
    print("all three arms equal    : %s" % (len(set(hashes.values())) == 1))
    print("equal to the shared one : %s"
          % all(value == plan["tests_sha256"] for value in hashes.values()))
    print("Test Writer calls, task : %d" % counter.count("test_writer"))

    print("\n-- the pre-fix path, for contrast: plan text only, no tests= --")
    for drift in (False, True):
        stub = run_eval.StubModel("perfect", vacuous_plan=True)
        stub.task = task
        counter = Counting(stub, drift=drift)
        agents_core.call_model = counter
        plan = run_eval.make_plan(task, keys)
        old = agents_core.run_workspace(task.prompt, keys, mode=3,
                                        plan=plan["plan"])
        old_hash = run_eval._suite_hash(old.tests)
        print("Test Writer %s:"
              % ("that samples (a real one)" if drift else "deterministic (the stub)"))
        print("    A'@3 gate sha256    : %s" % plan["tests_sha256"])
        print("    arm B gate sha256   : %s" % old_hash)
        print("    same bytes          : %s" % (old_hash == plan["tests_sha256"]))
        print("    arm B tests_status  : %s" % old.tests_status)
        print("    Test Writer calls   : %d (one task)"
              % counter.count("test_writer"))
    print("\nrun JSON under          : %s" % runs_dir)
    agents_core.Memory = memory_original
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
