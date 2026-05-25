"""
Solve router
  POST /solve    — classify + solve  (writes debug/input_*.json + debug/output_*.csv)
  POST /classify — classify only
  POST /debug    — full traceback on error
  GET  /logs     — tail log files
  GET  /debug-files — list saved debug files
  GET  /debug-files/{filename} — download a specific debug file
"""
import csv
import json
import traceback
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, FileResponse

from app.logger          import get_logger, APP_LOG, ERROR_LOG
from app.models.request  import ScheduleRequest
from app.models.response import ScheduleResponse
from app.classifier      import classify
from app.solvers.dispatcher import dispatch

router = APIRouter()
log    = get_logger(__name__)

DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "debug"
DEBUG_DIR.mkdir(exist_ok=True)


# ── Debug file writers ────────────────────────────────────────────────────────

def _write_input_json(req: ScheduleRequest, slug: str) -> Path:
    """Save the raw request as pretty JSON."""
    path = DEBUG_DIR / f"input_{slug}.json"
    path.write_text(
        json.dumps(req.model_dump(), indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _write_output_csv(result: ScheduleResponse, req: ScheduleRequest, slug: str) -> Path:
    """
    Save the schedule result as a human-readable CSV.
    Sections:
      1. META        — status, category, subtype, solve time, KPIs
      2. SCHEDULE    — one row per assignment (job, machine, start, end, duration, allergen, position, depends_on)
      3. UNASSIGNED  — jobs that couldn't be scheduled
      4. VIOLATIONS  — constraint violations
      5. GANTT       — text gantt chart per machine
    """
    path = DEBUG_DIR / f"output_{slug}.csv"

    # Build a lookup for task metadata from the request
    task_meta: dict[str, dict] = {}
    for t in req.tasks:
        task_meta[t.id] = {
            "allergen": (t.attributes or {}).get("allergen") or getattr(t, "allergen", "") or "",
            "position": (t.attributes or {}).get("position") or "",
            "depends_on": ", ".join((t.attributes or {}).get("depends_on", [])),
            "machine":   getattr(t, "machine_id", "") or "",
            "duration":  t.duration_minutes,
        }

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        # ── Section 1: META ───────────────────────────────────────────────
        w.writerow(["=== META ==="])
        w.writerow(["Field", "Value"])
        w.writerow(["Status",       result.status])
        w.writerow(["Category",     result.category])
        w.writerow(["Subtype",      result.subtype])
        w.writerow(["Solve time",   f"{result.solve_time_ms}ms"])
        w.writerow(["Timestamp",    slug])
        for kpi in result.kpis:
            w.writerow([kpi.key, f"{kpi.value}{kpi.unit}"])
        w.writerow([])

        # ── Section 2: SCHEDULE grouped by machine ───────────────────────
        w.writerow(["=== SCHEDULE (grouped by machine) ==="])

        # Group assignments by machine, sorted by start time
        from collections import defaultdict as _dd
        by_machine = _dd(list)
        for a in result.assignments:
            by_machine[a.resource_name].append(a)
        for m in by_machine:
            by_machine[m].sort(key=lambda a: a.metadata.get("start_min", 0))

        SHIFT_START_MIN = 7 * 60 + 20  # 440
        SHIFT_END_MIN   = 22 * 60      # 1320

        for machine_name, machine_jobs in sorted(by_machine.items()):
            w.writerow([])
            w.writerow([f"── Machine: {machine_name} ──"])
            w.writerow([
                "Seq", "Job ID", "Job name",
                "Start", "End", "Duration (min)",
                "Allergen", "Position", "Depends on",
                "Prev completed at", "Gap from prev (min)", "Notes"
            ])
            prev_end_min = SHIFT_START_MIN
            for seq, a in enumerate(machine_jobs, 1):
                meta      = task_meta.get(a.task_id, {})
                allergen  = a.metadata.get("allergen") or meta.get("allergen", "")
                position  = a.metadata.get("position") or meta.get("position", "")
                depends   = ", ".join(a.metadata.get("depends_on", [])) or meta.get("depends_on", "")
                dur       = int(a.cost) if a.cost else meta.get("duration", "")
                s_min     = a.metadata.get("start_min", 0)
                e_min     = a.metadata.get("end_min", 0)
                gap_min   = s_min - prev_end_min
                prev_str  = f"{prev_end_min//60:02d}:{prev_end_min%60:02d}" if seq > 1 else "shift start (07:20)"
                notes = []
                if position == "first": notes.append("first on machine")
                if position == "last":  notes.append("last on machine")
                if depends:             notes.append(f"waits for: {depends}")
                if gap_min > 0:         notes.append(f"idle {gap_min}min before this job")
                day_lbl  = a.metadata.get("day_label", "")
                deadline_lbl = a.metadata.get("deadline", "—")
                start_time = a.start.split(" ")[-1] if " " in a.start else a.start
                end_time   = a.end.split(" ")[-1]   if " " in a.end   else a.end
                w.writerow([
                    seq, a.task_id, a.task_name.split("  [")[0].strip(),
                    day_lbl, start_time, end_time, dur,
                    allergen, position, deadline_lbl, depends,
                    prev_str, gap_min,
                    " | ".join(notes),
                ])
                prev_end_min = e_min

            # trailing idle time to end of shift
            trail = SHIFT_END_MIN - prev_end_min
            if trail > 0:
                w.writerow(["", "", f"[idle until shift end]",
                            f"{prev_end_min//60:02d}:{prev_end_min%60:02d}", "22:00",
                            trail, "", "", "", "", trail, ""])

        w.writerow([])

        # ── Section 3: UNASSIGNED ─────────────────────────────────────────
        if result.unassigned:
            w.writerow(["=== UNASSIGNED JOBS ==="])
            w.writerow(["Job ID", "Job name", "Reason"])
            for u in result.unassigned:
                w.writerow([u.task_id, u.task_name, u.reason])
            w.writerow([])

        # ── Section 4: VIOLATIONS ─────────────────────────────────────────
        if result.violations:
            w.writerow(["=== CONSTRAINT VIOLATIONS ==="])
            w.writerow(["Constraint", "Severity", "Details"])
            for v in result.violations:
                w.writerow([v.constraint, v.severity, v.details])
            w.writerow([])

        # ── Section 5: TEXT GANTT ─────────────────────────────────────────
        w.writerow(["=== GANTT (text) ==="])
        if result.gantt:
            # Find max end for scale
            max_end = max(
                (b.start_offset + b.duration)
                for row in result.gantt for b in row.bars
            ) or 480
            bar_width = 60   # chars wide

            w.writerow(["Machine", "Timeline (each char ≈ " + str(round(max_end / bar_width, 1)) + " min)"])
            for row in result.gantt:
                canvas = ["."] * bar_width
                labels = []
                for b in sorted(row.bars, key=lambda x: x.start_offset):
                    left  = int(b.start_offset / max_end * bar_width)
                    width = max(1, int(b.duration / max_end * bar_width))
                    right = min(left + width, bar_width)
                    short = b.task_name.split("  [")[0][:width].ljust(width)
                    for ci in range(left, right):
                        canvas[ci] = short[ci - left] if (ci - left) < len(short) else "█"
                    labels.append(f"{b.task_name.split('[')[0].strip()} ({int(b.start_offset)}-{int(b.start_offset+b.duration)}min)")
                w.writerow([row.resource_name, "".join(canvas)])
                for lbl in labels:
                    w.writerow(["", f"  → {lbl}"])
            w.writerow([])

        # ── Section 6: INPUT SUMMARY ──────────────────────────────────────
        w.writerow(["=== INPUT SUMMARY ==="])
        w.writerow(["Allergen order", ", ".join(req.allergen_order or [])])
        w.writerow([])
        attrs_g = req.attributes or {}
        w.writerow(["Planning start", attrs_g.get("start_date", "—")])
        w.writerow(["Planning end",   attrs_g.get("end_date",   "—")])
        w.writerow([])
        w.writerow(["Job ID", "Job name", "Machine", "Duration", "Allergen", "Position", "Deadline", "Depends on"])
        for t in req.tasks:
            meta = task_meta.get(t.id, {})
            w.writerow([
                t.id, t.name,
                meta.get("machine", ""),
                t.duration_minutes,
                meta.get("allergen", ""),
                meta.get("position", ""),
                t.deadline or "",
                meta.get("depends_on", ""),
            ])

    return path


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/solve", response_model=ScheduleResponse, tags=["scheduling"])
def solve(req: ScheduleRequest) -> ScheduleResponse:
    """Classify and solve. Writes debug/input_*.json and debug/output_*.csv automatically."""
    try:
        category, subtype = classify(req)
    except ValueError as exc:
        log.warning("Classification failed: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))

    log.info("Dispatching  category=%s  subtype=%s", category.value, subtype)
    result = dispatch(req, category, subtype)

    # Write debug files after every solve
    slug = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{category.value}_{subtype}"
    try:
        json_path = _write_input_json(req, slug)
        csv_path  = _write_output_csv(result, req, slug)
        log.info("Debug files written: %s  |  %s", json_path.name, csv_path.name)
        result.solver_info["debug_input"]  = str(json_path)
        result.solver_info["debug_output"] = str(csv_path)
    except Exception as e:
        log.warning("Failed to write debug files: %s", e)

    return result


@router.post("/classify", tags=["scheduling"])
def classify_only(req: ScheduleRequest) -> dict:
    """Dry-run: classify without solving."""
    try:
        category, subtype = classify(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    algo_map = {
        "workforce":   "CP-SAT (constraint programming)",
        "jobshop":     "CP-SAT (disjunctive interval scheduling)",
        "routing":     "OR-Tools Routing (VRP/TSP)",
        "timetabling": "CP-SAT (graph colouring / assignment)",
    }
    return {
        "detected_category": category.value,
        "detected_subtype":  subtype,
        "algorithm":         algo_map.get(category.value, "CP-SAT"),
        "resources_count":   len(req.resources),
        "tasks_count":       len(req.tasks),
        "timeslots_count":   len(req.timeslots),
        "constraints_count": len(req.constraints),
        "solver_config":     req.solver_config.model_dump(),
    }


@router.post("/debug", tags=["scheduling"])
def debug_solve(req: ScheduleRequest) -> dict:
    """Same as /solve but returns the full traceback on error."""
    try:
        category, subtype = classify(req)
    except ValueError as exc:
        return {"error": str(exc), "stage": "classify"}
    try:
        result = dispatch(req, category, subtype)
        return {
            "status":      result.status,
            "category":    result.category,
            "subtype":     result.subtype,
            "solver_info": result.solver_info,
            "kpis":        [k.model_dump() for k in result.kpis],
            "violations":  [v.model_dump() for v in result.violations],
            "assignments": len(result.assignments),
            "unassigned":  [u.model_dump() for u in result.unassigned],
        }
    except Exception:
        tb = traceback.format_exc()
        log.error("Unhandled exception in /debug:\n%s", tb)
        return {"error": "Exception during solve", "traceback": tb,
                "category": category.value, "subtype": subtype}


@router.get("/logs", tags=["system"], response_class=PlainTextResponse)
def view_logs(
    file:  str = Query(default="app",   description="'app' or 'errors'"),
    lines: int = Query(default=50,      description="Number of tail lines", ge=1, le=500),
) -> str:
    """Tail the log files in the browser. GET /logs?file=app&lines=50"""
    log_path = APP_LOG if file == "app" else ERROR_LOG
    if not log_path.exists():
        return f"Log file not found: {log_path}\n(No entries yet.)"
    all_lines = log_path.read_text(encoding="utf-8").splitlines()
    tail      = all_lines[-lines:]
    header    = f"=== {log_path.name}  (last {len(tail)} of {len(all_lines)} lines) ===\n"
    return header + "\n".join(tail) + "\n"


@router.get("/debug-files", tags=["system"])
def list_debug_files() -> dict:
    """List all saved debug files (input JSON + output CSV)."""
    files = sorted(DEBUG_DIR.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "debug_dir": str(DEBUG_DIR),
        "files": [
            {
                "name":     f.name,
                "type":     "input"  if f.name.startswith("input_") else "output",
                "format":   f.suffix.lstrip("."),
                "size_kb":  round(f.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "url":      f"/debug-files/{f.name}",
            }
            for f in files
        ]
    }


@router.get("/debug-files/{filename}", tags=["system"])
def download_debug_file(filename: str):
    """Download a specific debug file by name."""
    path = DEBUG_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    # Safety check — prevent directory traversal
    if DEBUG_DIR not in path.parents and path.parent != DEBUG_DIR:
        raise HTTPException(status_code=403, detail="Access denied")
    media = "application/json" if path.suffix == ".json" else "text/csv"
    return FileResponse(path=str(path), media_type=media, filename=filename)