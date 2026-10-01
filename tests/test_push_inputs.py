import importlib.util
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("push_inputs", ROOT / "scripts/push_inputs.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
HEAD, PARENT, OTHER = "a" * 40, "b" * 40, "c" * 40


def success(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def failure(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class PushInputsTests(unittest.TestCase):
    def execute(self, responses):
        calls, delays = [], []
        def runner(command):
            calls.append(command)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        publisher.push_inputs(runner=runner, sleep=delays.append)
        return calls, delays

    def test_commit_refs_failure_retries_the_identical_commit_after_observation(self):
        calls, delays = self.execute([success(HEAD), success(PARENT),
            failure("remote: fatal error in commit_refs"), success(PARENT + "\trefs/heads/main"), success()])
        pushes = [command for command in calls if command[:2] == ["git", "push"]]
        self.assertEqual(pushes, [["git", "push", "origin", "HEAD:refs/heads/main"]] * 2)
        self.assertEqual(delays, [1])

    def test_lost_push_response_with_accepted_commit_is_not_replayed(self):
        calls, delays = self.execute([success(HEAD), success(PARENT),
            subprocess.TimeoutExpired("git push", 300), success(HEAD + "\trefs/heads/main")])
        self.assertEqual(sum(command[:2] == ["git", "push"] for command in calls), 1)
        self.assertEqual(delays, [])

    def test_permission_failure_is_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            self.execute([success(HEAD), success(PARENT), failure("permission denied")])

    def test_concurrent_branch_update_is_not_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "advanced"):
            self.execute([success(HEAD), success(PARENT), failure("fatal error in commit_refs"),
                          success(OTHER + "\trefs/heads/main")])

    def test_retry_budget_is_bounded(self):
        responses = [success(HEAD), success(PARENT)]
        for _ in range(4):
            responses += [failure("fatal error in commit_refs"), success(PARENT + "\trefs/heads/main")]
        with self.assertRaisesRegex(RuntimeError, "four attempts"):
            self.execute(responses)


if __name__ == "__main__":
    unittest.main()
