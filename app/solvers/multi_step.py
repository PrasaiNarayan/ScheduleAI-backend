"""
solvers/multi_step.py — CP-SAT solver for multi-step job scheduling

Each product has N sequential steps (e.g. Mix → Bake → Pack).
Each step runs on a machine of a required type.
Constraints:
  - Steps are sequential: step k+1 starts >= end of step k + gap_after_min
  - Allergen ordering per machine per day (only on steps with allergen_applies=True)
  - No two jobs overlap on the same machine
  - same_machine_as_step: step must use same physical machine as referenced step
  - Deadline: last step must end before deadline
"""
from __future__ import annotations
from datetime import date, timedelta, time
from typing import Optional
import math

try:
    from ortools.sat.python import cp_model
    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


# ── Shift defaults (fallback if DB hours not provided) ────────────────────────
DEFAULT_SHIFT_START = 7 * 60 + 20   # 07:20 = 440 min
DEFAULT_SHIFT_END   = 22 * 60        # 22:00 = 1320 min
DAY_MIN             = 24 * 60        # 1440 min per day


def _hhmm(t) -> int:
    """Convert 'HH:MM' string or time object to minutes from midnight."""
    if t is None:
        return DEFAULT_SHIFT_START
    if isinstance(t, time):
        return t.hour * 60 + t.minute
    parts = str(t).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _abs_to_date_time(abs_min: int, start_date: date):
    """Convert absolute minutes to (date_str, HH:MM)."""
    day_idx = abs_min // DAY_MIN
    min_in_day = abs_min % DAY_MIN
    d = start_date + timedelta(days=day_idx)
    return str(d), f"{min_in_day // 60:02d}:{min_in_day % 60:02d}"


