"""Causal selection, recovery, bounds and drift without any network or Cloud."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multipath_policy import MultipathSelector


class MultipathPolicyTests(unittest.TestCase):
    def test_initial_preference_and_failure_bypasses_four_paths(self):
        policy = MultipathSelector()
        now = 0
        selected = []
        for expected in "abcd":
            path = policy.select(now)
            selected.append(path)
            self.assertEqual(path, expected)
            now += 100
            policy.observe(path, False, 100, now)
        self.assertEqual(selected, list("abcd"))
        self.assertGreater(policy.retry_delay_ms(now), 0)
        self.assertEqual(policy.select(now), "a")
        self.assertEqual(policy.last_decision["reason"], "all_paths_cooling_retry_earliest")

    def test_timeout_is_censored_not_a_latency_sample(self):
        policy = MultipathSelector()
        policy.observe("a", True, 20, 20)
        before = policy.snapshot()["state"]["a"]
        policy.observe("a", False, 9000, 9020)
        after = policy.snapshot()["state"]["a"]
        self.assertEqual(before["srtt_ms"], after["srtt_ms"])
        self.assertEqual(before["rttvar_ms"], after["rttvar_ms"])
        self.assertGreater(after["timeout_ms"], before["timeout_ms"])
        self.assertNotEqual(policy.select(9020), "a")

    def test_hysteresis_avoids_flapping_between_similar_routes(self):
        policy = MultipathSelector(probe_every=100)
        policy.observe("a", True, 100, 100)
        policy.observe("b", True, 90, 190)
        self.assertEqual(policy.select(190), "a")
        self.assertEqual(policy.last_decision["reason"], "keep_path_with_hysteresis")

    def test_sustained_latency_degradation_changes_choice(self):
        policy = MultipathSelector(probe_every=100)
        policy.observe("a", True, 10, 10)
        policy.observe("b", True, 40, 50)
        self.assertEqual(policy.select(50), "a")
        for index in range(8):
            policy.observe("a", True, 300, 350 + index * 300)
        self.assertEqual(policy.select(2500), "b")

    def test_recovery_probes_cover_all_alternatives_and_are_bounded(self):
        policy = MultipathSelector(probe_every=5)
        probes, now = [], 0
        for step in range(31):
            path = policy.select(now)
            if policy.last_decision["reason"] == "bounded_recovery_probe":
                probes.append((step, path))
            now += 10
            policy.observe(path, True, 10 if path == "a" else 100, now)
        self.assertEqual({path for _, path in probes}, {"b", "c", "d"})
        self.assertEqual(len(probes), 6)
        self.assertTrue(all(right[0] - left[0] >= 5 for left, right in zip(probes, probes[1:])))

    def test_total_outage_then_one_recovered_route_resumes_without_schedule(self):
        policy = MultipathSelector(probe_every=5)
        now, attempts, recovery_choices = 0, {p: 0 for p in "abcd"}, []
        for index in range(70):
            path = policy.select(now)
            now += policy.last_decision["wait_ms"]
            attempts[path] += 1
            success = index >= 30 and path == "c"
            now += 10 if success else policy.request_timeout_ms(path)
            policy.observe(path, success, 10 if success else 500, now)
            if index >= 30 and success:
                recovery_choices.append(path)
        self.assertTrue(all(count > 1 for count in attempts.values()))
        self.assertGreater(len(recovery_choices), 20)
        self.assertEqual(set(recovery_choices), {"c"})

    def test_identical_completed_observations_make_identical_decisions(self):
        left, right = MultipathSelector(), MultipathSelector()
        now = 0
        for i in range(80):
            lp, rp = left.select(now), right.select(now)
            self.assertEqual(lp, rp)
            now += left.last_decision["wait_ms"] + 30
            success = i % 5 != 0
            left.observe(lp, success, 30, now)
            right.observe(rp, success, 30, now)
        self.assertEqual(left.snapshot(), right.snapshot())
        json.dumps(left.snapshot(), allow_nan=False)

    def test_timeout_and_cooldown_bounds_under_long_failure_streak(self):
        policy = MultipathSelector()
        for i in range(100):
            now = i * 10000
            policy.observe("a", False, 10000, now)
            row = policy.snapshot()["state"]["a"]
            self.assertLessEqual(row["cooldown_until_ms"] - now, 1500)
            self.assertTrue(150 <= policy.request_timeout_ms("a") <= 1000)
        policy.observe("a", True, 0, 1000000)
        self.assertEqual(policy.request_timeout_ms("a"), 150)

    def test_snapshot_and_observation_results_cannot_mutate_selector(self):
        policy = MultipathSelector()
        observed = policy.observe("a", True, 30, 30)
        observed["verified_acks"] = 999
        snapshot = policy.snapshot()
        snapshot["state"]["a"]["verified_acks"] = 999
        snapshot["limits"]["max_timeout_ms"] = 1
        self.assertEqual(policy.snapshot()["state"]["a"]["verified_acks"], 1)
        self.assertEqual(policy.limits["max_timeout_ms"], 1000)

    def test_rejects_invalid_input_and_backwards_clock(self):
        for paths in [("a",), ("a", "a"), ("a", "../../tmp"), ("a", 1)]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                MultipathSelector(paths)
        for kwargs in [dict(probe_every=True), dict(hysteresis=1), dict(initial_timeout_ms=1),
                       dict(cooldown_ms=float("nan")), dict(max_timeout_ms=float("inf"))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MultipathSelector(**kwargs)
        policy = MultipathSelector()
        for value in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.select(value)
        policy.select(100)
        with self.assertRaises(ValueError):
            policy.observe("a", True, 20, 90)
        with self.assertRaises(ValueError):
            policy.observe("a", 1, 20, 120)
        with self.assertRaises(ValueError):
            policy.observe("z", True, 20, 120)


if __name__ == "__main__":
    unittest.main()
