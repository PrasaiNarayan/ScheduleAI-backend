"""
Request models — unified input schema for all scheduling problem types.
The same /solve endpoint accepts all variants; the classifier picks the solver.
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────

class ProblemCategory(str, Enum):
    workforce    = "workforce"
    jobshop      = "jobshop"
    routing      = "routing"
    timetabling  = "timetabling"


class WorkforceSubtype(str, Enum):
    shift   = "shift"
    roster  = "roster"
    oncall  = "oncall"


class JobshopSubtype(str, Enum):
    classic   = "classic"
    flexible  = "flexible"
    openshop  = "openshop"


class RoutingSubtype(str, Enum):
    vrp   = "vrp"
    vrptw = "vrptw"
    tsp   = "tsp"


class TimetablingSubtype(str, Enum):
    school     = "school"
    meeting    = "meeting"
    conference = "conference"


class ConstraintTier(str, Enum):
    hard   = "hard"
    medium = "medium"
    soft   = "soft"


# ── Building blocks ───────────────────────────────────────────────────────────

class AvailabilityWindow(BaseModel):
    day:       Optional[str] = None          # "monday" | ISO date "2026-04-07"
    start:     str                           # "08:00"
    end:       str                           # "17:00"
    timezone:  str = "UTC"


class Resource(BaseModel):
    id:               str
    name:             str
    type:             str = "human"          # human | machine | room | vehicle
    skills:           list[str] = []
    capacity:         int = 1               # parallel tasks at once
    cost_per_hour:    float = 0.0
    location:         Optional[dict[str, float]] = None  # {lat, lng}
    availability:     list[AvailabilityWindow] = []
    attributes:       dict[str, Any] = {}


class Operation(BaseModel):
    """One step in a job-shop job — which machine and how long."""
    machine_id:   str
    duration_min: int = Field(default=1, ge=0)


class Task(BaseModel):
    id:                    str
    name:                  str
    duration_minutes:      int = Field(default=0, ge=0)
    priority:              int = 1
    required_skills:       list[str] = []
    required_resource_type: Optional[str] = None
    earliest_start:        Optional[str] = None   # ISO datetime or "HH:MM"
    deadline:              Optional[str] = None
    dependencies:          list[str] = []
    location:              Optional[dict[str, float]] = None
    setup_minutes:         int = 0
    teardown_minutes:      int = 0
    demand:                int = 0            # units (routing)
    time_window_open:      Optional[str] = None
    time_window_close:     Optional[str] = None
    operations:            list[Operation] = []   # job-shop ops
    teacher:               Optional[str] = None   # timetabling
    size:                  int = 0           # timetabling attendees
    machine_id:            Optional[str] = None   # single-machine-per-job (production model)
    machine_options:       list[dict] = []         # flexible: [{machine_id, duration_min}, ...]
    allergen:              Optional[str] = None   # allergen label e.g. "nuts"
    attributes:            dict[str, Any] = {}


class Timeslot(BaseModel):
    id:    str
    day:   Optional[str] = None
    start: str
    end:   str


class ConstraintDef(BaseModel):
    type:       str
    tier:       ConstraintTier = ConstraintTier.hard
    weight:     float = 1.0
    enabled:    bool = True
    expression: Optional[str] = None         # custom rule DSL (future)
    params:     dict[str, Any] = {}


class SolverConfig(BaseModel):
    algorithm:            str = "cp_sat"     # cp_sat | vrp
    time_limit_seconds:   int = Field(default=30, ge=1, le=300)
    num_workers:          int = Field(default=4, ge=1, le=16)
    horizon_start:        Optional[str] = None
    horizon_end:          Optional[str] = None
    allow_partial_solution: bool = True


# ── Top-level request ─────────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    """
    Universal scheduling request.  Pass `category` + `subtype` explicitly,
    or leave them null and the classifier will infer from your data shape.
    """
    category:       Optional[ProblemCategory] = None
    subtype:        Optional[str] = None

    resources:      list[Resource] = []
    tasks:          list[Task] = []
    timeslots:      list[Timeslot] = []      # timetabling
    constraints:    list[ConstraintDef] = []
    solver_config:  SolverConfig = Field(default_factory=SolverConfig)

    # Extended production scheduling
    allergen_order:  list[str] = []   # e.g. ["nuts","dairy","gluten"]
    attributes:      dict[str, Any] = {}  # solver-level config

    # Convenience: depot name for routing
    depot_name:     str = "Depot"
    num_vehicles:   Optional[int] = None
    vehicle_capacity: Optional[int] = None

    model_config = {"json_schema_extra": {"examples": []}}