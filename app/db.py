"""
db.py — SQL Server database layer for ScheduleAI
New schema: M_ (Master), T_ (Transaction), no-prefix (Operational)
"""
from __future__ import annotations
import json
from contextlib import contextmanager
from datetime import date, time, datetime
from typing import Optional, Generator

from sqlalchemy import create_engine, text
from decouple import config

USER          = config("USER")
PASSWORD      = config("PASSWORD")
SERVER        = config("SERVER")
DATABASE_NAME = config("DATABASE_NAME")
DRIVER        = config("DRIVER", default="ODBC Driver 17 for SQL Server")

DATABASE_URL = (
    f"mssql+pyodbc://{USER}:{PASSWORD}@{SERVER}/{DATABASE_NAME}"
    f"?driver={DRIVER.replace(' ', '+')}"
)

engine = create_engine(DATABASE_URL, echo=False)

DB_SERVER   = SERVER
DB_NAME     = DATABASE_NAME
DB_DRIVER   = DRIVER
DB_USER     = USER
DB_PASSWORD = PASSWORD
CONNECTION_STRING = DATABASE_URL.replace(PASSWORD, "***")


@contextmanager
def get_conn() -> Generator:
    with engine.connect() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _rows_to_list(result) -> list:
    keys = list(result.keys())
    return [dict(zip(keys, row)) for row in result.fetchall()]


def _fmt_time(t) -> Optional[str]:
    if t is None: return None
    if isinstance(t, time): return t.strftime("%H:%M")
    s = str(t)
    return s[:5] if len(s) >= 5 else s


def _fmt_date(d) -> Optional[str]:
    if d is None: return None
    if isinstance(d, (date, datetime)): return d.strftime("%Y-%m-%d")
    s = str(d)
    return s[:10] if len(s) >= 10 else s


# ── M_Allergen ────────────────────────────────────────────────────────────────

def get_allergens() -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT id, code, name, sort_order, colour_hex, active "
            "FROM M_Allergen WHERE active=1 ORDER BY sort_order"
        ))
        return _rows_to_list(result)


def get_allergen_order() -> list:
    return [r["code"] for r in get_allergens()]


# ── M_MachineType ────────────────────────────────────────────────────────────

def get_machine_types() -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT id, name, description, colour_hex, active "
            "FROM M_MachineType WHERE active=1 ORDER BY name"
        ))
        return _rows_to_list(result)


# ── M_Category + M_CategoryStep ──────────────────────────────────────────────

def get_categories() -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT id, name, description, active "
            "FROM M_Category WHERE active=1 ORDER BY name"
        ))
        return _rows_to_list(result)


def get_category_steps(category_id: Optional[int] = None) -> list:
    with get_conn() as conn:
        where = "WHERE cs.category_id=:cid" if category_id else ""
        params = {"cid": category_id} if category_id else {}
        result = conn.execute(text(
            "SELECT cs.id, cs.category_id, c.name AS category_name, "
            "cs.step_number, cs.step_name, cs.machine_type_id, "
            "mt.name AS machine_type_name, cs.allergen_flag, "
            "cs.cooling_time_min, cs.same_machine_as_step, cs.notes "
            "FROM M_CategoryStep cs "
            "JOIN M_Category c ON c.id=cs.category_id "
            "JOIN M_MachineType mt ON mt.id=cs.machine_type_id "
            f"{where} ORDER BY cs.category_id, cs.step_number"
        ), params)
        return _rows_to_list(result)


# ── M_Product ─────────────────────────────────────────────────────────────────

def get_products(active_only: bool = True,
                 allergen_code: Optional[str] = None,
                 category_id: Optional[int] = None,
                 deadline_from: Optional[str] = None,
                 deadline_to: Optional[str] = None,
                 planned_status: Optional[str] = None) -> list:
    with get_conn() as conn:
        conditions = ["1=1"]
        params: dict = {}
        if active_only:
            conditions.append("p.active=1")
        if allergen_code:
            conditions.append("a.code=:al"); params["al"] = allergen_code
        if category_id:
            conditions.append("p.category_id=:cid"); params["cid"] = category_id
        if planned_status == "planned":
            conditions.append(
                "EXISTS (SELECT 1 FROM T_PlanAssignment pa "
                "JOIN T_Plan pl ON pl.id=pa.plan_id "
                "WHERE pa.product_id=p.id "
                "AND pl.status IN ('solved','published'))"
            )
        elif planned_status == "unplanned":
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM T_PlanAssignment pa "
                "JOIN T_Plan pl ON pl.id=pa.plan_id "
                "WHERE pa.product_id=p.id "
                "AND pl.status IN ('solved','published'))"
            )
        where = " AND ".join(conditions)
        result = conn.execute(text(
            "SELECT p.id, p.name, p.category_id, c.name AS category_name, "
            "p.allergen_id, a.code AS allergen_code, a.name AS allergen_name, "
            "a.colour_hex AS allergen_colour, "
            "p.priority, p.notes, p.active, p.position, p.created_at, p.updated_at "
            "FROM M_Product p "
            "JOIN M_Category c ON c.id=p.category_id "
            "LEFT JOIN M_Allergen a ON a.id=p.allergen_id "
            f"WHERE {where} "
            "ORDER BY c.name, a.sort_order, p.priority, p.name"
        ), params)
        rows = _rows_to_list(result)
    for r in rows:
        r["created_at"] = _fmt_date(r["created_at"])
        r["updated_at"] = _fmt_date(r["updated_at"])
    return rows


