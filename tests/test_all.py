"""
pytest test suite — covers all four solvers + classifier.
Run with: pytest tests/ -v
"""
import json, pytest
from pathlib import Path
from app.models.request  import ScheduleRequest
from app.models.response import SolveStatus
from app.classifier      import classify
from app.solvers.dispatcher import dispatch
from app.models.request import ProblemCategory


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_example(name: str) -> ScheduleRequest:
    path = Path(__file__).parent.parent / "examples" / name
    return ScheduleRequest(**json.loads(path.read_text()))


def quick_solve(req: ScheduleRequest):
    req.solver_config.time_limit_seconds = 5
    category, subtype = classify(req)
    return dispatch(req, category, subtype)


# ── Classifier tests ──────────────────────────────────────────────────────────

class TestClassifier:

    def test_explicit_category_respected(self):
        req = ScheduleRequest(category="workforce", subtype="shift", resources=[], tasks=[])
        cat, sub = classify(req)
        assert cat == ProblemCategory.workforce
        assert sub == "shift"

    def test_routing_detected_from_demand(self):
        from app.models.request import Task, Resource
        req = ScheduleRequest(
            resources=[Resource(id="v1", name="Van", type="vehicle")],
            tasks=[Task(id="t1", name="Stop A", duration_minutes=10, demand=5)],
            num_vehicles=1,
        )
        cat, sub = classify(req)
        assert cat == ProblemCategory.routing
        assert sub == "tsp"

    def test_jobshop_detected_from_operations(self):
        from app.models.request import Task, Operation
        req = ScheduleRequest(
            tasks=[Task(
                id="j1", name="Job 1", duration_minutes=0,
                operations=[Operation(machine_id="M1", duration_min=30)]
            )]
        )
        cat, sub = classify(req)
        assert cat == ProblemCategory.jobshop

    def test_timetabling_detected_from_timeslots(self):
        from app.models.request import Timeslot
        req = ScheduleRequest(
            timeslots=[Timeslot(id="s1", day="Monday", start="09:00", end="10:00")],
            tasks=[],
        )
        cat, sub = classify(req)
        assert cat == ProblemCategory.timetabling

    def test_workforce_fallback(self):
        from app.models.request import Task, Resource
        req = ScheduleRequest(
            resources=[Resource(id="e1", name="Alice", type="human")],
            tasks=[Task(id="s1", name="Morning shift", duration_minutes=480,
                        earliest_start="08:00", deadline="16:00")],
        )
        cat, sub = classify(req)
        assert cat == ProblemCategory.workforce

    def test_empty_request_raises(self):
        req = ScheduleRequest()
        with pytest.raises(ValueError):
            classify(req)


# ── Workforce tests ───────────────────────────────────────────────────────────

class TestWorkforce:

    def test_shift_example_solves(self):
        req = load_example("workforce_shift.json")
        res = quick_solve(req)
        assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        assert res.category == "workforce"
        assert len(res.assignments) > 0

    def test_assignments_respect_skill_match(self):
        req = load_example("workforce_shift.json")
        res = quick_solve(req)
        # Build a skill lookup
        skill_map = {r.id: r.skills for r in req.resources}
        task_map  = {t.id: t.required_skills for t in req.tasks}
        for a in res.assignments:
            required = task_map.get(a.task_id, [])
            emp_skills = skill_map.get(a.resource_id, [])
            if required:
                assert any(s in emp_skills for s in required), \
                    f"{a.resource_name} lacks skill for {a.task_name}"

    def test_kpis_present(self):
        req = load_example("workforce_shift.json")
        res = quick_solve(req)
        kpi_keys = {k.key for k in res.kpis}
        assert "shifts_assigned" in kpi_keys
        assert "total_cost"      in kpi_keys
        assert "utilisation"     in kpi_keys

    def test_gantt_populated(self):
        req = load_example("workforce_shift.json")
        res = quick_solve(req)
        assert len(res.gantt) > 0
        for row in res.gantt:
            assert len(row.bars) > 0

    def test_infeasible_returns_gracefully(self):
        """Shift requiring a skill no employee has → graceful result."""
        from app.models.request import Task, Resource
        req = ScheduleRequest(
            category="workforce", subtype="shift",
            resources=[Resource(id="e1", name="Alice", type="human", skills=["driving"])],
            tasks=[Task(id="s1", name="Welding shift", duration_minutes=240,
                        required_skills=["welding"], earliest_start="08:00", deadline="12:00")],
            constraints=[],
        )
        req.solver_config.time_limit_seconds = 5
        cat, sub = classify(req)
        res = dispatch(req, cat, sub)
        assert len(res.unassigned) == 1
        assert res.unassigned[0].task_id == "s1"


