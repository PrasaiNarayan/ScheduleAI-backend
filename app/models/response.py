"""
Response models — unified output for all problem types.
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel


class SolveStatus(str, Enum):
    OPTIMAL     = "OPTIMAL"
    FEASIBLE    = "FEASIBLE"
    INFEASIBLE  = "INFEASIBLE"
    TIMEOUT     = "TIMEOUT"
    ERROR       = "ERROR"


class Assignment(BaseModel):
    task_id:      str
    task_name:    str
    resource_id:  str
    resource_name: str
    start:        str                 # ISO datetime or "HH:MM"
    end:          str
    cost:         float = 0.0
    metadata:     dict[str, Any] = {}


class UnassignedTask(BaseModel):
    task_id:   str
    task_name: str
    reason:    str


class ConstraintViolation(BaseModel):
    constraint: str
    details:    str
    severity:   str = "soft"         # soft | hard


class ScoreDetail(BaseModel):
    hard_violations:  int = 0
    medium_score:     float = 0.0    # unassigned tasks (negated)
    soft_score:       float = 0.0    # weighted penalties


class KPI(BaseModel):
    key:   str
    value: Any
    unit:  str = ""


class GanttBar(BaseModel):
    task_id:      str
    task_name:    str
    start_offset: float   # minutes from horizon start
    duration:     float   # minutes


class GanttRow(BaseModel):
    resource_id:   str
    resource_name: str
    bars:          list[GanttBar]


class ScheduleResponse(BaseModel):
    status:          SolveStatus
    category:        str
    subtype:         str
    solve_time_ms:   int

    score:           ScoreDetail
    kpis:            list[KPI]

    assignments:     list[Assignment]
    unassigned:      list[UnassignedTask]
    violations:      list[ConstraintViolation]
    gantt:           list[GanttRow]

    # Raw solver info
    solver_info:     dict[str, Any] = {}