def get_product_by_id(product_id: int) -> Optional[dict]:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT p.id, p.name, p.category_id, c.name AS category_name, "
            "p.allergen_id, a.code AS allergen_code, "
            "p.priority, p.notes, p.active "
            "FROM M_Product p "
            "JOIN M_Category c ON c.id=p.category_id "
            "LEFT JOIN M_Allergen a ON a.id=p.allergen_id "
            "WHERE p.id=:pid"
        ), {"pid": product_id})
        rows = _rows_to_list(result)
    return rows[0] if rows else None


def insert_product(data: dict) -> int:
    with get_conn() as conn:
        result = conn.execute(text(
            "INSERT INTO M_Product "
            "(name, category_id, allergen_id, priority, notes, position) "
            "OUTPUT INSERTED.id "
            "VALUES (:name, :cat, :al, :pri, :notes, :pos)"
        ), {"name": data["name"], "cat": data.get("category_id"),
            "al": data.get("allergen_id"), "pri": data.get("priority", 5),
            "notes": data.get("notes", ""), "pos": data.get("position")})
        return result.fetchone()[0]


def update_product(product_id: int, data: dict) -> bool:
    with get_conn() as conn:
        result = conn.execute(text(
            "UPDATE M_Product SET name=:name, category_id=:cat, "
            "allergen_id=:al, priority=:pri, notes=:notes, position=:pos "
            "WHERE id=:pid"
        ), {"name": data.get("name"), "cat": data.get("category_id"),
            "al": data.get("allergen_id"), "pri": data.get("priority", 5),
            "notes": data.get("notes", ""), "pos": data.get("position"),
            "pid": product_id})
        return result.rowcount > 0


def deactivate_product(product_id: int) -> bool:
    with get_conn() as conn:
        result = conn.execute(
            text("UPDATE M_Product SET active=0 WHERE id=:pid"), {"pid": product_id})
        return result.rowcount > 0


def get_product_step_durations(product_id: int) -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT psd.id, psd.product_id, psd.category_step_id, "
            "psd.duration_minutes, cs.step_number, cs.step_name, "
            "mt.name AS machine_type_name, cs.allergen_flag, cs.cooling_time_min "
            "FROM M_ProductStepDuration psd "
            "JOIN M_CategoryStep cs ON cs.id=psd.category_step_id "
            "JOIN M_MachineType mt ON mt.id=cs.machine_type_id "
            "WHERE psd.product_id=:pid ORDER BY cs.step_number"
        ), {"pid": product_id})
        return _rows_to_list(result)


def upsert_product_step_duration(product_id: int,
                                  category_step_id: int,
                                  duration_minutes: int) -> None:
    with get_conn() as conn:
        conn.execute(text(
            "MERGE M_ProductStepDuration AS t "
            "USING (SELECT :pid AS product_id, :sid AS category_step_id) AS s "
            "ON t.product_id=s.product_id AND t.category_step_id=s.category_step_id "
            "WHEN MATCHED THEN UPDATE SET duration_minutes=:dur "
            "WHEN NOT MATCHED THEN INSERT (product_id, category_step_id, duration_minutes) "
            "VALUES (:pid, :sid, :dur);"
        ), {"pid": product_id, "sid": category_step_id, "dur": duration_minutes})


