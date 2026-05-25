"""
Workforce Scheduler — CP-SAT  (robust rewrite)
"""
from __future__ import annotations
from ortools.sat.python import cp_model

from app.models.request import ScheduleRequest
from app.models.response import (
    Assignment, GanttBar, GanttRow, KPI, ScheduleResponse,
    ScoreDetail, SolveStatus, UnassignedTask, ConstraintViolation,
)
from app.solvers.base import BaseSolver


def _to_min(t) -> int:
    """Robustly parse any time string → minutes since midnight."""
    if not t:
        return 0
    s = str(t).strip()
    if "T" in s:
        s = s.split("T")[-1]
    elif " " in s and len(s) > 8:
        s = s.rsplit(" ", 1)[-1]
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return 0
    return h * 60 + m


def _fmt(mins: int) -> str:
    mins = max(0, int(mins))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _overlap(s1, e1, s2, e2) -> bool:
    return s1 < e2 and s2 < e1


class WorkforceSolver(BaseSolver):

    def _solve(self) -> ScheduleResponse:
        req = self.req
        resources = req.resources
        tasks = req.tasks
        cons = req.constraints
        cfg = self._cfg

        if not tasks:
            return self._empty("No shifts/tasks provided.")
        if not resources:
            return self._empty("No employees/resources provided.")

        # Parse shifts
        shift_data = []
        for t in tasks:
            s_min = _to_min(t.earliest_start)
            if t.deadline:
                e_min = _to_min(t.deadline)
                if e_min <= s_min:
                    e_min = s_min + max(t.duration_minutes, 60)
            else:
                e_min = s_min + max(t.duration_minutes, 60)
            dur = e_min - s_min
            if dur <= 0:
                dur = 60
                e_min = s_min + dur
            shift_data.append({"task": t, "start": s_min, "end": e_min, "dur": dur})

        # Parse employees
        emp_data = []
        for r in resources:
            avail_start, avail_end = 0, 24 * 60
            if r.availability:
                av = r.availability[0]
                avail_start = _to_min(av.start)
                avail_end = _to_min(av.end)
                if avail_end <= avail_start:
                    avail_end = avail_start + 8 * 60
            max_hrs = 40
            try:
                max_hrs = int((r.attributes or {}).get("max_hours", 40))
            except (ValueError, TypeError):
                pass
            emp_data.append({
                "res": r,
                "avail_start": avail_start,
                "avail_end": avail_end,
                "max_min": max_hrs * 60,
            })

        n_s, n_e = len(shift_data), len(emp_data)
        model = cp_model.CpModel()
        x = [[model.new_bool_var(f"x_s{s}_e{e}") for e in range(n_e)] for s in range(n_s)]

        for s in range(n_s):
            model.add(sum(x[s][e] for e in range(n_e)) <= 1)

        if self._is_enabled(cons, "skill_match"):
            for s, sd in enumerate(shift_data):
                req_skills = sd["task"].required_skills
                if req_skills:
                    for e, ed in enumerate(emp_data):
                        if not any(sk in ed["res"].skills for sk in req_skills):
                            model.add(x[s][e] == 0)

        if self._is_enabled(cons, "availability"):
            for s, sd in enumerate(shift_data):
                for e, ed in enumerate(emp_data):
                    if sd["start"] < ed["avail_start"] or sd["end"] > ed["avail_end"]:
                        model.add(x[s][e] == 0)

        if self._is_enabled(cons, "no_double"):
            for e in range(n_e):
                for s1 in range(n_s):
                    for s2 in range(s1 + 1, n_s):
                        if _overlap(shift_data[s1]["start"], shift_data[s1]["end"],
                                    shift_data[s2]["start"], shift_data[s2]["end"]):
                            model.add(x[s1][e] + x[s2][e] <= 1)

        if self._is_enabled(cons, "max_hours"):
            for e, ed in enumerate(emp_data):
                model.add(sum(x[s][e] * shift_data[s]["dur"] for s in range(n_s)) <= ed["max_min"])

        ASSIGN_W = 10_000
        obj = [ASSIGN_W * x[s][e] for s in range(n_s) for e in range(n_e)]

        if self._is_enabled(cons, "minimize_cost"):
            w = self._weight(cons, "minimize_cost", 6)
            for e, ed in enumerate(emp_data):
                rate = int(ed["res"].cost_per_hour * 100)
                for s in range(n_s):
                    obj.append(-int(w * rate * shift_data[s]["dur"] / 60) * x[s][e])

        model.maximize(sum(obj))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = cfg.time_limit_seconds
        solver.parameters.num_workers = cfg.num_workers
        status_code = solver.solve(model)

        status_map = {
            cp_model.OPTIMAL: SolveStatus.OPTIMAL,
            cp_model.FEASIBLE: SolveStatus.FEASIBLE,
            cp_model.INFEASIBLE: SolveStatus.INFEASIBLE,
            cp_model.UNKNOWN: SolveStatus.TIMEOUT,
        }
        status = status_map.get(status_code, SolveStatus.ERROR)

        if status == SolveStatus.INFEASIBLE:
            return ScheduleResponse(
                status=status, category=self.category, subtype=self.subtype,
                solve_time_ms=0, score=ScoreDetail(), kpis=[],
                assignments=[],
                unassigned=[
                    UnassignedTask(task_id=sd["task"].id, task_name=sd["task"].name,
                                   reason="Infeasible — check skills & availability match")
                    for sd in shift_data
                ],
                violations=[], gantt=[],
                solver_info={"cp_status": str(solver.status_name)},
            )

        assignments, unassigned, violations = [], [], []
        hours_worked = {ed["res"].id: 0.0 for ed in emp_data}

        for s, sd in enumerate(shift_data):
            placed = False
            for e, ed in enumerate(emp_data):
                if solver.value(x[s][e]):
                    dur_hrs = sd["dur"] / 60
                    cost = round(ed["res"].cost_per_hour * dur_hrs, 2)
                    hours_worked[ed["res"].id] += dur_hrs
                    assignments.append(Assignment(
                        task_id=sd["task"].id, task_name=sd["task"].name,
                        resource_id=ed["res"].id, resource_name=ed["res"].name,
                        start=_fmt(sd["start"]), end=_fmt(sd["end"]), cost=cost,
                    ))
                    placed = True
                    break
            if not placed:
                unassigned.append(UnassignedTask(
                    task_id=sd["task"].id, task_name=sd["task"].name,
                    reason="No qualified or available employee",
                ))

        total_cost = sum(a.cost for a in assignments)
        workers_used = sum(1 for h in hours_worked.values() if h > 0)
        util_pct = round(workers_used / max(n_e, 1) * 100, 1)

        kpis = [
            KPI(key="shifts_assigned", value=len(assignments)),
            KPI(key="unassigned", value=len(unassigned)),
            KPI(key="total_cost", value=round(total_cost, 2), unit="$"),
            KPI(key="utilisation", value=util_pct, unit="%"),
            KPI(key="workers_used", value=workers_used),
        ]

        gantt = []
        for ed in emp_data:
            bars = [
                GanttBar(task_id=a.task_id, task_name=a.task_name,
                         start_offset=float(_to_min(a.start)),
                         duration=float(_to_min(a.end) - _to_min(a.start)))
                for a in assignments if a.resource_id == ed["res"].id
            ]
            if bars:
                gantt.append(GanttRow(resource_id=ed["res"].id,
                                      resource_name=ed["res"].name, bars=bars))

        return ScheduleResponse(
            status=status, category=self.category, subtype=self.subtype,
            solve_time_ms=0,
            score=ScoreDetail(hard_violations=0, soft_score=-round(total_cost, 2)),
            kpis=kpis, assignments=assignments, unassigned=unassigned,
            violations=violations, gantt=gantt,
            solver_info={"cp_status": str(solver.status_name),
                         "parsed_shifts": [{"name": sd["task"].name,
                                            "start": _fmt(sd["start"]),
                                            "end": _fmt(sd["end"]),
                                            "dur_min": sd["dur"]}
                                           for sd in shift_data]},
        )

    def _empty(self, reason: str) -> ScheduleResponse:
        return ScheduleResponse(
            status=SolveStatus.INFEASIBLE, category=self.category, subtype=self.subtype,
            solve_time_ms=0, score=ScoreDetail(), kpis=[],
            assignments=[], unassigned=[], violations=[
                ConstraintViolation(constraint="input", details=reason, severity="hard")
            ], gantt=[],
        )
