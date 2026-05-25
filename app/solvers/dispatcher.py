"""
Solver dispatcher — routes to the right solver based on category + subtype.
"""
from __future__ import annotations
from app.models.request  import ProblemCategory, ScheduleRequest
from app.models.response import ScheduleResponse
from app.solvers.workforce   import WorkforceSolver
from app.solvers.jobshop     import JobshopSolver
from app.solvers.routing     import RoutingSolver
from app.solvers.timetabling import TimetablingSolver
from app.solvers.production  import ProductionSolver


_SOLVER_MAP = {
    ProblemCategory.workforce:   WorkforceSolver,
    ProblemCategory.jobshop:     JobshopSolver,
    ProblemCategory.routing:     RoutingSolver,
    ProblemCategory.timetabling: TimetablingSolver,
}

# Production subtype — extended job shop with allergen/chain/position constraints
_PRODUCTION_SUBTYPES = {"production", "allergen", "extended"}


def dispatch(req: ScheduleRequest, category: ProblemCategory, subtype: str) -> ScheduleResponse:
    # Route production subtype to dedicated solver
    if category == ProblemCategory.jobshop and subtype in _PRODUCTION_SUBTYPES:
        solver = ProductionSolver(req, category.value, subtype)
        return solver.run()

    # Also auto-route if any job has allergen or depends_on attributes
    if category == ProblemCategory.jobshop:
        has_allergen  = any((t.attributes or {}).get("allergen") or t.allergen for t in req.tasks)
        has_depends   = any((t.attributes or {}).get("depends_on") for t in req.tasks)
        has_position  = any((t.attributes or {}).get("position") for t in req.tasks)
        has_allergen_order = bool(req.allergen_order)
        if has_allergen or has_depends or has_position or has_allergen_order:
            solver = ProductionSolver(req, category.value, "production")
            return solver.run()

    solver_cls = _SOLVER_MAP.get(category)
    if not solver_cls:
        raise ValueError(f"No solver registered for category: {category}")
    return solver_cls(req, category.value, subtype).run()