def get_products_with_steps(active_only: bool = True,
                             deadline_from: Optional[str] = None,
                             deadline_to: Optional[str] = None) -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT p.id, p.name, p.allergen_id, a.code AS allergen, "
            "p.priority, p.category_id, c.name AS category_name, "
            "p.notes, p.position "
            "FROM M_Product p "
            "JOIN M_Category c ON c.id=p.category_id "
            "LEFT JOIN M_Allergen a ON a.id=p.allergen_id "
            "WHERE p.active=1 ORDER BY p.priority, p.name"
        ))
        products = _rows_to_list(result)
        if not products:
            return []
        id_list = ",".join(str(p["id"]) for p in products)
        step_result = conn.execute(text(
            "SELECT psd.product_id, psd.category_step_id, psd.duration_minutes, "
            "cs.step_number, cs.step_name, cs.machine_type_id, "
            "mt.name AS machine_type_name, cs.allergen_flag AS allergen_applies, "
            "cs.cooling_time_min AS gap_after_min, cs.same_machine_as_step "
            "FROM M_ProductStepDuration psd "
            "JOIN M_CategoryStep cs ON cs.id=psd.category_step_id "
            "JOIN M_MachineType mt ON mt.id=cs.machine_type_id "
            f"WHERE psd.product_id IN ({id_list}) "
            "ORDER BY psd.product_id, cs.step_number"
        ))
        all_steps = _rows_to_list(step_result)
    steps_by_product: dict = {}
    for s in all_steps:
        steps_by_product.setdefault(s["product_id"], []).append(s)
    for p in products:
        p["steps"] = steps_by_product.get(p["id"], [])
    return products


def get_order_lines_with_steps(status: str = "open") -> list:
    """Return open T_OrderLine rows with step info — each = one production batch."""
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT ol.id AS order_line_id, ol.order_id, o.order_number, "
            "o.customer_name, ol.product_id, p.name AS product_name, "
            "a.code AS allergen, p.position, "
            "COALESCE(ol.priority, p.priority) AS priority, "
            "COALESCE(ol.deadline, o.required_date) AS deadline, "
            "ol.quantity, ol.status AS line_status "
            "FROM T_OrderLine ol "
            "JOIN T_Order o ON o.id=ol.order_id "
            "JOIN M_Product p ON p.id=ol.product_id "
            "LEFT JOIN M_Allergen a ON a.id=p.allergen_id "
            "WHERE ol.status=:status AND p.active=1 "
            "AND o.status != 'cancelled' "
            "ORDER BY COALESCE(ol.priority, p.priority), "
            "COALESCE(ol.deadline, o.required_date), p.name"
        ), {"status": status})
        order_lines = _rows_to_list(result)
        if not order_lines:
            return []
        product_ids = list({ol["product_id"] for ol in order_lines})
        id_list = ",".join(str(i) for i in product_ids)
        step_result = conn.execute(text(
            "SELECT psd.product_id, psd.category_step_id, psd.duration_minutes, "
            "cs.step_number, cs.step_name, cs.machine_type_id, "
            "mt.name AS machine_type_name, cs.allergen_flag AS allergen_applies, "
            "cs.cooling_time_min AS gap_after_min, cs.same_machine_as_step "
            "FROM M_ProductStepDuration psd "
            "JOIN M_CategoryStep cs ON cs.id=psd.category_step_id "
            "JOIN M_MachineType mt ON mt.id=cs.machine_type_id "
            f"WHERE psd.product_id IN ({id_list}) "
            "ORDER BY psd.product_id, cs.step_number"
        ))
        all_steps = _rows_to_list(step_result)
    steps_by_product: dict = {}
    for s in all_steps:
        steps_by_product.setdefault(s["product_id"], []).append(s)
    for ol in order_lines:
        ol["deadline"] = _fmt_date(ol["deadline"])
        ol["steps"] = steps_by_product.get(ol["product_id"], [])
    return order_lines


# ── Machines ──────────────────────────────────────────────────────────────────

def get_machines(active_only: bool = True) -> list:
    with get_conn() as conn:
        where = "WHERE m.active=1" if active_only else ""
        result = conn.execute(text(
            "SELECT m.id, m.name, m.machine_type_id, "
            "mt.name AS machine_type_name, "
            "m.default_shift_start, m.default_shift_end, m.active, m.notes "
            "FROM Machines m "
            "JOIN M_MachineType mt ON mt.id=m.machine_type_id "
            f"{where} ORDER BY mt.name, m.name"
        ))
        rows = _rows_to_list(result)
    for r in rows:
        r["default_shift_start"] = _fmt_time(r["default_shift_start"])
        r["default_shift_end"]   = _fmt_time(r["default_shift_end"])
    return rows


def get_machines_by_type() -> dict:
    machines = get_machines(active_only=True)
    by_type: dict = {}
    for m in machines:
        by_type.setdefault(m["machine_type_name"], []).append(m)
    return by_type