# ── Job shop tests ────────────────────────────────────────────────────────────

class TestJobshop:

    def test_classic_example_solves(self):
        req = load_example("jobshop_classic.json")
        res = quick_solve(req)
        assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        assert res.category == "jobshop"
        assert len(res.assignments) > 0

    def test_makespan_kpi_present(self):
        req = load_example("jobshop_classic.json")
        res = quick_solve(req)
        kpi_keys = {k.key for k in res.kpis}
        assert "makespan" in kpi_keys

    def test_no_machine_overlap(self):
        """Verify no two operations on the same machine overlap in time."""
        req = load_example("jobshop_classic.json")
        res = quick_solve(req)

        def to_min(t: str) -> int:
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        from collections import defaultdict
        machine_intervals: dict[str, list[tuple]] = defaultdict(list)
        for a in res.assignments:
            machine_intervals[a.resource_id].append(
                (to_min(a.start), to_min(a.end), a.task_name)
            )

        for machine, intervals in machine_intervals.items():
            intervals.sort()
            for i in range(len(intervals) - 1):
                _, end_i, name_i = intervals[i]
                start_j, _, name_j = intervals[i + 1]
                assert end_i <= start_j, \
                    f"Machine {machine}: '{name_i}' overlaps '{name_j}'"

    def test_operation_precedence(self):
        """Op k+1 must start after op k ends for each job."""
        req = load_example("jobshop_classic.json")
        res = quick_solve(req)

        def to_min(t: str) -> int:
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        from collections import defaultdict
        job_ops: dict[str, list] = defaultdict(list)
        for a in res.assignments:
            base_id = a.task_id.split("_op")[0]
            op_num  = int(a.task_id.split("_op")[1]) if "_op" in a.task_id else 0
            job_ops[base_id].append((op_num, to_min(a.start), to_min(a.end)))

        for job_id, ops in job_ops.items():
            ops.sort()
            for i in range(len(ops) - 1):
                _, _, end_i   = ops[i]
                _, start_j, _ = ops[i + 1]
                assert end_i <= start_j, f"Job {job_id}: precedence violated"


# ── Routing tests ─────────────────────────────────────────────────────────────

class TestRouting:

    def test_vrptw_example_solves(self):
        req = load_example("routing_vrptw.json")
        res = quick_solve(req)
        assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        assert res.category == "routing"
        assert len(res.assignments) > 0

    def test_all_stops_served(self):
        req = load_example("routing_vrptw.json")
        res = quick_solve(req)
        served = {a.task_id for a in res.assignments}
        task_ids = {t.id for t in req.tasks}
        # Either served or in unassigned list
        accounted = served | {u.task_id for u in res.unassigned}
        assert task_ids == accounted

    def test_auto_detect_routing(self):
        req = load_example("auto_detect_routing.json")
        cat, sub = classify(req)
        assert cat == ProblemCategory.routing

    def test_kpis_present(self):
        req = load_example("routing_vrptw.json")
        res = quick_solve(req)
        kpi_keys = {k.key for k in res.kpis}
        assert "stops_served"  in kpi_keys
        assert "vehicles_used" in kpi_keys
        assert "total_travel"  in kpi_keys


# ── Timetabling tests ─────────────────────────────────────────────────────────

class TestTimetabling:

    def test_school_example_solves(self):
        req = load_example("timetabling_school.json")
        res = quick_solve(req)
        assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        assert res.category == "timetabling"

    def test_no_room_conflict(self):
        """No two sessions in same room at same timeslot."""
        req = load_example("timetabling_school.json")
        res = quick_solve(req)
        seen: set[tuple] = set()
        for a in res.assignments:
            key = (a.resource_id, a.start)
            assert key not in seen, f"Room conflict: {a.resource_name} double-booked at {a.start}"
            seen.add(key)

    def test_no_teacher_conflict(self):
        """Same teacher must not appear in two sessions at the same timeslot."""
        req = load_example("timetabling_school.json")
        res = quick_solve(req)
        teacher_map = {t.id: t.teacher for t in req.tasks}
        seen: set[tuple] = set()
        for a in res.assignments:
            teacher = teacher_map.get(a.task_id)
            if teacher:
                key = (teacher, a.start)
                assert key not in seen, f"Teacher conflict: {teacher} double-booked at {a.start}"
                seen.add(key)

    def test_kpis_present(self):
        req = load_example("timetabling_school.json")
        res = quick_solve(req)
        kpi_keys = {k.key for k in res.kpis}
        assert "sessions_scheduled" in kpi_keys
        assert "rooms_used"         in kpi_keys
