import sys, tempfile, sqlite3, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow


class ReplanChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        self.base = utcnow() + timedelta(days=1)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        for acid in ("AC1", "AC2", "AC3"):
            self.svc.seed_aircraft("ops", "ops_manager", {"id": acid, "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        for crid in ("CR1", "CR2", "CR3"):
            self.svc.seed_crew("ops", "ops_manager", {"id": crid, "name": "组" + crid, "base": "AAA",
                                                      "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB",
                                                       "valid_from": iso(self.base - timedelta(days=1)),
                                                       "valid_to": iso(self.base + timedelta(days=4))})

    def tearDown(self):
        self.tmp.cleanup()

    def _flight(self, no, aircraft, crew, std=None, sta=None, pax=150):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": no, "origin": "AAA", "destination": "BBB",
                                                              "std": iso(std or self.base), "sta": iso(sta or self.base + timedelta(hours=2)),
                                                              "aircraft_id": aircraft, "crew_id": crew, "passenger_count": pax})

    def _disruption(self, kind, resource, start, end):
        return self.svc.create_disruption("sched", "scheduler", {"kind": kind, "resource_id": resource,
                                                                  "starts_at": iso(start), "ends_at": iso(end)})

    def _plan(self, disruption_id, name, assignments):
        return self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption_id, "name": name, "assignments": assignments})

    def _lock(self, plan_id, revision, role="ops_manager", actor="ops"):
        return self.svc.lock_plan(plan_id, actor, role, {"expected_revision": revision})

    def test_replan_freezes_and_inherits(self):
        f1 = self._flight("AB100", "AC1", "CR1")
        f2 = self._flight("AB101", "AC3", "CR3", std=self.base + timedelta(hours=4), sta=self.base + timedelta(hours=6), pax=80)
        d1 = self._disruption("aircraft_fault", "AC1", self.base - timedelta(hours=1), self.base + timedelta(hours=1))
        plan = self._plan(d1["id"], "方案一", [
            {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))},
            {"flight_id": f2["id"], "aircraft_id": "AC3", "crew_id": "CR3", "new_std": iso(self.base + timedelta(hours=4)), "new_sta": iso(self.base + timedelta(hours=6))},
        ])
        self._lock(plan["id"], 1)
        # 新中断：AC2 在 f1 调整时段内故障
        self._disruption("aircraft_fault", "AC2", self.base + timedelta(hours=1), self.base + timedelta(hours=3))

        draft = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["source_plan_id"], plan["id"])
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "frozen")
        draft_flights = {a["flight_id"]: a for a in draft["assignments"]}
        # 受影响航班从当前航班状态生成初始调整
        self.assertIn(f1["id"], draft_flights)
        self.assertEqual(draft_flights[f1["id"]]["aircraft_id"], "AC2")
        self.assertEqual(draft_flights[f1["id"]]["new_std"], iso(self.base + timedelta(hours=2)))
        # 未受影响调整继承
        self.assertIn(f2["id"], draft_flights)
        self.assertEqual(draft_flights[f2["id"]]["aircraft_id"], "AC3")

    def test_replan_idempotent_no_flight_rewrite(self):
        f1 = self._flight("AB100", "AC1", "CR1")
        d1 = self._disruption("aircraft_fault", "AC1", self.base - timedelta(hours=1), self.base + timedelta(hours=1))
        plan = self._plan(d1["id"], "方案一", [
            {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))},
        ])
        self._lock(plan["id"], 1)
        self._disruption("aircraft_fault", "AC2", self.base, self.base + timedelta(hours=2))

        draft1 = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        flight_before = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (f1["id"],)).fetchone()
        draft2 = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        flight_after = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (f1["id"],)).fetchone()
        self.assertEqual(draft1["id"], draft2["id"])
        self.assertEqual(flight_before["std"], flight_after["std"])
        self.assertEqual(flight_before["aircraft_id"], flight_after["aircraft_id"])

    def test_replan_permissions(self):
        f1 = self._flight("AB100", "AC1", "CR1")
        d1 = self._disruption("aircraft_fault", "AC1", self.base - timedelta(hours=1), self.base + timedelta(hours=1))
        plan = self._plan(d1["id"], "方案一", [
            {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))},
        ])
        self._lock(plan["id"], 1)
        self._disruption("aircraft_fault", "AC2", self.base, self.base + timedelta(hours=2))
        draft = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.replan_plan(plan["id"], "viewer", "viewer", {})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self._lock(draft["id"], draft["revision"], role="scheduler", actor="sched")
        self.assertEqual(ctx.exception.status, 403)

    def test_lock_failure_rollback_and_retry(self):
        f1 = self._flight("AB100", "AC1", "CR1")
        d1 = self._disruption("aircraft_fault", "AC1", self.base - timedelta(hours=1), self.base + timedelta(hours=1))
        plan = self._plan(d1["id"], "方案一", [
            {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))},
        ])
        self._lock(plan["id"], 1)
        self._disruption("aircraft_fault", "AC2", self.base + timedelta(hours=1), self.base + timedelta(hours=3))
        draft = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        # AC4 维修已到期，第一次发布失败
        self.svc.repo.conn.execute("UPDATE aircraft SET maintenance_due=? WHERE id='AC3'", (iso(self.base - timedelta(hours=1)),))
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": draft["revision"], "flight_id": f1["id"], "aircraft_id": "AC3", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=5)), "new_sta": iso(self.base + timedelta(hours=7))})
        with self.assertRaises(ApiError) as ctx:
            self._lock(draft["id"], draft["revision"] + 1)
        self.assertEqual(ctx.exception.code, "plan_invalid")
        # 回滚后航班未被改写
        self.assertEqual(self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (f1["id"],)).fetchone()["aircraft_id"], "AC2")
        # 整批重试：恢复 AC3 维修，换成 AC2
        self.svc.repo.conn.execute("UPDATE aircraft SET maintenance_due=? WHERE id='AC3'", (iso(self.base + timedelta(days=5)),))
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": draft["revision"] + 1, "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=5)), "new_sta": iso(self.base + timedelta(hours=7))})
        locked = self._lock(draft["id"], draft["revision"] + 2)
        self.assertEqual(locked["status"], "locked")
        flight = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (f1["id"],)).fetchone()
        self.assertEqual(flight["aircraft_id"], "AC2")
        self.assertEqual(flight["std"], iso(self.base + timedelta(hours=5)))

    def test_delay_compensation_not_stacked(self):
        f1 = self._flight("AB100", "AC1", "CR1")
        d1 = self._disruption("aircraft_fault", "AC1", self.base - timedelta(hours=1), self.base + timedelta(hours=1))
        plan = self._plan(d1["id"], "方案一", [
            {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))},
        ])
        self._lock(plan["id"], 1)
        self.assertEqual(self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (f1["id"],)).fetchone()["delay_minutes"], 120)
        self._disruption("aircraft_fault", "AC2", self.base + timedelta(hours=1), self.base + timedelta(hours=3))
        draft = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        self.svc.add_assignment(draft["id"], "sched", "scheduler", {
            "expected_revision": draft["revision"], "flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=5)), "new_sta": iso(self.base + timedelta(hours=7))})
        self._lock(draft["id"], draft["revision"] + 1)
        flight = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (f1["id"],)).fetchone()
        # 补偿：delay 相对当前航班状态(base+2h)计算=180，不与原 delay(120) 叠加成 300
        self.assertEqual(flight["delay_minutes"], 180)

    def test_airport_closure_overlap_triggers_replan(self):
        self.svc.repo.conn.execute("UPDATE crew SET duty_start=? WHERE id='CR3'", (iso(self.base + timedelta(days=2) - timedelta(hours=2)),))
        f3 = self._flight("AB102", "AC3", "CR3", std=self.base + timedelta(days=2), sta=self.base + timedelta(days=2, hours=2), pax=60)
        d3 = self._disruption("airport_closure", "AAA", self.base + timedelta(days=2), self.base + timedelta(days=2, hours=1))
        plan = self._plan(d3["id"], "方案三", [
            {"flight_id": f3["id"], "aircraft_id": "AC3", "crew_id": "CR3", "new_std": iso(self.base + timedelta(days=2, hours=2)), "new_sta": iso(self.base + timedelta(days=2, hours=4))},
        ])
        self._lock(plan["id"], 1)
        self._disruption("airport_closure", "AAA", self.base + timedelta(days=2, hours=2), self.base + timedelta(days=2, hours=3))
        draft = self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "frozen")

    def test_no_overlap_replan_rejected(self):
        self.svc.repo.conn.execute("UPDATE crew SET duty_start=? WHERE id='CR3'", (iso(self.base + timedelta(days=3) - timedelta(hours=2)),))
        f4 = self._flight("AB103", "AC3", "CR3", std=self.base + timedelta(days=3), sta=self.base + timedelta(days=3, hours=2), pax=40)
        d5 = self._disruption("crew_timeout", "CR3", self.base + timedelta(days=10), self.base + timedelta(days=10, hours=2))
        plan = self._plan(d5["id"], "方案四", [
            {"flight_id": f4["id"], "aircraft_id": "AC3", "crew_id": "CR3", "new_std": iso(self.base + timedelta(days=3)), "new_sta": iso(self.base + timedelta(days=3, hours=2))},
        ])
        self._lock(plan["id"], 1)
        with self.assertRaises(ApiError) as ctx:
            self.svc.replan_plan(plan["id"], "sched", "scheduler", {})
        self.assertEqual(ctx.exception.code, "replan_not_needed")

    def test_compare_shows_invalid_after_new_disruption(self):
        f1 = self._flight("AB100", "AC1", "CR1")
        d1 = self._disruption("aircraft_fault", "AC1", self.base - timedelta(hours=1), self.base + timedelta(hours=1))
        plan = self._plan(d1["id"], "方案一", [
            {"flight_id": f1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))},
        ])
        self._lock(plan["id"], 1)
        self.assertTrue(self.svc.compare_plans(d1["id"])["plans"][0]["valid"])
        self._disruption("aircraft_fault", "AC2", self.base, self.base + timedelta(hours=2))
        compared = self.svc.compare_plans(d1["id"])["plans"][0]
        self.assertFalse(compared["valid"])
        self.assertTrue(any(p["code"] == "disruption_overlap" for p in compared["problems"]))

    def test_legacy_database_migration(self):
        old_db = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(str(old_db))
        con.executescript("""
        CREATE TABLE airports(code TEXT PRIMARY KEY, country TEXT NOT NULL, curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL);
        CREATE TABLE aircraft(id TEXT PRIMARY KEY, model TEXT NOT NULL, maintenance_due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE crew(id TEXT PRIMARY KEY, name TEXT NOT NULL, base TEXT NOT NULL, duty_start TEXT NOT NULL, max_duty_minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE permits(id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, destination TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, curfew_exempt INTEGER NOT NULL DEFAULT 0, UNIQUE(origin,destination,valid_from,valid_to));
        CREATE TABLE flights(id INTEGER PRIMARY KEY AUTOINCREMENT, flight_no TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, destination TEXT NOT NULL, std TEXT NOT NULL, sta TEXT NOT NULL, aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id), passenger_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'scheduled', delay_minutes INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1, cancel_reason TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE disruptions(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, resource_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
        CREATE TABLE recovery_plans(id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER NOT NULL REFERENCES disruptions(id), name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1, score_json TEXT, metrics_json TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, locked_at TEXT, locked_by TEXT);
        CREATE TABLE assignments(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES recovery_plans(id) ON DELETE CASCADE, flight_id INTEGER NOT NULL REFERENCES flights(id), aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id), new_std TEXT NOT NULL, new_sta TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned', delay_minutes INTEGER NOT NULL DEFAULT 0, missed_connections INTEGER NOT NULL DEFAULT 0, UNIQUE(plan_id,flight_id));
        CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        con.commit(); con.close()
        svc = AirlineRecoveryService(old_db)
        svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "组1", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "组2", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(self.base - timedelta(days=1)), "valid_to": iso(self.base + timedelta(days=4))})
        of = svc.create_flight("sched", "scheduler", {"flight_no": "AB200", "origin": "AAA", "destination": "BBB", "std": iso(self.base), "sta": iso(self.base + timedelta(hours=2)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 100})
        od = svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=1))})
        op = svc.create_plan("sched", "scheduler", {"disruption_id": od["id"], "name": "旧方案", "assignments": [
            {"flight_id": of["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))},
        ]})
        svc.lock_plan(op["id"], "ops", "ops_manager", {"expected_revision": 1})
        svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC2", "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=2))})
        draft = svc.replan_plan(op["id"], "sched", "scheduler", {})
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["source_plan_id"], op["id"])
        self.assertEqual(svc.get_plan(op["id"])["status"], "frozen")


if __name__ == "__main__":
    unittest.main()
