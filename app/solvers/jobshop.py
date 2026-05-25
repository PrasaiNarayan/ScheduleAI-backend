"""
Job Shop Scheduler — CP-SAT  (robust rewrite)
Supports: classic | flexible | openshop
"""
from __future__ import annotations
from ortools.sat.python import cp_model

from app.models.response import (
    Assignment, GanttBar, GanttRow, KPI, ScheduleResponse,
    ScoreDetail, SolveStatus, UnassignedTask, ConstraintViolation,
)
from app.solvers.base import BaseSolver


def _to_min(t) -> int | None:
    """Parse ISO datetime or HH:MM string → minutes. Returns None if unparseable."""
    if not t:
        return None
    s = str(t).strip()
    if "T" in s:
        s = s.split("T")[-1]
    elif " " in s and len(s) > 8:
        s = s.rsplit(" ", 1)[-1]
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        result = h * 60 + m
        return result if result > 0 else None
    except (ValueError, IndexError):
        return None


def _fmt(mins: int) -> str:
    mins = max(0, int(mins))
    return f"{mins // 60:02d}:{mins % 60:02d}"


class JobshopSolver(BaseSolver):

    def _solve(self) -> ScheduleResponse:
        req  = self.req
        cons = req.constraints
        cfg  = self._cfg

        # Only tasks that have operations defined
        jobs_raw = [t for t in req.tasks if t.operations]
        if not jobs_raw:
            return self._err(
                "No jobs with operations found. "
                "Make sure each job has at least one operation in the format: "
                "Machine Name → 30"
            )

        # Validate operations have positive durations
        for job in jobs_raw:
            for op in job.operations:
                if op.duration_min <= 0:
                    op.duration_min = 30  # safe default

        # Build machine index from all referenced machine names
        all_machine_names: set[str] = set()
        for job in jobs_raw:
            for op in job.operations:
                name = (op.machine_id or "").strip()
                if name:
                    all_machine_names.add(name)

        if not all_machine_names:
            return self._err("No valid machine names found in operations.")

        machine_list = sorted(all_machine_names)
        m_idx        = {m: i for i, m in enumerate(machine_list)}
        n_machines   = len(machine_list)

        # Also build resource name lookup for display
        resource_name = {r.name: r.name for r in req.resources}

        # Horizon = sum of all durations (safe upper bound)
        horizon = max(
            sum(op.duration_min for job in jobs_raw for op in job.operations) + 1,
            60,
        )

        model = cp_model.CpModel()

        # all_tasks[(j, k)] = {start, end, interval, machine_idx, dur}
        all_tasks:        dict[tuple, dict]  = {}
        machine_intervals: dict[int, list]   = {i: [] for i in range(n_machines)}
        job_ends:          list              = []
        deadline_vars:     list[tuple]       = []  # (job_name, deadline_min, end_var)

        for j, job in enumerate(jobs_raw):
            job_op_end = None

            for k, op in enumerate(job.operations):
                mname = (op.machine_id or "").strip()
                m_i   = m_idx.get(mname)
                if m_i is None:
                    continue  # machine not recognised — skip silently

                dur   = max(op.duration_min, 1)
                start = model.new_int_var(0, horizon, f"s_j{j}_k{k}")
                end   = model.new_int_var(0, horizon, f"e_j{j}_k{k}")
                intv  = model.new_interval_var(start, dur, end, f"i_j{j}_k{k}")

                all_tasks[(j, k)] = {
                    "start": start, "end": end, "interval": intv,
                    "machine": m_i, "dur": dur, "mname": mname,
                }
                machine_intervals[m_i].append(intv)

                # Precedence: classic + flexible enforce order; openshop does not
                if self.subtype != "openshop" and k > 0:
                    prev = all_tasks.get((j, k - 1))
                    if prev:
                        model.add(prev["end"] <= start)

                job_op_end = end

            if job_op_end is not None:
                job_ends.append(job_op_end)
                dl = _to_min(job.deadline)
                if dl and self._is_enabled(cons, "deadline"):
                    deadline_vars.append((job.name, dl, job_op_end))

        if not job_ends:
            return self._err(
                "All operations were skipped — machine names in the operations textarea "
                "must match exactly the machine names you defined above. "
                "Example: if you named a machine 'Lathe A', write 'Lathe A → 30' in the operations box."
            )

        # No-overlap per machine (always enforced)
        for m_i, intervals in machine_intervals.items():
            if len(intervals) > 1:
                model.add_no_overlap(intervals)

        # Deadline hard constraints
        violations: list[ConstraintViolation] = []
        for job_name, dl, end_var in deadline_vars:
            model.add(end_var <= dl)

        # Makespan variable
        makespan = model.new_int_var(0, horizon, "makespan")
        model.add_max_equality(makespan, job_ends)
        model.minimize(makespan)

        # Solve
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = cfg.time_limit_seconds
        solver.parameters.num_workers         = cfg.num_workers
        status_code = solver.solve(model)

        status_map = {
            cp_model.OPTIMAL:    SolveStatus.OPTIMAL,
            cp_model.FEASIBLE:   SolveStatus.FEASIBLE,
            cp_model.INFEASIBLE: SolveStatus.INFEASIBLE,
            cp_model.UNKNOWN:    SolveStatus.TIMEOUT,
        }
        status = status_map.get(status_code, SolveStatus.ERROR)

        if status in (SolveStatus.INFEASIBLE, SolveStatus.ERROR):
            return ScheduleResponse(
                status=status, category=self.category, subtype=self.subtype,
                solve_time_ms=0, score=ScoreDetail(), kpis=[],
                assignments=[],
                unassigned=[UnassignedTask(task_id=j.id, task_name=j.name,
                                           reason="Infeasible — check deadlines vs durations")
                             for j in jobs_raw],
                violations=violations, gantt=[],
                solver_info={"cp_status": str(solver.status_name)},
            )

        makespan_val = solver.value(makespan)

        # Extract assignments
        assignments:   list[Assignment] = []
        machine_load:  dict[int, int]   = {i: 0 for i in range(n_machines)}

        for j, job in enumerate(jobs_raw):
            for k, op in enumerate(job.operations):
                td = all_tasks.get((j, k))
                if not td:
                    continue
                s_val = solver.value(td["start"])
                e_val = solver.value(td["end"])
                m_i   = td["machine"]
                mname = td["mname"]
                machine_load[m_i] += td["dur"]

                assignments.append(Assignment(
                    task_id=f"{job.id}_op{k}",
                    task_name=f"{job.name} — op{k+1} ({mname})",
                    resource_id=mname,
                    resource_name=resource_name.get(mname, mname),
                    start=_fmt(s_val),
                    end=_fmt(e_val),
                    cost=float(td["dur"]),  # cost field stores duration (mins)
                ))

        avg_util = round(
            sum(machine_load.values()) / max(makespan_val * n_machines, 1) * 100, 1
        ) if makespan_val > 0 else 0.0

        kpis = [
            KPI(key="makespan",         value=makespan_val, unit="min"),
            KPI(key="operations",       value=len(assignments)),
            KPI(key="avg_machine_util", value=avg_util, unit="%"),
            KPI(key="jobs_scheduled",   value=len(jobs_raw)),
        ]

        # Gantt — one row per machine, start_offset in minutes from t=0
        gantt: list[GanttRow] = []
        for m_i, mname in enumerate(machine_list):
            bars = []
            for a in assignments:
                if a.resource_id != mname:
                    continue
                # a.start is "HH:MM" where H = hours from time-zero, not wall clock
                # Convert back: "01:30" means 90 minutes from start
                parts = a.start.split(":")
                offset = int(parts[0]) * 60 + int(parts[1])
                bars.append(GanttBar(
                    task_id=a.task_id,
                    task_name=a.task_name,
                    start_offset=float(offset),
                    duration=a.cost,
                ))
            if bars:
                gantt.append(GanttRow(
                    resource_id=mname,
                    resource_name=resource_name.get(mname, mname),
                    bars=bars,
                ))

        return ScheduleResponse(
            status=status, category=self.category, subtype=self.subtype,
            solve_time_ms=0,
            score=ScoreDetail(soft_score=-float(makespan_val)),
            kpis=kpis, assignments=assignments, unassigned=[],
            violations=violations, gantt=gantt,
            solver_info={
                "cp_status":   str(solver.status_name),
                "makespan_min": makespan_val,
                "machines":    machine_list,
                "jobs":        [{"name": j.name, "ops": len(j.operations)} for j in jobs_raw],
            },
        )

    def _err(self, reason: str) -> ScheduleResponse:
        return ScheduleResponse(
            status=SolveStatus.INFEASIBLE,
            category=self.category, subtype=self.subtype,
            solve_time_ms=0, score=ScoreDetail(), kpis=[],
            assignments=[], unassigned=[], violations=[
                ConstraintViolation(constraint="input", details=reason, severity="hard")
            ], gantt=[],
        )
