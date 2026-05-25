"""
routers/data.py — Data management endpoints for new M_/T_ schema

Master:      GET /allergens, /categories, /machine-types
Products:    GET/POST /products, GET/PUT/DELETE /products/{id}
Orders:      GET/POST /orders, GET/POST /orders/{id}/lines
Machines:    GET/POST /machines, PATCH /machines/{id}/deactivate, DELETE /machines/{id}
Overrides:   GET/POST /overrides, DELETE /overrides/{machine_id}/{date}
Holidays:    GET/POST /holidays, DELETE /holidays/{date}
Calendar:    GET /calendar/working-days, GET /calendar/machine-hours/{date}
"""
from __future__ import annotations
import csv
import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field

from app import db

router = APIRouter(tags=["data"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class MessageOut(BaseModel):
    message: str

class MachineCreate(BaseModel):
    name:                str
    machine_type_id:     int
    default_shift_start: str = "07:20"
    default_shift_end:   str = "22:00"
    notes:               str = ""

class MachineUpdate(BaseModel):
    name:                Optional[str] = None
    default_shift_start: Optional[str] = None
    default_shift_end:   Optional[str] = None
    notes:               Optional[str] = None
    active:              Optional[bool] = None

class OverrideCreate(BaseModel):
    machine_id:    int
    override_date: str
    shift_start:   Optional[str] = None
    shift_end:     Optional[str] = None
    closed:        bool = False
    reason:        str  = ""

class HolidayCreate(BaseModel):
    holiday_date: str
    name:         str
    holiday_type: str = "public"
    notes:        str = ""

class ProductCreate(BaseModel):
    name:        str
    category_id: int
    allergen_id: Optional[int] = None
    priority:    int = Field(5, ge=1, le=10)
    notes:       str = ""
    position:    Optional[str] = None  # 'first', 'last', or None

class ProductUpdate(BaseModel):
    name:        Optional[str] = None
    category_id: Optional[int] = None
    allergen_id: Optional[int] = None
    priority:    Optional[int] = None
    notes:       Optional[str] = None
    position:    Optional[str] = None  # 'first', 'last', or None

class OrderCreate(BaseModel):
    order_number:  str
    customer_name: str = ""
    order_date:    str
    required_date: str
    notes:         str = ""

class OrderLineCreate(BaseModel):
    product_id: int
    quantity:   int = Field(1, ge=1)
    deadline:   Optional[str] = None
    priority:   Optional[int] = Field(None, ge=1, le=10)
    notes:      str = ""

class StepDurationUpdate(BaseModel):
    category_step_id: int
    duration_minutes: int = Field(..., ge=1)


# ══════════════════════════════════════════════════════════════════════════════
# DEBUG
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/db-status", tags=["debug"])
def db_status():
    import pyodbc
    available_drivers = pyodbc.drivers()
    config = {
        "server":             db.DB_SERVER,
        "database":           db.DB_NAME,
        "driver_configured":  db.DB_DRIVER,
        "user":               db.DB_USER,
        "connection_string":  db.CONNECTION_STRING,
        "available_sql_drivers": [d for d in available_drivers if "SQL" in d.upper()],
    }
    try:
        with db.get_conn() as conn:
            result = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT @@VERSION AS v, DB_NAME() AS db"))
            row = result.fetchone()
            config["status"]      = "connected"
            config["db_name"]     = row[1]
            config["sql_version"] = row[0].split("\n")[0]
    except Exception as e:
        config["status"] = "failed"
        config["error"]  = str(e)
    return config


# ══════════════════════════════════════════════════════════════════════════════
# MASTER REFERENCE DATA
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/allergens")
def list_allergens():
    return db.get_allergens()


@router.get("/machine-types")
def list_machine_types():
    return db.get_machine_types()


@router.get("/categories")
def list_categories():
    return db.get_categories()


@router.get("/categories/{category_id}/steps")
def list_category_steps(category_id: int):
    return db.get_category_steps(category_id=category_id)


# ══════════════════════════════════════════════════════════════════════════════
# MACHINES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/machines")
def list_machines(active_only: bool = Query(True)):
    return db.get_machines(active_only=active_only)


@router.post("/machines", status_code=201)
def create_machine(body: MachineCreate):
    machine_id = db.create_machine(
        name=body.name,
        machine_type_id=body.machine_type_id,
        shift_start=body.default_shift_start,
        shift_end=body.default_shift_end,
        notes=body.notes,
    )
    return {"message": f"Machine '{body.name}' created", "id": machine_id}


@router.put("/machines/{machine_id}")
def update_machine(machine_id: int, body: MachineUpdate):
    machines = db.get_machines(active_only=False)
    m = next((x for x in machines if x["id"] == machine_id), None)
    if not m:
        raise HTTPException(404, "Machine not found")
    db.update_machine(machine_id, {
        "name":                body.name or m["name"],
        "default_shift_start": body.default_shift_start or m["default_shift_start"],
        "default_shift_end":   body.default_shift_end   or m["default_shift_end"],
        "notes":               body.notes if body.notes is not None else m.get("notes",""),
    })
    if body.active is not None:
        db.set_machine_active(machine_id, body.active)
    return {"message": f"Machine {machine_id} updated"}


@router.patch("/machines/{machine_id}/deactivate")
def deactivate_machine(machine_id: int):
    db.set_machine_active(machine_id, False)
    return {"message": f"Machine {machine_id} deactivated"}


@router.delete("/machines/{machine_id}")
def delete_machine(machine_id: int):
    n = db.delete_machine(machine_id)
    if n == 0:
        raise HTTPException(404, "Machine not found")
    return {"message": f"Machine {machine_id} deleted"}


# ══════════════════════════════════════════════════════════════════════════════
# SHIFT OVERRIDES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/overrides")
def list_overrides(
    machine_id: Optional[int] = Query(None),
    from_date:  Optional[str] = Query(None),
    to_date:    Optional[str] = Query(None),
):
    return db.get_shift_overrides(
        machine_id=machine_id, from_date=from_date, to_date=to_date)


@router.post("/overrides", status_code=201)
def create_override(body: OverrideCreate):
    db.upsert_shift_override(
        machine_id=body.machine_id,
        override_date=body.override_date,
        shift_start=body.shift_start,
        shift_end=body.shift_end,
        closed=body.closed,
        reason=body.reason,
    )
    action = "closed" if body.closed else f"{body.shift_start}–{body.shift_end}"
    return {"message": f"Override set for machine {body.machine_id} on {body.override_date}: {action}"}


@router.delete("/overrides/{machine_id}/{override_date}")
def delete_override(machine_id: int, override_date: str):
    n = db.delete_shift_override(machine_id, override_date)
    if n == 0:
        raise HTTPException(404, "Override not found")
    return {"message": f"Override removed"}


# ══════════════════════════════════════════════════════════════════════════════
# HOLIDAYS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/holidays")
def list_holidays(
    from_date: Optional[str] = Query(None),
    to_date:   Optional[str] = Query(None),
):
    return db.get_holidays(from_date=from_date, to_date=to_date)


@router.post("/holidays", status_code=201)
def create_holiday(body: HolidayCreate):
    db.upsert_holiday(
        holiday_date=body.holiday_date,
        name=body.name,
        holiday_type=body.holiday_type,
        notes=body.notes,
    )
    return {"message": f"Holiday '{body.name}' on {body.holiday_date} saved"}


@router.delete("/holidays/{holiday_date}")
def delete_holiday(holiday_date: str):
    n = db.delete_holiday(holiday_date)
    if n == 0:
        raise HTTPException(404, "Holiday not found")
    return {"message": f"Holiday on {holiday_date} removed"}


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTS (M_Product)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/products")
def list_products(
    active_only:    bool          = Query(True),
    allergen_code:  Optional[str] = Query(None),
    category_id:    Optional[int] = Query(None),
    deadline_from:  Optional[str] = Query(None),
    deadline_to:    Optional[str] = Query(None),
    planned_status: Optional[str] = Query(None),
):
    return db.get_products(
        active_only=active_only,
        allergen_code=allergen_code,
        category_id=category_id,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        planned_status=planned_status,
    )


@router.get("/products/{product_id}")
def get_product(product_id: int):
    p = db.get_product_by_id(product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    p["steps"] = db.get_product_step_durations(product_id)
    return p


@router.post("/products", status_code=201)
def create_product(body: ProductCreate):
    product_id = db.insert_product({
        "name":        body.name,
        "category_id": body.category_id,
        "allergen_id": body.allergen_id,
        "priority":    body.priority,
        "notes":       body.notes,
    })
    return {"message": f"Product '{body.name}' created", "id": product_id}


@router.put("/products/{product_id}")
def update_product(product_id: int, body: ProductUpdate):
    p = db.get_product_by_id(product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    updated = db.update_product(product_id, {
        "name":        body.name        or p["name"],
        "category_id": body.category_id or p["category_id"],
        "allergen_id": body.allergen_id if body.allergen_id is not None else p["allergen_id"],
        "priority":    body.priority    or p["priority"],
        "notes":       body.notes       if body.notes is not None else p.get("notes", ""),
    })
    if not updated:
        raise HTTPException(500, "Update failed")
    return {"message": f"Product {product_id} updated"}


@router.delete("/products/{product_id}")
def deactivate_product(product_id: int):
    ok = db.deactivate_product(product_id)
    if not ok:
        raise HTTPException(404, "Product not found")
    return {"message": f"Product {product_id} deactivated"}


@router.put("/products/{product_id}/steps")
def update_product_steps(product_id: int, steps: list[StepDurationUpdate]):
    """Update step durations for a product."""
    p = db.get_product_by_id(product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    for s in steps:
        db.upsert_product_step_duration(
            product_id=product_id,
            category_step_id=s.category_step_id,
            duration_minutes=s.duration_minutes,
        )
    return {"message": f"Step durations updated for product {product_id}"}


# ══════════════════════════════════════════════════════════════════════════════
# ORDERS (T_Order + T_OrderLine)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/orders")
def list_orders(status: Optional[str] = Query(None)):
    return db.get_orders(status=status)


@router.post("/orders", status_code=201)
def create_order(body: OrderCreate):
    order_id = db.create_order(
        order_number=body.order_number,
        customer_name=body.customer_name,
        order_date=body.order_date,
        required_date=body.required_date,
        notes=body.notes,
    )
    return {"message": f"Order '{body.order_number}' created", "id": order_id}


@router.get("/orders/{order_id}/lines")
def list_order_lines(order_id: int):
    return db.get_order_lines(order_id=order_id)


@router.post("/orders/{order_id}/lines", status_code=201)
def add_order_line(order_id: int, body: OrderLineCreate):
    line_id = db.add_order_line(
        order_id=order_id,
        product_id=body.product_id,
        quantity=body.quantity,
        deadline=body.deadline,
        priority=body.priority,
        notes=body.notes,
    )
    return {"message": "Order line added", "id": line_id}


@router.get("/order-lines")
def list_all_order_lines(status: Optional[str] = Query("open")):
    return db.get_order_lines(status=status)


# ══════════════════════════════════════════════════════════════════════════════
# CSV IMPORT (updated for new schema)
# ══════════════════════════════════════════════════════════════════════════════
# CSV format: name, category, allergen, priority, notes
# allergen: A/B/C/D/E/F or empty
# category: must match M_Category.name exactly

@router.post("/products/import/preview")
async def preview_csv(file: UploadFile = File(...)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "File must be a .csv")
    content = await file.read()
    rows    = _parse_csv(content.decode("utf-8-sig"))
    categories = {c["name"]: c["id"] for c in db.get_categories()}
    allergens  = {a["code"] for a in db.get_allergens()}
    preview_rows = []
    for i, row in enumerate(rows, 1):
        errors = _validate_row(row, categories, allergens)
        preview_rows.append({
            "row_number": i,
            "name":       row.get("name", ""),
            "category":   row.get("category", ""),
            "allergen":   row.get("allergen") or None,
            "priority":   _safe_int(row.get("priority"), 5),
            "valid":      len(errors) == 0,
            "errors":     errors,
        })
    valid   = sum(1 for r in preview_rows if r["valid"])
    invalid = len(preview_rows) - valid
    return {
        "total_rows":  len(preview_rows),
        "valid_rows":  valid,
        "invalid_rows": invalid,
        "rows":        preview_rows,
    }


@router.post("/products/import/commit")
async def commit_csv(
    file: UploadFile = File(...),
    skip_invalid: bool = Query(True),
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "File must be a .csv")
    content = await file.read()
    rows    = _parse_csv(content.decode("utf-8-sig"))
    categories = {c["name"]: c["id"] for c in db.get_categories()}
    allergens  = {a["code"] for a in db.get_allergens()}
    db_rows, skip_errors = [], []
    for i, row in enumerate(rows, 1):
        errs = _validate_row(row, categories, allergens)
        if errs:
            if not skip_invalid:
                raise HTTPException(400, f"Row {i}: {errs}")
            skip_errors.append({"row": i, "reason": "; ".join(errs)})
            continue
        db_rows.append({
            "name":     row.get("name","").strip(),
            "category": row.get("category","").strip(),
            "allergen": (row.get("allergen") or "").strip().upper() or None,
            "priority": _safe_int(row.get("priority"), 5),
            "notes":    row.get("notes","").strip(),
        })
    result = db.bulk_upsert_products(db_rows)
    result["errors"] = skip_errors + result.get("errors", [])
    return result


# ══════════════════════════════════════════════════════════════════════════════
# CALENDAR
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/calendar/working-days")
def get_working_days(
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date:   str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    rows = db.call_get_working_days(start_date, end_date)
    return [{"work_date":    db._fmt_date(r["work_date"]),
             "day_name":     r["day_name"],
             "is_holiday":   bool(r["is_holiday"]),
             "holiday_name": r.get("holiday_name", "")} for r in rows]


@router.get("/calendar/machine-hours/{work_date}")
def get_machine_hours(work_date: str):
    return db.call_get_machine_hours(work_date)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [{k.strip().lower(): (v.strip() if v else "")
             for k, v in row.items()} for row in reader]


def _validate_row(row: dict, categories: dict, allergens: set) -> list[str]:
    errors = []
    if not row.get("name","").strip():
        errors.append("name is required")
    cat = row.get("category","").strip()
    if not cat:
        errors.append("category is required")
    elif cat not in categories:
        errors.append(f"Unknown category '{cat}'. Valid: {list(categories.keys())}")
    al = (row.get("allergen") or "").strip().upper()
    if al and al not in allergens:
        errors.append(f"Unknown allergen '{al}'. Valid: {sorted(allergens)}")
    pri = _safe_int(row.get("priority","5"))
    if pri is None or not (1 <= pri <= 10):
        errors.append("priority must be 1–10")
    return errors


def _safe_int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default