def create_machine(name: str, machine_type_id: int,
                    shift_start: str = "07:20",
                    shift_end: str = "22:00", notes: str = "") -> int:
    with get_conn() as conn:
        result = conn.execute(text(
            "INSERT INTO Machines (name, machine_type_id, default_shift_start, "
            "default_shift_end, notes) OUTPUT INSERTED.id "
            "VALUES (:name, :mt, :ss, :se, :notes)"
        ), {"name": name, "mt": machine_type_id,
            "ss": shift_start, "se": shift_end, "notes": notes})
        return result.fetchone()[0]


def update_machine(machine_id: int, data: dict) -> bool:
    with get_conn() as conn:
        result = conn.execute(text(
            "UPDATE Machines SET name=:name, "
            "default_shift_start=:ss, default_shift_end=:se, "
            "notes=:notes WHERE id=:id"
        ), {"name": data.get("name"),
            "ss": data.get("default_shift_start", "07:20"),
            "se": data.get("default_shift_end", "22:00"),
            "notes": data.get("notes", ""), "id": machine_id})
        return result.rowcount > 0


def set_machine_active(machine_id: int, active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            text("UPDATE Machines SET active=:a WHERE id=:id"),
            {"a": int(active), "id": machine_id})


def delete_machine(machine_id: int) -> int:
    with get_conn() as conn:
        result = conn.execute(
            text("DELETE FROM Machines WHERE id=:id"), {"id": machine_id})
        return result.rowcount


# ── MachineShiftOverride ──────────────────────────────────────────────────────

def get_shift_overrides(machine_id: Optional[int] = None,
                         from_date: Optional[str] = None,
                         to_date: Optional[str] = None) -> list:
    with get_conn() as conn:
        conditions = ["1=1"]
        params: dict = {}
        if machine_id:
            conditions.append("o.machine_id=:mid"); params["mid"] = machine_id
        if from_date:
            conditions.append("o.override_date>=:fd"); params["fd"] = from_date
        if to_date:
            conditions.append("o.override_date<=:td"); params["td"] = to_date
        where = " AND ".join(conditions)
        result = conn.execute(text(
            "SELECT o.id, o.machine_id, m.name AS machine_name, "
            "o.override_date, o.shift_start, o.shift_end, o.closed, o.reason "
            "FROM MachineShiftOverride o "
            "JOIN Machines m ON m.id=o.machine_id "
            f"WHERE {where} ORDER BY o.override_date, m.name"
        ), params)
        rows = _rows_to_list(result)
    for r in rows:
        r["override_date"] = _fmt_date(r["override_date"])
        r["shift_start"]   = _fmt_time(r["shift_start"])
        r["shift_end"]     = _fmt_time(r["shift_end"])
    return rows


def upsert_shift_override(machine_id: int, override_date: str,
                           shift_start: Optional[str], shift_end: Optional[str],
                           closed: bool = False, reason: str = "") -> None:
    with get_conn() as conn:
        conn.execute(text(
            "MERGE MachineShiftOverride AS t "
            "USING (SELECT :mid AS machine_id, :dt AS override_date) AS s "
            "ON t.machine_id=s.machine_id AND t.override_date=s.override_date "
            "WHEN MATCHED THEN UPDATE SET "
            "  shift_start=:ss, shift_end=:se, closed=:cl, reason=:rsn "
            "WHEN NOT MATCHED THEN INSERT "
            "  (machine_id, override_date, shift_start, shift_end, closed, reason) "
            "VALUES (:mid, :dt, :ss, :se, :cl, :rsn);"
        ), {"mid": machine_id, "dt": override_date,
            "ss": shift_start, "se": shift_end,
            "cl": int(closed), "rsn": reason})


def delete_shift_override(machine_id: int, override_date: str) -> int:
    with get_conn() as conn:
        result = conn.execute(text(
            "DELETE FROM MachineShiftOverride "
            "WHERE machine_id=:mid AND override_date=:dt"
        ), {"mid": machine_id, "dt": override_date})
        return result.rowcount


# ── Holidays ──────────────────────────────────────────────────────────────────

def get_holidays(from_date: Optional[str] = None,
                  to_date: Optional[str] = None) -> list:
    with get_conn() as conn:
        conditions = ["1=1"]
        params: dict = {}
        if from_date:
            conditions.append("holiday_date>=:fd"); params["fd"] = from_date
        if to_date:
            conditions.append("holiday_date<=:td"); params["td"] = to_date
        where = " AND ".join(conditions)
        result = conn.execute(text(
            f"SELECT id, holiday_date, name, holiday_type, notes "
            f"FROM Holidays WHERE {where} ORDER BY holiday_date"
        ), params)
        rows = _rows_to_list(result)
    for r in rows:
        r["holiday_date"] = _fmt_date(r["holiday_date"])
    return rows


