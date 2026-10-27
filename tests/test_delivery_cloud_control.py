"""Network-program Cloud helper tests with in-memory SDK fakes only."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("network_cloud_control_under_test", ROOT / "cloud_control.py")
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)
SPACE, ENTITY, JOB = "mem_test", "ent_" + "1" * 32, "job_" + "2" * 32


def job(completed=False):
    return {"job_id": JOB, "entity_id": ENTITY, "status": "completed" if completed else "pending",
            "accepted_count": 1, "completed_count": int(completed), "pending_count": int(not completed),
            "processing_count": 0, "dead_letter_count": 0}


def recall(record):
    return {"schema": "basira-tsst-evidence/v1", "memory_space_id": SPACE, "entity_id": ENTITY, "count": 2,
            "evidence": [{"evidence_id": "ev_" + "a" * 32, "excerpt": control.canonical(record), "integrity_verified": True},
                         {"evidence_id": "ev_" + "b" * 32, "excerpt": "synthetic-secret-unrelated-source", "integrity_verified": True}]}


def ranking():
    return {"schema": "basira-odvl-ranking/v1", "memory_space_id": SPACE, "entity_id": ENTITY,
            "count": 2, "ranking_applied": True, "reordered": True,
            "ranking": [{"candidate_id": "b", "rank": 1, "score": .2},
                        {"candidate_id": "a", "rank": 2, "score": .7}]}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rank_requires_exact_ids_scores_types_scope_and_order(self):
        candidates = [{"candidate_id": "a", "score": .7}, {"candidate_id": "b", "score": .2}]
        self.assertEqual(control.validate_rank(ranking(), candidates, SPACE, ENTITY), ["b", "a"])
        bad = []
        for change in ({"entity_id": "ent_" + "9" * 32}, {"count": True}, {"reordered": False}):
            bad.append({**ranking(), **change})
        value = ranking(); value["ranking"][0]["score"] = .8; bad.append(value)
        value = ranking(); value["ranking"][0]["rank"] = True; bad.append(value)
        value = ranking(); value["ranking"][0]["candidate_id"] = "a"; bad.append(value)
        value = ranking(); value["ranking"].append({"candidate_id": "x", "rank": 3, "score": .4}); bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.validate_rank(value, candidates, SPACE, ENTITY)
        with self.assertRaises(ValueError):
            control.validate_rank(ranking(), candidates + [candidates[0]], SPACE, ENTITY)

    def test_job_scope_and_completed_counters_are_required(self):
        self.assertEqual(control.validate_job(job(), ENTITY)["status"], "pending")
        self.assertEqual(control.validate_job(job(True), ENTITY, completed=True, expected_id=JOB), job(True))
        for value in (job(), {**job(True), "completed_count": True}, {**job(True), "entity_id": "ent_" + "9" * 32},
                      {**job(True), "completed_count": 0}, {**job(True), "job_id": "job_" + "8" * 32}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.validate_job(value, ENTITY, completed=True, expected_id=JOB)

    def test_literal_recall_excludes_unrelated_text_and_unverified_or_truncated_records(self):
        record = {"schema": "civil-lab-summary/v1", "run_id": "test-run", "summary": {"received": 16}}
        response = recall(record)
        proof = control.literal_recall_proof(response, record, SPACE, ENTITY)
        self.assertTrue(proof["literal_record_recalled"])
        self.assertEqual(proof["matching_evidence_ids"], ["ev_" + "a" * 32])
        self.assertNotIn("synthetic-secret-unrelated-source", json.dumps(proof))
        self.assertNotIn("excerpt", proof["matching_evidence"][0])
        for mutation in ({"integrity_verified": False}, {"excerpt": control.canonical(record)[:-2]}):
            changed = copy.deepcopy(response); changed["evidence"][0].update(mutation)
            self.assertFalse(control.literal_recall_proof(changed, record, SPACE, ENTITY)["literal_record_recalled"])

    def test_recall_bad_scope_count_or_duplicate_ids_fail_closed(self):
        record = {"run_id": "test-run"}
        for change in ({"entity_id": "ent_" + "9" * 32}, {"count": True}, {"count": 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                control.literal_recall_proof({**recall(record), **change}, record, SPACE, ENTITY)
        response = recall(record); response["evidence"][1]["evidence_id"] = response["evidence"][0]["evidence_id"]
        with self.assertRaises(ValueError): control.literal_recall_proof(response, record, SPACE, ENTITY)

    def test_policy_is_typed_bounded_json_not_executable_instructions(self):
        policy = {"schema": "civil-queue-policy/v1", "ewma_alpha": .35}
        self.assertEqual(control.parse_policy("```json\n" + json.dumps(policy) + "\n```", "network"), policy)
        for value in ({**policy, "command": "arbitrary text"}, {**policy, "ewma_alpha": True},
                      {**policy, "ewma_alpha": 0.99}, {**policy, "ewma_alpha": float("nan")}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.parse_policy(json.dumps(value), "network")
        with self.assertRaises(ValueError): control.parse_policy(json.dumps(policy), "typo")
        with self.assertRaises(ValueError): control.parse_policy('Result: ' + json.dumps(policy), "network")
        with self.assertRaises(ValueError): control.parse_policy('{"schema":"civil-queue-policy/v1","ewma_alpha":0.3,"ewma_alpha":0.4}', "network")
        delivery = {"schema": "resilience-retry-policy/v1", "max_attempts": 12, "base_delay_ms": 100,
                    "max_delay_ms": 1000, "request_timeout_ms": 500, "total_budget_ms": 25000}
        self.assertEqual(control.parse_policy(json.dumps(delivery), "delivery"), delivery)
        with self.assertRaises(ValueError): control.parse_policy(json.dumps({**delivery, "max_delay_ms": 50}), "delivery")

    def test_save_is_private_atomic_and_does_not_follow_predictable_temp_symlink(self):
        path = self.root / "report.json"
        untouched = self.root / "untouched.txt"; untouched.write_text("keep")
        path.with_suffix(".json.tmp").symlink_to(untouched)
        control.save(path, {"completed": False})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(untouched.read_text(), "keep")
        control.save(path, {"completed": True})
        self.assertTrue(json.loads(path.read_text())["completed"])

    def test_remember_recall_validates_fakes_and_only_passes_own_summary_to_prompt(self):
        campaign = control.Campaign.__new__(control.Campaign)
        campaign.space, campaign.entity, campaign.run_id, campaign.path = SPACE, ENTITY, "civil-unit-test", self.root
        campaign.report = {"calls": []}
        class SDK:
            def remember(self, entity, content, **kwargs):
                self.record = json.loads(content)
                return job()
            def wait_job(self, *_args, **_kwargs): return job(True)
            def recall_tsst(self, *_args, **_kwargs): return recall(self.record)
        campaign.client = SDK()
        result = campaign.remember_recall("calibration", {"messages": 16, "errors": 0})
        self.assertEqual(result["summary"]["messages"], 16)
        self.assertEqual(len(campaign.report["calls"]), 3)
        self.assertNotIn("synthetic-secret-unrelated-source", (self.root / "cloud-report.json").read_text())

    def test_main_persists_failed_policy_and_never_prints_raw_exception(self):
        campaign = SimpleNamespace(report={"calls": [], "completed": False}, path=self.root,
                                   client=Mock(), run_id="civil-test")
        args = ["prepare", "--kind", "network", "--credentials", "unused", "--entity", ENTITY,
                "--summary", "unused", "--workloads", "unused", "--output", str(self.root), "--run"]
        stdout = io.StringIO()
        with patch.object(control, "Campaign", return_value=campaign), \
                patch.object(control, "prepare", side_effect=ValueError("synthetic-secret-in-error")), redirect_stdout(stdout):
            self.assertEqual(control.main(args), 1)
        self.assertFalse(json.loads((self.root / "cloud-report.json").read_text())["completed"])
        self.assertNotIn("synthetic-secret-in-error", stdout.getvalue())
        campaign.client.close.assert_called_once()

    def test_close_failure_cannot_overwrite_success(self):
        client = Mock(); client.close.side_effect = RuntimeError("synthetic-secret-in-close")
        campaign = SimpleNamespace(report={"calls": []}, path=self.root, client=client, run_id="civil-test")
        args = ["prepare", "--kind", "network", "--credentials", "unused", "--entity", ENTITY,
                "--summary", "unused", "--workloads", "unused", "--output", str(self.root), "--run"]
        with patch.object(control, "Campaign", return_value=campaign), patch.object(control, "prepare"), redirect_stdout(io.StringIO()):
            self.assertEqual(control.main(args), 0)


if __name__ == "__main__":
    unittest.main()
