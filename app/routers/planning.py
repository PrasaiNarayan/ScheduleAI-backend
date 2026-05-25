"""
routers/planning.py — Database-driven planning with multi-step job support
"""
from __future__ import annotations
import time
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import db

router = APIRouter(prefix="/plans", tags=["planning"])


class PlanCreate(BaseModel):
    name:               str = "Plan"
    start_date:         str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date:           str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    allergen_order:     str = "A,B,C,D,E,F"
    notes:              str = ""
    time_limit_seconds: int = 60


class ReplanBody(BaseModel):
    start_date:         Optional[str] = None
    end_date:           Optional[str] = None
    allergen_order:     Optional[str] = None
    time_limit_seconds: int           = 60


class LockRequest(BaseModel):
    reason: str = ""


class LockStepRequest(BaseModel):
    assignment_id: int          # T_PlanAssignment.id — specific bar in Gantt
    reason:        str = ""


class LockDateRangeRequest(BaseModel):
    date_from: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to:   str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    locked:    bool = True
    reason:    str  = ""


class PlanOut(BaseModel):
    id:             int
    name:           str
    start_date:     str
    end_date:       str
    allergen_order: str
    status:         str
    solve_time_ms:  Optional[int]
    jobs_scheduled: Optional[int]
    jobs_total:     Optional[int]
    notes:          Optional[str]


def _safe_date(v) -> str:
    if v is None:
        return ""
    from datetime import date as _d, datetime as _dt
    if isinstance(v, (_d, _dt)):
        return v.strftime("%Y-%m-%d")
    s = str(v)[:10]
    return s if s != "None" else ""


@router.get("/schedule")
def get_consolidated_schedule(
    start_date:      str  = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date:        str  = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    include_solved:  bool = Query(True),
    include_partial: bool = Query(False),
):
    from datetime import date as _date

    all_plans = db.get_plans(status="published")
    seen_ids  = {p["id"] for p in all_plans}
    if include_solved:
        for p in db.get_plans(status="solved"):
            if p["id"] not in seen_ids:
                all_plans.append(p); seen_ids.add(p["id"])
    if include_partial:
        for p in db.get_plans(status="partial"):
            if p["id"] not in seen_ids:
                all_plans.append(p); seen_ids.add(p["id"])
    all_plans.sort(key=lambda p: p["created_at"] or "", reverse=True)

    if not all_plans:
        return {"start_date": start_date, "end_date": end_date,
                "by_date": {}, "plans_used": []}

    start = _date.fromisoformat(start_date)
    end   = _date.fromisoformat(end_date)
    all_dates = []
    d = start
    while d <= end:
        all_dates.append(str(d)); d += timedelta(days=1)

    date_to_plan: dict = {}
    for plan in all_plans:
        for date_str in all_dates:
            if date_str not in date_to_plan:
                if plan["start_date"] <= date_str <= plan["end_date"]:
                    date_to_plan[date_str] = plan

    plan_assignments: dict = {}
    for pid in {p["id"] for p in date_to_plan.values()}:
        plan_assignments[pid] = db.get_multi_step_assignments(pid)

    by_date: dict = {}
    for date_str in all_dates:
        if date_str not in date_to_plan:
            by_date[date_str] = {"date": date_str, "plan_id": None,
                "plan_name": None, "covered": False,
                "assignments": [], "by_machine": {}}
            continue
        plan = date_to_plan[date_str]
        pid  = plan["id"]
        day_a = [a for a in plan_assignments[pid]
                 if a["scheduled_date"] == date_str]
        by_machine: dict = {}
        for a in day_a:
            by_machine.setdefault(a["machine_name"], []).append(a)
        for m in by_machine:
            by_machine[m].sort(key=lambda x: x["start_time"] or "")
        by_date[date_str] = {"date": date_str, "plan_id": pid,
            "plan_name": plan["name"], "covered": True,
            "assignments": day_a, "by_machine": by_machine}

    seen_pids: set = set()
    plans_used = []
    for date_str in sorted(date_to_plan):
        p = date_to_plan[date_str]
        if p["id"] not in seen_pids:
            seen_pids.add(p["id"])
            plans_used.append({"plan_id": p["id"], "plan_name": p["name"],
                                "status": p["status"]})

    return {
        "start_date": start_date, "end_date": end_date,
        "by_date": by_date, "plans_used": plans_used,
        "total_jobs":   sum(len(v["assignments"]) for v in by_date.values()),
        "covered_days": sum(1 for v in by_date.values() if v["covered"]),
    }


