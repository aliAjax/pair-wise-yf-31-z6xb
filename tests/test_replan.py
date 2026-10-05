import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow  # noqa: E402


class ReplanFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.svc = AirlineRecoveryService(self.db_path)
        self.base = utcnow() + timedelta(days=1)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC3", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=12)), "max_duty_minutes": 1500})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR3", "name": "丙组", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(self.base - timedelta(days=1)), "valid_to": iso(self.base + timedelta(days=2))})

    def tearDown(self):
        self.tmp.cleanup()

    def flight(self, number, aircraft="AC1", crew="CR1", hours=0):
        return self.svc.create_flight("sched", "scheduler", {
            "flight_no": number, "origin": "AAA", "destination": "BBB",
            "std": iso(self.base + timedelta(hours=hours)),
            "sta": iso(self.base + timedelta(hours=hours + 2)),
            "aircraft_id": aircraft, "crew_id": crew, "passenger_count": 150,
        })

    def disrupt(self, kind, resource, start_h, end_h):
        return self.svc.create_disruption("sched", "scheduler", {
            "kind": kind, "resource_id": resource,
            "starts_at": iso(self.base + timedelta(hours=start_h)),
            "ends_at": iso(self.base + timedelta(hours=end_h)),
        })

    def plan_and_lock(self, disruption, assignment, name="初版方案"):
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": name, "assignments": [assignment]})
        self.assertTrue(self.svc.validate_plan(plan["id"], "sched", "scheduler")["valid"])
        return self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})

    def test_new_disruption_invalidates_locked_plan_until_replan_published(self):
        f1 = self.flight("AB200")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        locked = self.plan_and_lock(d1, {
            "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)),
            "new_sta": iso(self.base + timedelta(hours=5)),
        })
        # A freshly registered fault now overlaps the locked recovery window.
        self.disrupt("aircraft_fault", "AC2", 3, 6)
        check = self.svc.validate_plan(locked["id"], "auditor", "auditor")
        self.assertFalse(check["valid"])
        self.assertIn("aircraft_fault_active", {p["code"] for p in check["problems"]})

        status, draft = self.svc.replan_plan(locked["id"], "sched", "scheduler", {})
        self.assertEqual(status, 201)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["parent_plan_id"], locked["id"])
        old = self.svc.get_plan(locked["id"])
        self.assertEqual(old["status"], "superseded")
        self.assertIsNotNone(old["frozen_at"])
        self.assertIsNone(old["metrics_json"])
        # Frozen historical version cannot be re-locked or published.
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(locked["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "plan_superseded")
        # Draft was seeded from current flight state (the old plan's writeback).
        seeded = draft["assignments"][0]
        self.assertIsNotNone(seeded["affected_by_json"])
        self.assertEqual(seeded["aircraft_id"], "AC2")
        self.assertEqual(seeded["new_std"], iso(self.base + timedelta(hours=3)))
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(draft["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "plan_invalid")

        # Scheduler resolves the affected assignment onto a healthy resource.
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": 1, "flight_id": f1["id"], "aircraft_id": "AC3", "crew_id": "CR3",
            "new_std": iso(self.base + timedelta(hours=3, minutes=30)),
            "new_sta": iso(self.base + timedelta(hours=5, minutes=30)),
        })
        self.assertTrue(self.svc.validate_plan(draft["id"], "sched", "scheduler")["valid"])
        published = self.svc.lock_plan(draft["id"], "ops", "ops_manager", {"expected_revision": 2})
        self.assertEqual(published["status"], "locked")
        live = next(f for f in self.svc.state()["flights"] if f["id"] == f1["id"])
        self.assertEqual(live["aircraft_id"], "AC3")
        self.assertEqual(live["crew_id"], "CR3")
        self.assertEqual(live["std"], iso(self.base + timedelta(hours=3, minutes=30)))
        # Cumulative delay compensates on top of the old plan's 180 minutes, not against old std.
        self.assertEqual(live["delay_minutes"], 210)

    def test_unaffected_adjustments_are_inherited(self):
        f1 = self.flight("AB201", hours=0)
        f2 = self.flight("AB202", hours=8, aircraft="AC2", crew="CR2")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        plan = self.svc.create_plan("sched", "scheduler", {
            "disruption_id": d1["id"], "name": "多航班方案", "assignments": [
                {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                 "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))},
                {"flight_id": f2["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                 "new_std": iso(self.base + timedelta(hours=10)), "new_sta": iso(self.base + timedelta(hours=12))},
            ],
        })
        self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.disrupt("aircraft_fault", "AC2", 3, 5)  # overlaps only the first flight
        _, draft = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        by_flight = {a["flight_id"]: a for a in draft["assignments"]}
        self.assertIsNotNone(by_flight[f1["id"]]["affected_by_json"])
        self.assertIsNone(by_flight[f2["id"]]["affected_by_json"])
        # Unaffected adjustment is inherited verbatim and publishes without any edit.
        inherited = by_flight[f2["id"]]
        self.assertEqual(inherited["new_std"], iso(self.base + timedelta(hours=10)))
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": 1, "flight_id": f1["id"], "aircraft_id": "AC3", "crew_id": "CR3",
            "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5)),
        })
        self.assertTrue(self.svc.validate_plan(draft["id"], "sched", "scheduler")["valid"])
        self.svc.lock_plan(draft["id"], "ops", "ops_manager", {"expected_revision": 2})
        live = {f["id"]: f for f in self.svc.state()["flights"]}
        self.assertEqual(live[f2["id"]]["std"], iso(self.base + timedelta(hours=10)))
        self.assertEqual(live[f2["id"]]["delay_minutes"], 120)

    def test_duplicate_replan_returns_one_draft_and_does_not_rewrite_flights(self):
        f1 = self.flight("AB203")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        locked = self.plan_and_lock(d1, {
            "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)),
            "new_sta": iso(self.base + timedelta(hours=5)),
        })
        self.disrupt("aircraft_fault", "AC2", 3, 6)
        _, first = self.svc.replan_plan(locked["id"], "sched", "scheduler", {})
        flight_before = next(f for f in self.svc.state()["flights"] if f["id"] == f1["id"])
        status, second = self.svc.replan_plan(locked["id"], "sched", "scheduler", {})
        self.assertEqual(status, 200)
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["deduplicated"])
        flight_after = next(f for f in self.svc.state()["flights"] if f["id"] == f1["id"])
        self.assertEqual(flight_before["revision"], flight_after["revision"])
        self.assertEqual(flight_before["std"], flight_after["std"])
        # Also one draft when two dispatchers submit simultaneously.
        results: list = []
        errors: list = []

        def worker():
            try:
                results.append(self.svc.replan_plan(locked["id"], "sched2", "scheduler", {}))
            except ApiError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 2)
        self.assertFalse(errors)
        self.assertEqual({item[0] for item in results}, {200})
        self.assertEqual({item[1]["id"] for item in results}, {first["id"]})
        drafts = [r for r in self.svc.state()["plans"] if r.get("parent_plan_id") == locked["id"]]
        self.assertEqual(len(drafts), 1)

    def test_replan_requires_overlap_and_locked_status(self):
        f1 = self.flight("AB204")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        locked = self.plan_and_lock(d1, {
            "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)),
            "new_sta": iso(self.base + timedelta(hours=5)),
        })
        with self.assertRaises(ApiError) as ctx:
            self.svc.replan_plan(locked["id"], "sched", "scheduler", {})
        self.assertEqual(ctx.exception.code, "plan_not_affected")
        draft = self.svc.create_plan("sched", "scheduler", {"disruption_id": d1["id"], "name": "草稿", "assignments": []})
        with self.assertRaises(ApiError) as ctx:
            self.svc.replan_plan(draft["id"], "sched", "scheduler", {})
        self.assertEqual(ctx.exception.code, "plan_not_locked")
        with self.assertRaises(ApiError) as ctx:
            self.svc.replan_plan(locked["id"], "v", "viewer", {})
        self.assertEqual(ctx.exception.status, 403)

    def test_failed_publish_changes_nothing_and_batch_can_retry(self):
        f1 = self.flight("AB205")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        locked = self.plan_and_lock(d1, {
            "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)),
            "new_sta": iso(self.base + timedelta(hours=5)),
        })
        self.disrupt("crew_timeout", "CR2", 3, 6)
        _, draft = self.svc.replan_plan(locked["id"], "sched", "scheduler", {})
        # First attempt still uses the timed-out crew: the whole batch is rejected atomically.
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(draft["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "plan_invalid")
        live = next(f for f in self.svc.state()["flights"] if f["id"] == f1["id"])
        self.assertEqual(live["aircraft_id"], "AC2")
        self.assertEqual(live["crew_id"], "CR2")
        still_draft = self.svc.get_plan(draft["id"])
        self.assertEqual(still_draft["status"], "draft")
        # Fix and retry the whole batch.
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": 1, "flight_id": f1["id"], "aircraft_id": "AC3", "crew_id": "CR3",
            "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5)),
        })
        published = self.svc.lock_plan(draft["id"], "ops", "ops_manager", {"expected_revision": 2})
        self.assertEqual(published["status"], "locked")
        # Repeated publish is idempotent and never rewrites flights twice.
        live = next(f for f in self.svc.state()["flights"] if f["id"] == f1["id"])
        again = self.svc.lock_plan(draft["id"], "ops", "ops_manager", {"expected_revision": 2})
        self.assertEqual(again["status"], "locked")
        live2 = next(f for f in self.svc.state()["flights"] if f["id"] == f1["id"])
        self.assertEqual(live["revision"], live2["revision"])

    def test_publish_permission_is_ops_manager_only(self):
        f1 = self.flight("AB206")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        locked = self.plan_and_lock(d1, {
            "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)),
            "new_sta": iso(self.base + timedelta(hours=5)),
        })
        self.disrupt("aircraft_fault", "AC2", 3, 6)
        _, draft = self.svc.replan_plan(locked["id"], "sched", "scheduler", {})
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": 1, "flight_id": f1["id"], "aircraft_id": "AC3", "crew_id": "CR3",
            "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5)),
        })
        for actor, role in (("sched", "scheduler"), ("v", "viewer"), ("a", "auditor")):
            with self.assertRaises(ApiError) as ctx:
                self.svc.lock_plan(draft["id"], actor, role, {"expected_revision": 2})
            self.assertEqual(ctx.exception.status, 403)

    def test_legacy_schema_is_upgraded_and_old_locked_plan_remains_compatible(self):
        # Build a database with the original (pre-replan) schema and a locked legacy plan.
        self.svc.repo.conn.close()
        legacy = sqlite3.connect(str(self.db_path))
        legacy.execute("PRAGMA foreign_keys=ON")
        legacy.executescript(
            """
            DROP TABLE recovery_plans;
            DROP TABLE assignments;
            CREATE TABLE recovery_plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER, name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1, score_json TEXT, metrics_json TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, locked_at TEXT, locked_by TEXT);
            CREATE TABLE assignments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL, flight_id INTEGER NOT NULL,
                aircraft_id TEXT NOT NULL, crew_id TEXT NOT NULL, new_std TEXT NOT NULL, new_sta TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned', delay_minutes INTEGER NOT NULL DEFAULT 0,
                missed_connections INTEGER NOT NULL DEFAULT 0, UNIQUE(plan_id,flight_id));
            """
        )
        legacy.commit()
        legacy.close()
        # Re-open with the current service: additive migration must not touch existing data.
        svc = AirlineRecoveryService(self.db_path)
        self.svc = svc
        f1 = self.flight("AB207")
        d1 = self.disrupt("aircraft_fault", "AC1", -1, 2)
        locked = self.plan_and_lock(d1, {
            "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)),
            "new_sta": iso(self.base + timedelta(hours=5)),
        })
        self.assertIsNone(locked["parent_plan_id"])
        self.disrupt("aircraft_fault", "AC2", 3, 6)
        _, draft = svc.replan_plan(locked["id"], "sched", "scheduler", {})
        self.assertEqual(draft["parent_plan_id"], locked["id"])
        # Old stored results were voided and recomputed live on the frozen plan.
        old = svc.get_plan(locked["id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["score"]["cost_score"], old["metrics"]["total_delay_minutes"] * 100 + 150)


if __name__ == "__main__":
    unittest.main()