def upsert_holiday(holiday_date: str, name: str,
                    holiday_type: str = "public", notes: str = "") -> None:
    with get_conn() as conn:
        conn.execute(text(
            "MERGE Holidays AS t "
            "USING (SELECT :hd AS holiday_date) AS s "
            "ON t.holiday_date=s.holiday_date "
            "WHEN MATCHED THEN UPDATE SET name=:name, holiday_type=:ht, notes=:notes "
            "WHEN NOT MATCHED THEN INSERT (holiday_date, name, holiday_type, notes) "
            "VALUES (:hd, :name, :ht, :notes);"
        ), {"hd": holiday_date, "name": name, "ht": holiday_type, "notes": notes})


def delete_holiday(holiday_date: str) -> int:
    with get_conn() as conn:
        result = conn.execute(
            text("DELETE FROM Holidays WHERE holiday_date=:hd"), {"hd": holiday_date})
        return result.rowcount


# ── Stored procedures ─────────────────────────────────────────────────────────

def call_get_working_days(start_date: str, end_date: str) -> list:
    with get_conn() as conn:
        result = conn.execute(
            text("EXEC sp_get_working_days :sd, :ed"),
            {"sd": start_date, "ed": end_date})
        return _rows_to_list(result)


def call_get_machine_hours(work_date: str) -> list:
    with get_conn() as conn:
        result = conn.execute(
            text("EXEC sp_get_machine_hours :dt"), {"dt": work_date})
        rows = _rows_to_list(result)
    for r in rows:
        r["shift_start"] = _fmt_time(r["shift_start"])
        r["shift_end"]   = _fmt_time(r["shift_end"])
    return rows


# ── T_Order + T_OrderLine ─────────────────────────────────────────────────────

def get_orders(status: Optional[str] = None) -> list:
    with get_conn() as conn:
        where = "WHERE o.status=:status" if status else ""
        params = {"status": status} if status else {}
        result = conn.execute(text(
            "SELECT o.id, o.order_number, o.customer_name, o.order_date, "
            "o.required_date, o.status, o.notes, COUNT(ol.id) AS line_count "
            "FROM T_Order o LEFT JOIN T_OrderLine ol ON ol.order_id=o.id "
            f"{where} GROUP BY o.id, o.order_number, o.customer_name, "
            "o.order_date, o.required_date, o.status, o.notes "
            "ORDER BY o.required_date DESC, o.order_number"
        ), params)
        rows = _rows_to_list(result)
    for r in rows:
        r["order_date"]    = _fmt_date(r["order_date"])
        r["required_date"] = _fmt_date(r["required_date"])
    return rows


def get_order_lines(order_id: Optional[int] = None,
                     status: Optional[str] = None) -> list:
    with get_conn() as conn:
        conditions = ["1=1"]
        params: dict = {}
        if order_id:
            conditions.append("ol.order_id=:oid"); params["oid"] = order_id
        if status:
            conditions.append("ol.status=:status"); params["status"] = status
        where = " AND ".join(conditions)
        result = conn.execute(text(
            "SELECT ol.id, ol.order_id, o.order_number, o.customer_name, "
            "ol.product_id, p.name AS product_name, a.code AS allergen, "
            "ol.quantity, ol.deadline, COALESCE(ol.priority, p.priority) AS priority, "
            "ol.status, ol.notes "
            "FROM T_OrderLine ol "
            "JOIN T_Order o ON o.id=ol.order_id "
            "JOIN M_Product p ON p.id=ol.product_id "
            "LEFT JOIN M_Allergen a ON a.id=p.allergen_id "
            f"WHERE {where} ORDER BY o.order_number, p.name"
        ), params)
        rows = _rows_to_list(result)
    for r in rows:
        r["deadline"] = _fmt_date(r["deadline"])
    return rows


def create_order(order_number: str, customer_name: str,
                  order_date: str, required_date: str, notes: str = "") -> int:
    with get_conn() as conn:
        result = conn.execute(text(
            "INSERT INTO T_Order (order_number, customer_name, order_date, "
            "required_date, notes) OUTPUT INSERTED.id "
            "VALUES (:no, :cust, :od, :rd, :notes)"
        ), {"no": order_number, "cust": customer_name,
            "od": order_date, "rd": required_date, "notes": notes})
        return result.fetchone()[0]


def add_order_line(order_id: int, product_id: int, quantity: int,
                    deadline: Optional[str] = None,
                    priority: Optional[int] = None, notes: str = "") -> int:
    with get_conn() as conn:
        result = conn.execute(text(
            "INSERT INTO T_OrderLine (order_id, product_id, quantity, "
            "deadline, priority, notes) OUTPUT INSERTED.id "
            "VALUES (:oid, :pid, :qty, :dl, :pri, :notes)"
        ), {"oid": order_id, "pid": product_id, "qty": quantity,
            "dl": deadline, "pri": priority, "notes": notes})
        return result.fetchone()[0]