@router.post("", status_code=201)
def create_and_solve(body: PlanCreate):
    plan_id = db.create_plan(
        start_date=body.start_date, end_date=body.end_date,
        allergen_order=body.allergen_order, name=body.name, notes=body.notes,
    )
    try:
        result = _run_solver(plan_id=plan_id, start_date=body.start_date,
                             end_date=body.end_date,
                             allergen_order=body.allergen_order,
                             time_limit=body.time_limit_seconds)
    except Exception as e:
        db.update_plan_status(plan_id, "draft")
        raise HTTPException(500, f"Solver error: {e}")
    return {
        "plan_id": plan_id, "status": result["status"],
        "jobs_scheduled": result["jobs_scheduled"],
        "jobs_total": result["jobs_total"],
        "steps_scheduled": result["steps_scheduled"],
        "jobs_unscheduled": result["jobs_unscheduled"],
        "solve_time_ms": result["solve_time_ms"],
        "message": (f"Plan {plan_id} created. "
                    f"{result['jobs_scheduled']}/{result['jobs_total']} products scheduled."),
    }


@router.get("", response_model=list[PlanOut])
def list_plans(status: Optional[str] = Query(None)):
    return db.get_plans(status=status)


@router.get("/{plan_id}")
def get_plan(plan_id: int):
    plans = db.get_plans()
    plan  = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        raise HTTPException(404, "Plan not found")
    plan["assignments"] = db.get_multi_step_assignments(plan_id)
    return plan


@router.get("/{plan_id}/assignments")
def get_assignments(plan_id: int):
    assignments = db.get_multi_step_assignments(plan_id)
    unscheduled = db.get_unscheduled(plan_id)

    by_machine: dict = {}
    for a in assignments:
        m = a["machine_name"]; d = a["scheduled_date"]
        by_machine.setdefault(m, {}).setdefault(d, []).append(a)
    for m in by_machine:
        for d in by_machine[m]:
            by_machine[m][d].sort(key=lambda x: x["start_time"] or "")

    by_product: dict = {}
    for a in assignments:
        pid = a["product_id"]
        by_product.setdefault(pid, {
            "product_id": pid, "product_name": a["product_name"],
            "allergen": a["allergen"], "locked": a["locked"], "steps": [],
        })["steps"].append(a)
    for pid in by_product:
        by_product[pid]["steps"].sort(key=lambda x: x["step_number"] or 0)

    return {
        "plan_id": plan_id,
        "assignments": assignments,
        "by_machine": by_machine,
        "by_product": list(by_product.values()),
        "locked_count": sum(1 for a in assignments if a["locked"]),
        "unscheduled": unscheduled,
        "unscheduled_count": len(unscheduled),
    }


@router.post("/{plan_id}/replan")
def replan(plan_id: int, body: Optional[ReplanBody] = None):
    plans = db.get_plans()
    plan  = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        raise HTTPException(404, "Plan not found")

    start_date     = body.start_date     if (body and body.start_date)     else _safe_date(plan["start_date"])
    end_date       = body.end_date       if (body and body.end_date)       else _safe_date(plan["end_date"])
    allergen_order = body.allergen_order if (body and body.allergen_order) else plan["allergen_order"]
    time_limit     = body.time_limit_seconds if body else 60

    if not start_date or not end_date:
        raise HTTPException(400, f"Could not resolve dates from plan {plan_id}")

    try:
        result = _run_solver(plan_id=plan_id, start_date=start_date,
                             end_date=end_date, allergen_order=allergen_order,
                             time_limit=time_limit)
    except Exception as e:
        raise HTTPException(500, f"Solver error: {e}")

    locked = db.get_locked_assignments(plan_id)
    return {
        "plan_id": plan_id, "status": result["status"],
        "jobs_scheduled": result["jobs_scheduled"],
        "jobs_total": result["jobs_total"],
        "steps_scheduled": result["steps_scheduled"],
        "jobs_unscheduled": result["jobs_unscheduled"],
        "locked_kept": len(locked),
        "solve_time_ms": result["solve_time_ms"],
        "message": (f"Replanned. {result['jobs_scheduled']}/{result['jobs_total']} "
                    f"products scheduled."),
    }


# ── Soft lock product (day-locked, time flexible) ────────────────────────────
@router.patch("/{plan_id}/soft-lock/product/{product_id}")
def soft_lock_product(plan_id: int, product_id: int, body: LockRequest):
    """Soft lock: product must stay on same day but solver picks best time/machine."""
    n = db.soft_lock_product(plan_id, product_id, locked=True, reason=body.reason)
    if n == 0:
        raise HTTPException(404, "No assignments found for this product")
    return {"message": f"Product {product_id} soft-locked (day fixed, time flexible)",
            "rows": n}


