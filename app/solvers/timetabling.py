"""
Timetabling Scheduler — CP-SAT
================================
Supports:  school | meeting | conference

Model
-----
Variables : assign[s][r][t] ∈ {0,1}
            (1 if session s is placed in room r at timeslot t)

Hard constraints (when enabled):
  - room_conflict    : sum_s assign[s][r][t] <= 1  ∀ r,t
  - teacher_conflict : sum_(s sharing teacher) assign[s][r][t] <= 1  ∀ teacher,t
  - capacity         : assign[s][r][t] = 0 if room.capacity < session.size

Soft objectives:
  - spread_sessions  : penalise sessions all in same timeslot
  - preferred_room   : bonus for room type match (attributes)

Objective: maximise sessions assigned (medium) + soft objectives
"""
from __future__ import annotations
from ortools.sat.python import cp_model

from app.models.request import ScheduleRequest, Timeslot
from app.models.response import (
    Assignment, GanttBar, GanttRow, KPI, ScheduleResponse,
    ScoreDetail, SolveStatus, UnassignedTask, ConstraintViolation,
)
from app.solvers.base import BaseSolver


class TimetablingSolver(BaseSolver):

    def _solve(self) -> ScheduleResponse:
        req      = self.req
        cons     = req.constraints
        cfg      = self._cfg

        sessions  = req.tasks           # things to schedule
        rooms     = [r for r in req.resources if r.type in ("room", "human", "machine")] or req.resources
        timeslots = req.timeslots

        if not sessions:
            return self._infeasible("No sessions to schedule.")
        if not rooms:
            return self._infeasible("No rooms defined.")
        if not timeslots:
            # Auto-generate timeslots if none provided
            timeslots = [
                Timeslot(id=f"slot-{i}", day="Monday", start=f"{8+i}:00", end=f"{9+i}:00")
                for i in range(max(len(sessions), 4))
            ]

        n_s = len(sessions)
        n_r = len(rooms)
        n_t = len(timeslots)

        model = cp_model.CpModel()

        # x[s][r][t] = 1 if session s assigned to room r at timeslot t
        x = [[[model.new_bool_var(f"x_s{s}_r{r}_t{t}")
                for t in range(n_t)] for r in range(n_r)] for s in range(n_s)]

        # ── Each session assigned to at most one (room, timeslot) ─────────────
        for s in range(n_s):
            model.add(sum(x[s][r][t] for r in range(n_r) for t in range(n_t)) <= 1)

        # ── Hard: room conflict ────────────────────────────────────────────────
        if self._is_enabled(cons, "room_conflict") or True:
            for r in range(n_r):
                for t in range(n_t):
                    model.add(sum(x[s][r][t] for s in range(n_s)) <= 1)

        # ── Hard: teacher/host conflict ────────────────────────────────────────
        if self._is_enabled(cons, "teacher_conflict"):
            # Group sessions by teacher
            from collections import defaultdict
            teacher_sessions: dict[str, list[int]] = defaultdict(list)
            for s, ses in enumerate(sessions):
                if ses.teacher:
                    teacher_sessions[ses.teacher].append(s)
            for teacher, sess_list in teacher_sessions.items():
                if len(sess_list) > 1:
                    for t in range(n_t):
                        model.add(
                            sum(x[s][r][t] for s in sess_list for r in range(n_r)) <= 1
                        )

        # ── Hard: capacity ─────────────────────────────────────────────────────
        if self._is_enabled(cons, "capacity"):
            for s, ses in enumerate(sessions):
                if ses.size and ses.size > 0:
                    for r, room in enumerate(rooms):
                        # room capacity stored in attributes or capacity field
                        room_cap = int(room.attributes.get("capacity", room.capacity * 20))
                        if room_cap < ses.size:
                            for t in range(n_t):
                                model.add(x[s][r][t] == 0)

        # ── Objective ─────────────────────────────────────────────────────────
        assign_weight = 1000
        obj_terms = [
            assign_weight * x[s][r][t]
            for s in range(n_s) for r in range(n_r) for t in range(n_t)
        ]

        # Soft: spread sessions (penalise same timeslot having many sessions)
        if self._is_enabled(cons, "spread"):
            w = int(self._weight(cons, "spread", 5) * 10)
            for t in range(n_t):
                sessions_in_slot = model.new_int_var(0, n_s, f"load_t{t}")
                model.add(sessions_in_slot == sum(x[s][r][t] for s in range(n_s) for r in range(n_r)))
                # Penalise overloaded slots (above 2)
                overflow = model.new_int_var(0, n_s, f"overflow_t{t}")
                model.add(overflow >= sessions_in_slot - 2)
                model.add(overflow >= 0)
                obj_terms.append(-w * overflow)

        model.maximize(sum(obj_terms))

        # ── Solve ─────────────────────────────────────────────────────────────
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
                assignments=[], unassigned=[
                    UnassignedTask(task_id=s.id, task_name=s.name, reason="Infeasible")
                    for s in sessions
                ],
                violations=[], gantt=[],
                solver_info={"cp_status": str(solver.status_name)},
            )

        assignments:  list[Assignment]     = []
        unassigned:   list[UnassignedTask] = []
        gantt_map:    dict[str, list[GanttBar]] = {}

        for s, ses in enumerate(sessions):
            placed = False
            for r, room in enumerate(rooms):
                for t, slot in enumerate(timeslots):
                    if solver.value(x[s][r][t]):
                        label = f"{room.name} · {slot.day or ''} {slot.start}–{slot.end}"
                        assignments.append(Assignment(
                            task_id=ses.id,
                            task_name=ses.name,
                            resource_id=room.id,
                            resource_name=room.name,
                            start=f"{slot.day or 'Mon'} {slot.start}",
                            end=f"{slot.day or 'Mon'} {slot.end}",
                            cost=float(ses.size or 0),
                            metadata={
                                "timeslot": f"{slot.day} {slot.start}",
                                "teacher": ses.teacher or "",
                                "room": room.name,
                            },
                        ))
                        bar = GanttBar(
                            task_id=ses.id, task_name=ses.name,
                            start_offset=float(t * 60),
                            duration=60.0,
                        )
                        gantt_map.setdefault(room.id, []).append(bar)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                unassigned.append(UnassignedTask(
                    task_id=ses.id, task_name=ses.name,
                    reason="No room/timeslot available without conflict",
                ))

        slots_used = len({a.start for a in assignments})
        rooms_used = len({a.resource_id for a in assignments})

        kpis = [
            KPI(key="sessions_scheduled", value=len(assignments)),
            KPI(key="unscheduled",        value=len(unassigned)),
            KPI(key="rooms_used",         value=rooms_used),
            KPI(key="timeslots_used",     value=slots_used),
        ]

        gantt = [
            GanttRow(resource_id=rid, resource_name=next((r.name for r in rooms if r.id == rid), rid), bars=bars)
            for rid, bars in gantt_map.items()
        ]

        return ScheduleResponse(
            status=status, category=self.category, subtype=self.subtype,
            solve_time_ms=0,
            score=ScoreDetail(hard_violations=0, soft_score=float(len(unassigned))),
            kpis=kpis, assignments=assignments, unassigned=unassigned,
            violations=[], gantt=gantt,
            solver_info={"cp_status": str(solver.status_name)},
        )

    def _infeasible(self, reason: str) -> ScheduleResponse:
        return ScheduleResponse(
            status=SolveStatus.INFEASIBLE, category=self.category, subtype=self.subtype,
            solve_time_ms=0, score=ScoreDetail(), kpis=[],
            assignments=[], unassigned=[], violations=[
                ConstraintViolation(constraint="input", details=reason, severity="hard")
            ], gantt=[],
        )