def solve_multi_step(
    products_with_steps: list[dict],
    machines_by_type: dict[str, list[dict]],
    machine_hours_by_day: dict[str, dict[str, tuple]],
    working_days: list[dict],
    allergen_order: list[str],
    start_date: date,
    time_limit_seconds: int = 60,
    locked_assignments: list[dict] | None = None,
) -> dict:
    """
    Main solver entry point.

    products_with_steps: from db.get_products_with_steps()
      each product has p['steps'] = list of step dicts with:
        step_number, step_name, machine_type_name, duration_minutes,
        gap_after_min, same_machine_as_step, allergen_applies, category_step_id

    machines_by_type: {type_name: [{id, name, shift_start, shift_end}]}
    machine_hours_by_day: {date_str: {machine_name: (start_min, end_min)}}
    working_days: [{date, day_name}]
    allergen_order: ['A','B','C','D','E','F']

    Returns:
      {assignments: [...], unscheduled: [...], status: str, solve_ms: int}
    """
    if not HAS_ORTOOLS:
        return {"error": "ortools not installed", "assignments": [], "unscheduled": []}

    num_days = len(working_days)
    if num_days == 0:
        return {"assignments": [], "unscheduled": [], "status": "NO_WORKING_DAYS"}

    horizon = num_days * DAY_MIN

    model = cp_model.CpModel()

    # ── Build allergen rank map ───────────────────────────────────────────────
    allergen_rank = {a: i for i, a in enumerate(allergen_order)}

    # ── Working day index → date string ──────────────────────────────────────
    day_dates = [d["date"] for d in working_days]

    # ── Get shift window for a machine on a day ───────────────────────────────
    def get_shift(machine_name: str, day_idx: int):
        day_str = day_dates[day_idx] if day_idx < len(day_dates) else None
        if day_str and day_str in machine_hours_by_day:
            mh = machine_hours_by_day[day_str].get(machine_name)
            if mh:
                return mh[0], mh[1]  # (start_min, end_min) within day
        return DEFAULT_SHIFT_START, DEFAULT_SHIFT_END

    # ── Index all machines ────────────────────────────────────────────────────
    all_machine_ids:   list[int]  = []
    all_machine_names: list[str]  = []
    machine_id_to_idx: dict       = {}

    for mtype, machines in machines_by_type.items():
        for m in machines:
            idx = len(all_machine_ids)
            all_machine_ids.append(m["id"])
            all_machine_names.append(m["name"])
            machine_id_to_idx[m["id"]] = idx

    n_machines = len(all_machine_ids)

    # ── Per-machine interval lists for no-overlap ─────────────────────────────
    machine_intervals: list[list] = [[] for _ in range(n_machines)]

    # ── Separate hard locks from soft locks ───────────────────────────────────
    # hard locks: exact time+machine — injected as fixed blocked intervals
    # soft locks: day-only — constrain product to stay on same day, time flexible

    def _parse_t(t):
        if hasattr(t, 'hour'):
            return t.hour * 60 + t.minute
        parts = str(t).split(":")
        return int(parts[0]) * 60 + int(parts[1])

    day_date_to_idx = {d: i for i, d in enumerate(day_dates)}

    # soft_lock_info: {(product_id, order_line_id): {step_number: (day_idx, machine_id)}}
    # tells us which day AND which machine each step of a soft-locked product must use
    soft_lock_info: dict = {}

    if locked_assignments:
        for la in locked_assignments:
            if not la.get("locked"):
                continue
            scope = la.get("lock_scope") or "product"
            sdate = str(la.get("scheduled_date", ""))[:10]
            d_idx = day_date_to_idx.get(sdate, -1)
            if d_idx < 0:
                continue

            if scope == "soft":
                # Soft lock — fix day + machine per step, keep time flexible
                key      = (la.get("product_id"), la.get("order_line_id"))
                step_num = la.get("step_number") or 0
                m_id     = la.get("machine_id")
                if key not in soft_lock_info:
                    soft_lock_info[key] = {}
                soft_lock_info[key][step_num] = (d_idx, m_id)

            else:
                # Hard lock — inject as fixed blocked interval on this machine
                m_id = la.get("machine_id")
                if m_id not in machine_id_to_idx:
                    continue
                m_idx = machine_id_to_idx[m_id]
                s_abs = d_idx * DAY_MIN + _parse_t(la["start_time"])
                e_abs = d_idx * DAY_MIN + _parse_t(la["end_time"])
                dur   = e_abs - s_abs
                if dur <= 0:
                    continue
                fix_s = model.new_constant(s_abs)
                fix_e = model.new_constant(e_abs)
                fix_interval = model.new_interval_var(
                    fix_s, dur, fix_e,
                    f"locked_m{m_idx}_d{d_idx}_s{s_abs}"
                )
                machine_intervals[m_idx].append(fix_interval)

    # ── Step variables ────────────────────────────────────────────────────────
    # step_vars[product_idx][step_idx] = {
    #   start, end, interval, machine_idx, assigned (bool),
    #   on_day: [bool per day], step_info
    # }
    step_vars: list[list[dict]] = []
    product_scheduled: list     = []  # bool var per product
    unscheduled_pre:  list      = []  # products dropped before solver runs (deadline passed)

    EARLY_W  = 1        # kept for reference — overridden in objective section below

    for p_idx, product in enumerate(products_with_steps):
        steps     = product["steps"]
        deadline  = product.get("deadline")
        p_sched   = model.new_bool_var(f"p{p_idx}_sched")

        # Deadline handling — HARD RULE
        # If the product cannot finish by its deadline it must NOT be scheduled.
        # dl_abs = absolute minute by which the last step must END.
        # If deadline < plan start → impossible to meet → force unscheduled.
        # If deadline >= plan start → enforce hard constraint on last step end.
        dl_abs       = None
        deadline_ok  = True   # False = impossible to meet, skip this product
        if deadline:
            try:
                dl_date    = date.fromisoformat(str(deadline)[:10])
                dl_day_idx = (dl_date - start_date).days
                if dl_day_idx < 0:
                    # Deadline already passed — cannot schedule at all
                    deadline_ok = False
                elif dl_day_idx < num_days:
                    dl_abs = dl_day_idx * DAY_MIN + DEFAULT_SHIFT_END
                else:
                    # Deadline beyond planning window — use last working day
                    dl_abs = (num_days - 1) * DAY_MIN + DEFAULT_SHIFT_END
            except (ValueError, TypeError):
                pass

        # If deadline is impossible, mark as unscheduled immediately
        if not deadline_ok:
            unscheduled_pre.append({
                "product_id":    product.get("product_id") or product["id"],
                "order_line_id": product.get("order_line_id"),
                "product_name":  product.get("product_name") or product["name"],
                "reason":        f"Deadline {deadline} already passed — cannot schedule",
            })
            # Force this product's bool to 0 so it doesn't count in objective
            model.add(p_sched == 0)
            product_scheduled.append(p_sched)
            step_vars.append([])
            continue

        product_scheduled.append(p_sched)
        product["_deadline_dropped"] = False  # deadline is valid, solver will handle it

        # ── Soft lock: constrain this product to its original day(s) and machine ─
        # day fixed, machine fixed, time flexible within the shift window
        p_key       = (product.get("product_id") or product["id"],
                       product.get("order_line_id"))
        p_soft_info = soft_lock_info.get(p_key)  # {step_num: (day_idx, machine_id)}

        p_step_vars = []
        for s_idx, step in enumerate(steps):
            dur         = step["duration_minutes"]
            mtype_name  = step["machine_type_name"]
            gap_after   = step["gap_after_min"]

            # Machines eligible for this step
            eligible_machines = machines_by_type.get(mtype_name, [])
            if not eligible_machines:
                # No machines of required type — product can't be scheduled
                model.add(p_sched == 0)
                break

            # One interval per eligible machine
            opt_vars = []
            for m in eligible_machines:
                m_idx = machine_id_to_idx[m["id"]]

                # ── Soft lock: skip machines that aren't the locked one ────────
                if p_soft_info is not None:
                    step_num = step.get("step_number", s_idx + 1)
                    if step_num in p_soft_info:
                        _, locked_m_id = p_soft_info[step_num]
                        if m["id"] != locked_m_id:
                            continue  # only allow the originally assigned machine
                s_var = model.new_int_var(0, horizon, f"p{p_idx}s{s_idx}m{m_idx}_s")
                e_var = model.new_int_var(0, horizon, f"p{p_idx}s{s_idx}m{m_idx}_e")
                asgn  = model.new_bool_var(f"p{p_idx}s{s_idx}m{m_idx}_a")
                intv  = model.new_optional_interval_var(s_var, dur, e_var, asgn,
                            f"p{p_idx}s{s_idx}m{m_idx}_intv")
                machine_intervals[m_idx].append(intv)

                # Build per-day on_day booleans
                day_choices = []
                for d_idx in range(num_days):
                    # ── Soft lock: restrict to locked day ─────────────────────
                    if p_soft_info is not None:
                        step_num = step.get("step_number", s_idx + 1)
                        if step_num in p_soft_info:
                            locked_d_idx, _ = p_soft_info[step_num]
                            if d_idx != locked_d_idx:
                                continue  # skip wrong day
                        else:
                            continue  # step not tracked — skip

                    sh_s, sh_e = get_shift(m["name"], d_idx)
                    shift_len = sh_e - sh_s
                    if dur > shift_len:
                        continue  # won't fit on this machine this day

                    d_lo  = d_idx * DAY_MIN + sh_s
                    d_hi  = d_idx * DAY_MIN + sh_e - dur
                    d_end = d_idx * DAY_MIN + sh_e

                    on_day = model.new_bool_var(f"p{p_idx}s{s_idx}m{m_idx}d{d_idx}")
                    model.add(s_var >= d_lo).only_enforce_if(on_day)
                    model.add(s_var <= d_hi).only_enforce_if(on_day)
                    model.add(e_var <= d_end).only_enforce_if(on_day)

                    before = model.new_bool_var(f"p{p_idx}s{s_idx}m{m_idx}d{d_idx}_b")
                    model.add(s_var < d_lo).only_enforce_if(before)
                    model.add(s_var >= d_lo).only_enforce_if(before.negated())
                    after  = model.new_bool_var(f"p{p_idx}s{s_idx}m{m_idx}d{d_idx}_a")
                    model.add(s_var > d_hi).only_enforce_if(after)
                    model.add(s_var <= d_hi).only_enforce_if(after.negated())
                    model.add_bool_or([on_day, before, after])
                    day_choices.append(on_day)

                if day_choices:
                    model.add(sum(day_choices) == 1).only_enforce_if(asgn)
                    model.add(sum(day_choices) == 0).only_enforce_if(asgn.negated())
                else:
                    model.add(asgn == 0)

                model.add(e_var == s_var + dur).only_enforce_if(asgn)
                model.add(s_var == 0).only_enforce_if(asgn.negated())
                model.add(e_var == 0).only_enforce_if(asgn.negated())

                opt_vars.append({
                    "start":    s_var,
                    "end":      e_var,
                    "assigned": asgn,
                    "machine_id": m["id"],
                    "machine_name": m["name"],
                    "m_idx":    m_idx,
                    "on_day":   day_choices,
                })

            # Exactly one machine chosen when product is scheduled
            if opt_vars:
                model.add(sum(ov["assigned"] for ov in opt_vars) == 1).only_enforce_if(p_sched)
                model.add(sum(ov["assigned"] for ov in opt_vars) == 0).only_enforce_if(p_sched.negated())

            p_step_vars.append({
                "opt_vars":  opt_vars,
                "step_info": step,
                "gap_after": gap_after,
                "dur":       dur,
            })

        step_vars.append(p_step_vars)

    # ── Sequential step constraints ───────────────────────────────────────────
    for p_idx, product in enumerate(products_with_steps):
        p_sched    = product_scheduled[p_idx]
        p_sv       = step_vars[p_idx]
        deadline   = product.get("deadline")
        steps      = product["steps"]

        # Deadline on last step
        dl_abs = None
        if deadline:
            try:
                dl_date = date.fromisoformat(deadline)
                dl_day_idx = (dl_date - start_date).days
                if dl_day_idx >= 0:
                    dl_abs = min(dl_day_idx, num_days - 1) * DAY_MIN + DEFAULT_SHIFT_END
            except ValueError:
                pass

        for s_idx in range(len(p_sv)):
            sv = p_sv[s_idx]
            if not sv["opt_vars"]:
                continue

            # Aggregate start/end across machine options
            # Use auxiliary vars: step_start = whichever machine is assigned
            step_s = model.new_int_var(0, horizon, f"p{p_idx}s{s_idx}_S")
            step_e = model.new_int_var(0, horizon, f"p{p_idx}s{s_idx}_E")
            for ov in sv["opt_vars"]:
                model.add(step_s == ov["start"]).only_enforce_if(ov["assigned"])
                model.add(step_e == ov["end"]).only_enforce_if(ov["assigned"])
            sv["step_s"] = step_s
            sv["step_e"] = step_e

            # Sequential: next step starts after current step ends + gap
            if s_idx < len(p_sv) - 1:
                nv = p_sv[s_idx + 1]
                if nv["opt_vars"]:
                    gap = sv["gap_after"]
                    # Apply gap directly to each machine option for next step
                    for ov_next in nv["opt_vars"]:
                        model.add(ov_next["start"] >= step_e + gap).only_enforce_if(
                            [p_sched, ov_next["assigned"]])

            # Deadline on last step
            if s_idx == len(p_sv) - 1 and dl_abs is not None:
                model.add(step_e <= dl_abs).only_enforce_if(p_sched)

            # same_machine_as_step constraint
            same_as = steps[s_idx].get("same_machine_as_step")
            if same_as is not None and same_as > 0:
                ref_s_idx = same_as - 1  # step_number is 1-based
                if ref_s_idx < len(p_sv) and ref_s_idx != s_idx:
                    ref_sv = p_sv[ref_s_idx]
                    # For each machine: if ref step uses it, current step must too
                    for ov_curr in sv["opt_vars"]:
                        for ov_ref in ref_sv["opt_vars"]:
                            if ov_curr["machine_id"] == ov_ref["machine_id"]:
                                # If ref is assigned, curr must be assigned too
                                model.add(ov_curr["assigned"] == 1).only_enforce_if(
                                    [p_sched, ov_ref["assigned"]])
                            else:
                                # If ref uses machine X, curr can't use different machine
                                model.add(ov_curr["assigned"] == 0).only_enforce_if(
                                    ov_ref["assigned"])

    # ── No-overlap per machine ────────────────────────────────────────────────
    for m_idx in range(n_machines):
        if machine_intervals[m_idx]:
            model.add_no_overlap(machine_intervals[m_idx])

    # ── Allergen ordering per machine per day ─────────────────────────────────
    # Only on steps where allergen_applies = True
    # Group (product, step) by allergen tier
    allergen_step_list = []  # (p_idx, s_idx, tier, opt_vars, on_day)
    for p_idx, product in enumerate(products_with_steps):
        allergen = product.get("allergen")
        tier = allergen_rank.get(allergen, -1)
        if tier < 0:
            continue
        for s_idx, sv in enumerate(step_vars[p_idx]):
            if not sv["opt_vars"]:
                continue
            if not sv["step_info"].get("allergen_applies", True):
                continue
            allergen_step_list.append((p_idx, s_idx, tier, sv["opt_vars"]))

    # For each machine, for each pair of steps with different allergen tiers,
    # enforce order within the same day
    machine_to_steps: dict[int, list] = {}
    for p_idx, s_idx, tier, opt_vars in allergen_step_list:
        for ov in opt_vars:
            m_idx = ov["m_idx"]
            machine_to_steps.setdefault(m_idx, []).append(
                (p_idx, s_idx, tier, ov))

    for m_idx, entries in machine_to_steps.items():
        for i, (p_a, s_a, tier_a, ov_a) in enumerate(entries):
            for p_b, s_b, tier_b, ov_b in entries:
                if (p_a == p_b and s_a == s_b) or tier_a >= tier_b:
                    continue
                # Same-day allergen order: ov_a ends before ov_b starts
                od_a = ov_a["on_day"]
                od_b = ov_b["on_day"]
                for d_idx in range(min(len(od_a), len(od_b))):
                    model.add(ov_a["end"] <= ov_b["start"]).only_enforce_if(
                        [ov_a["assigned"], ov_b["assigned"],
                         od_a[d_idx], od_b[d_idx]]
                    )

    # ── Position ordering per machine ────────────────────────────────────────
    # HARD CONSTRAINT: if position rule cannot be satisfied, product is NOT
    # scheduled (solver drops it, same as deadline violation).
    #
    # first: on whichever machine this step is assigned to,
    #        this step's end <= ALL other products' step starts on that machine
    # last:  on whichever machine this step is assigned to,
    #        this step's start >= ALL other products' step ends on that machine
    #
    # Allergen order still applies independently — if first+allergen conflict
    # the solver will simply not assign the product to that machine/day.

    # Collect all steps that participate in allergen ordering (Mix, Bake, Fry)
    all_pos_steps = []  # (p_idx, s_idx, position, p_sched, opt_vars)
    for p_idx, product in enumerate(products_with_steps):
        pos     = product.get("position")
        p_sched = product_scheduled[p_idx]
        for s_idx, sv in enumerate(step_vars[p_idx]):
            if not sv["opt_vars"]:
                continue
            if not sv["step_info"].get("allergen_applies", True):
                continue
            all_pos_steps.append((p_idx, s_idx, pos, p_sched, sv["opt_vars"]))

    position_constraints_added = 0

    # For each first/last product step, enforce ordering against every other step
    for i, (p_a, s_a, pos_a, sched_a, ovs_a) in enumerate(all_pos_steps):
        if pos_a not in ("first", "last"):
            continue  # only constrain positioned products

        for j, (p_b, s_b, pos_b, sched_b, ovs_b) in enumerate(all_pos_steps):
            if p_a == p_b:
                continue  # same product

            # For first: A must end before B starts (B is any non-first product)
            # For last:  B must end before A starts (B is any non-last product)
            if pos_a == "first" and pos_b == "first":
                continue  # two first-only products — allergen handles their order
            if pos_a == "last" and pos_b == "last":
                continue  # two last-only products — allergen handles their order

            a_before_b = (pos_a == "first")  # first before everything else
            b_before_a = (pos_a == "last")   # everything else before last

            for ov_a in ovs_a:
                for ov_b in ovs_b:
                    if ov_a["m_idx"] != ov_b["m_idx"]:
                        continue  # different machines — no constraint
                    if a_before_b:
                        model.add(ov_a["end"] <= ov_b["start"]).only_enforce_if(
                            [ov_a["assigned"], ov_b["assigned"]])
                        position_constraints_added += 1
                    else:  # b_before_a (last)
                        model.add(ov_b["end"] <= ov_a["start"]).only_enforce_if(
                            [ov_a["assigned"], ov_b["assigned"]])
                        position_constraints_added += 1

    print(f"[position] steps={len(all_pos_steps)} "
          f"positioned={sum(1 for _,_,p,_,_ in all_pos_steps if p)} "
          f"constraints={position_constraints_added}")

    # ── Objective ─────────────────────────────────────────────────────────────
    # Priority is used to weight which products MUST be scheduled.
    # Higher priority (10) = 10x more important to schedule than priority 1.
    # Allergen ORDER (A before B) is a hard constraint — separate from priority.
    # A product with priority=8 and allergen=B will still run AFTER allergen=A
    # products on the same machine that day — allergen order is non-negotiable.
    # But if capacity is tight and we can only fit 10 of 13 products,
    # the solver will drop priority=3 products before priority=8 ones.

    PRIORITY_W = 100_000  # scheduling a product is worth 100k × priority
    EARLY_W    = 10       # 10 pts per minute earlier — enough to prefer
                          # May 20 over May 22 (2 days = 2880 min × 10 = 28,800 pts)
                          # but small enough not to block finding feasible solution

    weighted_scheduled = []
    effective_starts   = []

    for p_idx, product in enumerate(products_with_steps):
        p_sched  = product_scheduled[p_idx]
        priority = int(product.get("priority", 5))
        p_sv     = step_vars[p_idx]

        # Priority-weighted scheduling term
        weighted_scheduled.append(priority * PRIORITY_W * p_sched)

        # Early completion: minimise end time of last step (Pack)
        # Using END not START so cooling gap is also pulled earlier
        last_sv = None
        for sv in reversed(p_sv):
            if sv["opt_vars"]:
                last_sv = sv
                break
        if last_sv:
            ee = model.new_int_var(0, horizon, f"ee_{p_idx}")
            for ov in last_sv["opt_vars"]:
                model.add(ee == ov["end"]).only_enforce_if(
                    [p_sched, ov["assigned"]])
            model.add(ee == 0).only_enforce_if(p_sched.negated())
            effective_starts.append(ee)

    model.maximize(
        sum(weighted_scheduled)           # primary: schedule all products
        - EARLY_W * sum(effective_starts) # secondary: minimise last-step end time
    )

    # ── Solve ─────────────────────────────────────────────────────────────────
    solver   = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = 1   # deterministic — parallel workers cause different results
    solver.parameters.random_seed = 42  # fixed seed — same input always gives same output
    status   = solver.solve(model)
    solve_ms = int(solver.wall_time * 1000)

    status_map = {
        cp_model.OPTIMAL:   "OPTIMAL",
        cp_model.FEASIBLE:  "FEASIBLE",
        cp_model.INFEASIBLE:"INFEASIBLE",
        cp_model.UNKNOWN:   "UNKNOWN",
    }
    status_str = status_map.get(status, "UNKNOWN")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        unscheduled = [{"product_id":    p.get("product_id") or p["id"],
                        "order_line_id": p.get("order_line_id"),
                        "product_name":  p.get("product_name") or p["name"],
                        "reason": status_str}
                       for p in products_with_steps]
        return {"assignments": [], "unscheduled": unscheduled_pre + unscheduled,
                "status": status_str, "solve_ms": solve_ms}

    # ── Extract results ───────────────────────────────────────────────────────
    assignments  = []
    unscheduled  = []

    for p_idx, product in enumerate(products_with_steps):
        p_sched = product_scheduled[p_idx]
        if not solver.value(p_sched):
            unscheduled.append({
                "product_id":    product.get("product_id") or product["id"],
                "order_line_id": product.get("order_line_id"),
                "product_name":  product.get("product_name") or product["name"],
                "reason":        "Could not fit in planning window",
            })
            continue

        p_sv = step_vars[p_idx]
        for s_idx, sv in enumerate(p_sv):
            if not sv["opt_vars"]:
                continue
            step_info = sv["step_info"]

            # Find which machine was chosen
            chosen = None
            for ov in sv["opt_vars"]:
                if solver.value(ov["assigned"]):
                    chosen = ov
                    break

            if chosen is None:
                continue

            s_val = solver.value(chosen["start"])
            e_val = solver.value(chosen["end"])
            sched_date, start_time = _abs_to_date_time(s_val, start_date)
            _,           end_time  = _abs_to_date_time(e_val, start_date)

            allergen = product.get("allergen") if step_info.get("allergen_applies") else None

            assignments.append({
                "job_id":           product["id"],           # = order_line_id when order-based
                "product_id":       product.get("product_id") or product["id"],
                "product_name":     product.get("product_name") or product["name"],
                "order_line_id":    product.get("order_line_id"),
                "category_step_id": step_info["category_step_id"],
                "step_number":      step_info["step_number"],
                "step_name":        step_info["step_name"],
                "machine_id":       chosen["machine_id"],
                "machine_name":     chosen["machine_name"],
                "machine_type":     step_info["machine_type_name"],
                "scheduled_date":   sched_date,
                "start_time":       start_time,
                "end_time":         end_time,
                "duration_minutes": sv["dur"],
                "allergen":         allergen,
                "position_slot":    None,
            })

    return {
        "assignments":  assignments,
        "unscheduled":  unscheduled_pre + unscheduled,  # pre = deadline passed, solver = couldn't fit
        "status":       status_str,
        "solve_ms":     solve_ms,
    }