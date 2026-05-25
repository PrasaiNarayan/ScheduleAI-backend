"""
Problem Classifier
==================
Auto-detects the scheduling problem category and subtype from the input data
shape when the caller doesn't declare them explicitly.

Detection priority:
  1. Explicit category/subtype in request   → use as-is
  2. Structural heuristics on input fields  → classify
  3. Fallback                               → raise 422

Heuristic rules
---------------
routing      → any task has `location` OR `time_window_open` OR demand > 0
               sub: tsp if num_vehicles==1, vrptw if time windows present, else vrp

jobshop      → any task has non-empty `operations` list
               sub: flexible if multiple machines per op are allowed (flagged by
                    attribute), openshop if attribute present, else classic

timetabling  → `timeslots` list is non-empty OR any task has `teacher` / `size`
               sub: school if teacher present, meeting if size<=20 avg, else conference

workforce    → fallback when none of the above match
               sub: oncall if tasks have time_window_*, roster if >5 shifts, else shift
"""
from __future__ import annotations

from app.logger import get_logger
from app.models.request import (
    ProblemCategory, ScheduleRequest,
    WorkforceSubtype, JobshopSubtype, RoutingSubtype, TimetablingSubtype,
)

_log = get_logger(__name__)


def classify(req: ScheduleRequest) -> tuple[ProblemCategory, str]:
    cat, sub = _classify_inner(req)
    _log.info("Classified  category=%s  subtype=%s  explicit=%s",
              cat.value, sub, req.category is not None)
    return cat, sub


def _classify_inner(req: ScheduleRequest) -> tuple[ProblemCategory, str]:
    """
    Returns (category, subtype).
    Raises ValueError if the problem cannot be classified.
    """
    # ── 1. Explicit declaration ───────────────────────────────────────────────
    if req.category is not None:
        subtype = req.subtype or _default_subtype(req.category, req)
        return req.category, subtype

    # ── 2. Routing detection ─────────────────────────────────────────────────
    has_location   = any(t.location for t in req.tasks)
    has_tw         = any(t.time_window_open for t in req.tasks)
    has_demand     = any(t.demand and t.demand > 0 for t in req.tasks)
    # Resources of type vehicle, or num_vehicles declared
    has_vehicles   = (
        any(r.type == "vehicle" for r in req.resources)
        or (req.num_vehicles is not None and req.num_vehicles >= 1)
    )

    if has_location or has_tw or has_demand or has_vehicles:
        n_vehicles = req.num_vehicles or sum(1 for r in req.resources if r.type == "vehicle") or 1
        if n_vehicles == 1:
            sub = RoutingSubtype.tsp
        elif has_tw:
            sub = RoutingSubtype.vrptw
        else:
            sub = RoutingSubtype.vrp
        return ProblemCategory.routing, sub.value

    # ── 3. Job-shop detection ────────────────────────────────────────────────
    has_ops = any(t.operations for t in req.tasks)
    if has_ops:
        # flexible: task attribute or multiple machine options per op
        if any(t.attributes.get("flexible") for t in req.tasks):
            sub = JobshopSubtype.flexible
        elif any(t.attributes.get("openshop") for t in req.tasks):
            sub = JobshopSubtype.openshop
        else:
            sub = JobshopSubtype.classic
        return ProblemCategory.jobshop, sub.value

    # ── 4. Timetabling detection ─────────────────────────────────────────────
    has_slots   = len(req.timeslots) > 0
    has_teacher = any(t.teacher for t in req.tasks)
    has_size    = any(t.size and t.size > 0 for t in req.tasks)
    has_rooms   = any(r.type == "room" for r in req.resources)

    if has_slots or has_teacher or has_size or has_rooms:
        if has_teacher:
            sub = TimetablingSubtype.school
        else:
            avg_size = (
                sum(t.size for t in req.tasks if t.size) / max(sum(1 for t in req.tasks if t.size), 1)
            )
            sub = TimetablingSubtype.meeting if avg_size <= 20 else TimetablingSubtype.conference
        return ProblemCategory.timetabling, sub.value

    # ── 5. Workforce fallback ────────────────────────────────────────────────
    if req.resources or req.tasks:
        has_tw_tasks = any(t.time_window_open for t in req.tasks)
        n_tasks      = len(req.tasks)
        if has_tw_tasks:
            sub = WorkforceSubtype.oncall
        elif n_tasks > 5:
            sub = WorkforceSubtype.roster
        else:
            sub = WorkforceSubtype.shift
        return ProblemCategory.workforce, sub.value

    raise ValueError(
        "Cannot classify problem: no recognisable fields found. "
        "Please set `category` and `subtype` explicitly, or provide "
        "resources, tasks, timeslots, or vehicles."
    )


def _default_subtype(category: ProblemCategory, req: ScheduleRequest) -> str:
    if category == ProblemCategory.workforce:
        return WorkforceSubtype.shift.value
    if category == ProblemCategory.jobshop:
        return JobshopSubtype.classic.value
    if category == ProblemCategory.routing:
        n = req.num_vehicles or 1
        return RoutingSubtype.tsp.value if n == 1 else RoutingSubtype.vrp.value
    if category == ProblemCategory.timetabling:
        return TimetablingSubtype.school.value
    return "default"