@router.patch("/{plan_id}/soft-unlock/product/{product_id}")
def soft_unlock_product(plan_id: int, product_id: int):
    """Remove soft lock from a product."""
    n = db.soft_lock_product(plan_id, product_id, locked=False)
    if n == 0:
        raise HTTPException(404, "No assignments found for this product")
    return {"message": f"Product {product_id} soft-lock removed", "rows": n}


# ── Lock single step (one bar in Gantt) ──────────────────────────────────────
@router.patch("/{plan_id}/lock/step")
def lock_step(plan_id: int, body: LockStepRequest):
    """Lock or unlock a single step assignment (one bar in the Gantt chart)."""
    n = db.lock_step(plan_id, body.assignment_id,
                     locked=True, reason=body.reason)
    if n == 0:
        raise HTTPException(404, "Assignment not found")
    return {"message": f"Step {body.assignment_id} locked", "rows": n}


@router.patch("/{plan_id}/unlock/step")
def unlock_step(plan_id: int, body: LockStepRequest):
    """Unlock a single step assignment."""
    n = db.lock_step(plan_id, body.assignment_id,
                     locked=False, reason="")
    if n == 0:
        raise HTTPException(404, "Assignment not found")
    return {"message": f"Step {body.assignment_id} unlocked", "rows": n}


# ── Lock all steps of one product ─────────────────────────────────────────────
@router.patch("/{plan_id}/lock/product/{product_id}")
def lock_product(plan_id: int, product_id: int, body: LockRequest):
    """Lock all steps of a product in this plan."""
    n = db.lock_product(plan_id, product_id,
                        locked=True, reason=body.reason)
    if n == 0:
        raise HTTPException(404, "No assignments found for this product")
    return {"message": f"All steps of product {product_id} locked", "rows": n}


@router.patch("/{plan_id}/unlock/product/{product_id}")
def unlock_product(plan_id: int, product_id: int):
    """Unlock all steps of a product in this plan."""
    n = db.lock_product(plan_id, product_id, locked=False)
    if n == 0:
        raise HTTPException(404, "No assignments found for this product")
    return {"message": f"All steps of product {product_id} unlocked", "rows": n}


# ── Lock by date range ────────────────────────────────────────────────────────
@router.patch("/{plan_id}/lock/dates")
def lock_dates(plan_id: int, body: LockDateRangeRequest):
    """Lock or unlock all assignments whose scheduled_date falls in the range."""
    n = db.lock_date_range(plan_id,
                           date_from=body.date_from,
                           date_to=body.date_to,
                           locked=body.locked,
                           reason=body.reason)
    action = "locked" if body.locked else "unlocked"
    return {
        "message": f"{n} assignments {action} for {body.date_from} → {body.date_to}",
        "rows": n,
    }


# ── Legacy endpoints (keep for backwards compatibility) ───────────────────────
@router.patch("/{plan_id}/assignments/{product_id}/lock")
def lock_assignment(plan_id: int, product_id: int, body: LockRequest):
    n = db.lock_product(plan_id, product_id, locked=True, reason=body.reason)
    if n == 0:
        raise HTTPException(404, "Assignment not found")
    return {"message": f"Product {product_id} locked in plan {plan_id}"}


@router.patch("/{plan_id}/assignments/{product_id}/unlock")
def unlock_assignment(plan_id: int, product_id: int):
    n = db.lock_product(plan_id, product_id, locked=False)
    if n == 0:
        raise HTTPException(404, "Assignment not found")
    return {"message": f"Product {product_id} unlocked in plan {plan_id}"}


@router.patch("/{plan_id}/publish")
def publish_plan(plan_id: int):
    plans = db.get_plans()
    if not any(p["id"] == plan_id for p in plans):
        raise HTTPException(404, "Plan not found")
    db.update_plan_status(plan_id, "published")
    return {"message": f"Plan {plan_id} approved"}


@router.patch("/{plan_id}/archive")
def archive_plan(plan_id: int):
    db.update_plan_status(plan_id, "archived")
    return {"message": f"Plan {plan_id} archived"}