def update_order_line_status(order_line_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            text("UPDATE T_OrderLine SET status=:s WHERE id=:id"),
            {"s": status, "id": order_line_id})


# ── T_Plan ────────────────────────────────────────────────────────────────────

def create_plan(start_date: str, end_date: str,
                allergen_order: str = "A,B,C,D,E,F",
                name: str = "Plan", notes: str = "") -> int:
    with get_conn() as conn:
        result = conn.execute(text(
            "INSERT INTO T_Plan (name, start_date, end_date, allergen_order, notes) "
            "OUTPUT INSERTED.id VALUES (:name, :sd, :ed, :ao, :notes)"
        ), {"name": name, "sd": start_date, "ed": end_date,
            "ao": allergen_order, "notes": notes})
        return result.fetchone()[0]


def get_plans(status: Optional[str] = None) -> list:
    with get_conn() as conn:
        where = "WHERE status=:status" if status else ""
        params = {"status": status} if status else {}
        result = conn.execute(text(
            "SELECT id, name, start_date, end_date, allergen_order, "
            "status, solve_time_ms, jobs_scheduled, jobs_total, notes, created_at "
            f"FROM T_Plan {where} ORDER BY created_at DESC"
        ), params)
        rows = _rows_to_list(result)
    for r in rows:
        r["start_date"] = _fmt_date(r["start_date"])
        r["end_date"]   = _fmt_date(r["end_date"])
        r["created_at"] = _fmt_date(r["created_at"])
    return rows


def update_plan_status(plan_id: int, status: str,
                        solve_time_ms: Optional[int] = None,
                        jobs_scheduled: Optional[int] = None,
                        jobs_total: Optional[int] = None) -> None:
    with get_conn() as conn:
        conn.execute(text(
            "UPDATE T_Plan SET status=:status, "
            "solve_time_ms=COALESCE(:ms, solve_time_ms), "
            "jobs_scheduled=COALESCE(:js, jobs_scheduled), "
            "jobs_total=COALESCE(:jt, jobs_total) "
            "WHERE id=:pid"
        ), {"status": status, "ms": solve_time_ms,
            "js": jobs_scheduled, "jt": jobs_total, "pid": plan_id})


# ── T_PlanAssignment ──────────────────────────────────────────────────────────

def save_multi_step_assignments(plan_id: int, assignments: list) -> None:
    with get_conn() as conn:
        # Hard-locked rows: preserved exactly — not deleted, not overwritten
        # Soft-locked rows: deleted and replaced with new solver output,
        #   but re-marked as soft-locked so next replan respects them too
        hard_locked = conn.execute(text(
            "SELECT product_id, order_line_id, category_step_id "
            "FROM T_PlanAssignment "
            "WHERE plan_id=:pid AND locked=1 "
            "  AND lock_scope != 'soft'"
        ), {"pid": plan_id}).fetchall()
        hard_keys = {(r[0], r[1], r[2]) for r in hard_locked}

        # Soft-locked product keys — new rows will be re-marked as soft
        soft_locked = conn.execute(text(
            "SELECT DISTINCT product_id, order_line_id "
            "FROM T_PlanAssignment "
            "WHERE plan_id=:pid AND locked=1 AND lock_scope='soft'"
        ), {"pid": plan_id}).fetchall()
        soft_products = {(r[0], r[1]) for r in soft_locked}

        # Delete unlocked rows and soft-locked rows (will be re-inserted)
        conn.execute(text(
            "DELETE FROM T_PlanAssignment "
            "WHERE plan_id=:pid "
            "  AND (locked=0 OR lock_scope='soft')"
        ), {"pid": plan_id})

        for a in assignments:
            key = (a["product_id"], a.get("order_line_id"),
                   a.get("category_step_id"))
            if key in hard_keys:
                continue  # hard-locked row already in DB, skip

            # Check if this product was soft-locked — persist the lock
            prod_key = (a["product_id"], a.get("order_line_id"))
            is_soft = prod_key in soft_products

            conn.execute(text(
                "INSERT INTO T_PlanAssignment "
                "(plan_id, order_line_id, product_id, category_step_id, "
                "machine_id, scheduled_date, start_time, end_time, "
                "duration_minutes, allergen_code, locked, lock_scope, version) "
                "VALUES (:pid, :ol, :prod, :step, :mach, "
                ":sd, :st, :et, :dur, :al, :lk, :scope, 1)"
            ), {
                "pid":   plan_id,
                "ol":    a.get("order_line_id"),
                "prod":  a["product_id"],
                "step":  a.get("category_step_id"),
                "mach":  a["machine_id"],
                "sd":    a["scheduled_date"],
                "st":    a["start_time"],
                "et":    a["end_time"],
                "dur":   a["duration_minutes"],
                "al":    a.get("allergen_code") or a.get("allergen"),
                "lk":    1 if is_soft else 0,
                "scope": "soft" if is_soft else None,
            })


def get_multi_step_assignments(plan_id: int) -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT pa.id, pa.plan_id, pa.order_line_id, "
            "pa.product_id, p.name AS product_name, "
            "pa.category_step_id, cs.step_number, cs.step_name, "
            "pa.machine_id, m.name AS machine_name, "
            "mt.name AS machine_type_name, "
            "pa.scheduled_date, pa.start_time, pa.end_time, "
            "pa.duration_minutes, pa.allergen_code AS allergen, "
            "pa.locked, pa.lock_reason, pa.lock_scope, pa.version, "
            "p.position AS product_position, "
            "ol.order_id, o.order_number "
            "FROM T_PlanAssignment pa "
            "JOIN M_Product p ON p.id=pa.product_id "
            "JOIN Machines m ON m.id=pa.machine_id "
            "LEFT JOIN M_CategoryStep cs ON cs.id=pa.category_step_id "
            "LEFT JOIN M_MachineType mt ON mt.id=cs.machine_type_id "
            "LEFT JOIN T_OrderLine ol ON ol.id=pa.order_line_id "
            "LEFT JOIN T_Order o ON o.id=ol.order_id "
            "WHERE pa.plan_id=:pid "
            "ORDER BY pa.scheduled_date, pa.product_id, cs.step_number"
        ), {"pid": plan_id})
        rows = _rows_to_list(result)
    for r in rows:
        r["scheduled_date"] = _fmt_date(r["scheduled_date"])
        r["start_time"]     = _fmt_time(r["start_time"])
        r["end_time"]       = _fmt_time(r["end_time"])
        r["allergen"]       = r["allergen"] or None
        r["lock_reason"]    = r["lock_reason"] or None
    return rows


def get_locked_assignments(plan_id: int) -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT product_id, order_line_id, category_step_id "
            "FROM T_PlanAssignment WHERE plan_id=:pid AND locked=1"
        ), {"pid": plan_id})
        return _rows_to_list(result)


def soft_lock_product(plan_id: int, product_id: int,
                      locked: bool, reason: str = "") -> int:
    """Soft lock: product stays on same day but solver picks best time/machine."""
    with get_conn() as conn:
        result = conn.execute(text(
            "UPDATE T_PlanAssignment "
            "SET locked=:lk, lock_scope=:scope, lock_reason=:rsn "
            "WHERE plan_id=:pid AND product_id=:prod"
        ), {
            "lk":    int(locked),
            "scope": "soft" if locked else None,
            "rsn":   reason if locked else None,
            "pid":   plan_id,
            "prod":  product_id,
        })
        return result.rowcount


def lock_step(plan_id: int, assignment_id: int,
              locked: bool, reason: str = "") -> int:
    """Lock/unlock a single T_PlanAssignment row by its id."""
    with get_conn() as conn:
        result = conn.execute(text(
            "UPDATE T_PlanAssignment "
            "SET locked=:lk, lock_scope=:scope, lock_reason=:rsn "
            "WHERE id=:aid AND plan_id=:pid"
        ), {
            "lk":    int(locked),
            "scope": "step" if locked else None,
            "rsn":   reason if locked else None,
            "aid":   assignment_id,
            "pid":   plan_id,
        })
        return result.rowcount


def lock_product(plan_id: int, product_id: int,
                 locked: bool, reason: str = "") -> int:
    """Lock/unlock all steps of a product in a plan."""
    with get_conn() as conn:
        result = conn.execute(text(
            "UPDATE T_PlanAssignment "
            "SET locked=:lk, lock_scope=:scope, lock_reason=:rsn "
            "WHERE plan_id=:pid AND product_id=:prod"
        ), {
            "lk":    int(locked),
            "scope": "product" if locked else None,
            "rsn":   reason if locked else None,
            "pid":   plan_id,
            "prod":  product_id,
        })
        return result.rowcount


def lock_date_range(plan_id: int, date_from: str, date_to: str,
                    locked: bool, reason: str = "") -> int:
    """Lock/unlock all assignments whose scheduled_date is in [date_from, date_to]."""
    with get_conn() as conn:
        result = conn.execute(text(
            "UPDATE T_PlanAssignment "
            "SET locked=:lk, lock_scope=:scope, lock_reason=:rsn "
            "WHERE plan_id=:pid "
            "  AND scheduled_date BETWEEN :df AND :dt"
        ), {
            "lk":    int(locked),
            "scope": "date" if locked else None,
            "rsn":   reason if locked else None,
            "pid":   plan_id,
            "df":    date_from,
            "dt":    date_to,
        })
        return result.rowcount