def _run_solver(plan_id: int, start_date: str, end_date: str,
                allergen_order: str, time_limit: int) -> dict:
    from app.solvers.multi_step import solve_multi_step

    t0 = time.perf_counter()

    all_days     = db.call_get_working_days(start_date, end_date)
    working_days = [{"date": db._fmt_date(d["work_date"]), "day_name": d["day_name"]}
                    for d in all_days if not d["is_holiday"]]

    if not working_days:
        raise ValueError("No working days in the selected date range")

    start_date_obj = date.fromisoformat(start_date)

    machine_hours_by_day: dict = {}
    for day in working_days:
        hours = db.call_get_machine_hours(day["date"])
        machine_hours_by_day[day["date"]] = {}
        for mh in hours:
            if not mh["closed"] and mh["shift_start"] and mh["shift_end"]:
                def _to_min(t):
                    if hasattr(t, 'hour'):
                        return t.hour * 60 + t.minute
                    parts = str(t).split(":")
                    return int(parts[0]) * 60 + int(parts[1])
                machine_hours_by_day[day["date"]][mh["machine_name"]] = (
                    _to_min(mh["shift_start"]), _to_min(mh["shift_end"]),
                )

    products = db.get_order_lines_with_steps(status="open")
    if not products:
        # Fallback: if no open order lines, plan all active products
        products = db.get_products_with_steps(active_only=True)
        using_orders = False
    else:
        using_orders = True

    if not products:
        raise ValueError("No open order lines or active products with step definitions")

    # For order-line-based planning: each order line is a separate job
    # Two order lines for the same product = two separate production batches
    # job_label shows in logs: "Almond Cookie [ORD-001]" vs "Almond Cookie [ORD-003]"
    if using_orders:
        for ol in products:
            ol["name"] = f"{ol['product_name']} [{ol.get('order_number','?')}]"
            ol["id"]   = ol["order_line_id"]   # solver uses 'id' as job key

    machines_by_type = db.get_machines_by_type()

    # Check if all products are already past deadline — skip solver entirely
    from datetime import date as _date
    all_overdue = all(
        (p.get("deadline") and
         (_date.fromisoformat(str(p["deadline"])[:10]) - start_date_obj).days < 0)
        for p in products
    )

    if all_overdue:
        plan_status = "failed"
        db.update_plan_status(plan_id=plan_id, status=plan_status,
                              solve_time_ms=0, jobs_scheduled=0,
                              jobs_total=len(products))
        db.save_unscheduled(plan_id, [
            {
                "product_id":    p.get("product_id") or p["id"],
                "order_line_id": p.get("order_line_id"),
                "reason":        f"Deadline {p.get('deadline')} already passed",
            }
            for p in products
        ])
        return {
            "plan_id":          plan_id,
            "status":           "DEADLINE_PASSED",
            "jobs_scheduled":   0,
            "jobs_total":       len(products),
            "steps_scheduled":  0,
            "jobs_unscheduled": len(products),
            "solve_time_ms":    0,
            "message":          f"Plan {plan_id} created. 0/{len(products)} products scheduled — all deadlines already passed.",
        }

    result = solve_multi_step(
        products_with_steps=products,
        machines_by_type=machines_by_type,
        machine_hours_by_day=machine_hours_by_day,
        working_days=working_days,
        allergen_order=allergen_order.split(","),
        start_date=start_date_obj,
        time_limit_seconds=time_limit,
        locked_assignments=db.get_multi_step_assignments(plan_id)
            if plan_id else [],
    )

    solve_ms = int((time.perf_counter() - t0) * 1000)
    scheduled_product_ids = {a["product_id"] for a in result["assignments"]}
    jobs_scheduled = len({(a["product_id"], a.get("order_line_id")) for a in result["assignments"]})
    jobs_total     = len(products)

    # Attach order_line_id and product_id to each assignment before saving
    if using_orders:
        ol_map = {ol["order_line_id"]: ol for ol in products}
        for a in result["assignments"]:
            job_id = a.get("job_id") or a.get("product_id")
            if job_id in ol_map:
                a["order_line_id"] = ol_map[job_id]["order_line_id"]
                a["product_id"]    = ol_map[job_id]["product_id"]

    db.save_multi_step_assignments(plan_id, result["assignments"])

    # Deduplicate unscheduled by (order_line_id, product_id) before saving
    # Multiple reasons can produce the same product entry — keep first occurrence
    seen_unscheduled = set()
    deduped_unscheduled = []
    for u in result["unscheduled"]:
        key = (u.get("order_line_id"), u.get("product_id"))
        if key not in seen_unscheduled:
            seen_unscheduled.add(key)
            deduped_unscheduled.append({
                "product_id":    u.get("product_id"),
                "order_line_id": u.get("order_line_id"),
                "reason":        u.get("reason", "Could not schedule"),
            })
    db.save_unscheduled(plan_id, deduped_unscheduled)

    if jobs_scheduled == 0:
        plan_status = "failed"
    elif jobs_scheduled == jobs_total:
        plan_status = "solved"
    else:
        plan_status = "partial"

    db.update_plan_status(plan_id=plan_id, status=plan_status,
                          solve_time_ms=solve_ms,
                          jobs_scheduled=jobs_scheduled,
                          jobs_total=jobs_total)

    return {
        "status":           result["status"],
        "jobs_scheduled":   jobs_scheduled,
        "jobs_total":       jobs_total,
        "steps_scheduled":  len(result["assignments"]),
        "jobs_unscheduled": len(result["unscheduled"]),
        "solve_time_ms":    solve_ms,
    }