# Keep old name as alias for backwards compatibility
def toggle_lock(plan_id: int, product_id: int,
                locked: bool, reason: str = "") -> int:
    return lock_product(plan_id, product_id, locked=locked, reason=reason)


# ── T_PlanUnscheduled ─────────────────────────────────────────────────────────

def get_unscheduled(plan_id: int) -> list:
    with get_conn() as conn:
        result = conn.execute(text(
            "SELECT pu.id, pu.plan_id, pu.order_line_id, "
            "pu.product_id, p.name AS product_name, "
            "a.code AS allergen, p.priority, pu.reason, "
            "COALESCE(ol.deadline, o.required_date) AS deadline "
            "FROM T_PlanUnscheduled pu "
            "JOIN M_Product p ON p.id=pu.product_id "
            "LEFT JOIN M_Allergen a ON a.id=p.allergen_id "
            "LEFT JOIN T_OrderLine ol ON ol.id=pu.order_line_id "
            "LEFT JOIN T_Order o ON o.id=ol.order_id "
            "WHERE pu.plan_id=:pid ORDER BY p.priority, p.name"
        ), {"pid": plan_id})
        rows = _rows_to_list(result)
    for r in rows:
        r["deadline"] = _fmt_date(r["deadline"])
    return rows


def save_unscheduled(plan_id: int, unscheduled: list) -> None:
    with get_conn() as conn:
        conn.execute(text(
            "DELETE FROM T_PlanUnscheduled WHERE plan_id=:pid"), {"pid": plan_id})
        seen = set()
        for u in unscheduled:
            product_id = u.get("product_id")
            if not product_id and u.get("product_name"):
                row = conn.execute(text(
                    "SELECT id FROM M_Product WHERE name=:nm"),
                    {"nm": u["product_name"]}).fetchone()
                if row:
                    product_id = row[0]
            if not product_id:
                continue
            # Deduplicate — unique constraint is (plan_id, order_line_id, product_id)
            key = (plan_id, u.get("order_line_id"), product_id)
            if key in seen:
                continue
            seen.add(key)
            conn.execute(text(
                "INSERT INTO T_PlanUnscheduled "
                "(plan_id, order_line_id, product_id, reason) "
                "VALUES (:pid, :ol, :prod, :rsn)"
            ), {"pid": plan_id, "ol": u.get("order_line_id"),
                "prod": product_id, "rsn": u.get("reason", "Could not schedule")})


# ── CSV import ────────────────────────────────────────────────────────────────

def bulk_upsert_products(rows: list) -> dict:
    inserted = 0; updated = 0; errors = []
    categories = {c["name"]: c["id"] for c in get_categories()}
    allergens  = {a["code"]: a["id"] for a in get_allergens()}
    with get_conn() as conn:
        for i, row in enumerate(rows):
            try:
                name = row.get("name", "").strip()
                if not name:
                    errors.append({"row": i+2, "reason": "Missing name"}); continue
                cat_name = row.get("category", "").strip()
                cat_id   = categories.get(cat_name)
                if not cat_id:
                    errors.append({"row": i+2,
                        "reason": f"Unknown category '{cat_name}'. "
                                  f"Valid: {list(categories.keys())}"}); continue
                al_code = row.get("allergen", "").strip() or None
                al_id   = allergens.get(al_code) if al_code else None
                priority = int(row.get("priority", 5))
                notes    = row.get("notes", "").strip()
                existing = conn.execute(
                    text("SELECT id FROM M_Product WHERE name=:nm"),
                    {"nm": name}).fetchone()
                if existing:
                    conn.execute(text(
                        "UPDATE M_Product SET category_id=:cat, allergen_id=:al, "
                        "priority=:pri, notes=:notes WHERE id=:id"
                    ), {"cat": cat_id, "al": al_id, "pri": priority,
                        "notes": notes, "id": existing[0]})
                    updated += 1
                else:
                    conn.execute(text(
                        "INSERT INTO M_Product "
                        "(name, category_id, allergen_id, priority, notes) "
                        "VALUES (:name, :cat, :al, :pri, :notes)"
                    ), {"name": name, "cat": cat_id, "al": al_id,
                        "pri": priority, "notes": notes})
                    inserted += 1
            except Exception as e:
                errors.append({"row": i+2, "reason": str(e)})
    return {"inserted": inserted, "updated": updated, "errors": errors}