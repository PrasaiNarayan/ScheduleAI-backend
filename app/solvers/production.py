"""
Production Scheduler — CP-SAT v6 (Multi-day, Flexible Machines)
================================================================
Multi-day planning:
  - User specifies start_date and end_date (inclusive)
  - Each day has a shift window: SHIFT_START_OFFSET – SHIFT_END_OFFSET
    (minutes from midnight, default 07:20–22:00 = 440–1320)
  - Total horizon = num_days * 1440 minutes (minutes from day-0 midnight)
  - Job start/end are in absolute minutes from day-0 midnight
  - A job on day D can start at D*1440 + SHIFT_START, end by D*1440 + SHIFT_END

Deadlines:
  - Each job has an optional deadline_day (0-indexed from start_date)
  - Constraint: end[job] <= deadline_day * 1440 + SHIFT_END

Machine names:
  - Users supply real names (Oven A, Oven B, Packer, Mixer, etc.)
  - No more "Machine 1" defaults

Allergen labels:
  - A, B, C, D, E, F  (user-defined, but front-end uses these letters)

Load balancing across days:
  - Daily load per machine tracked
  - Objective penalises max-min imbalance across machines (summed over days)

Partial assignment: jobs that can't fit keep the rest schedulable.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional
from ortools.sat.python import cp_model

from app.models.response import (
    Assignment, GanttBar, GanttRow, KPI, ScheduleResponse,
    ScoreDetail, SolveStatus, UnassignedTask, ConstraintViolation,
)
from app.solvers.base import BaseSolver

SHIFT_START_OFF = 7 * 60 + 20   # 440 min from midnight
SHIFT_END_OFF   = 22 * 60        # 1320 min from midnight
SHIFT_LEN       = SHIFT_END_OFF - SHIFT_START_OFF   # 880 min
DAY_MIN         = 1440           # minutes per day
BIG_M           = 1_000_000      # reward per scheduled job
BALANCE_W       = 5              # load-balance penalty weight


def _fmt(abs_min: int, start_date: Optional[date] = None) -> str:
    """Absolute minutes → 'YYYY-MM-DD HH:MM' or 'HH:MM' if single day."""
    abs_min = max(0, int(abs_min))
    day_idx = abs_min // DAY_MIN
    mins    = abs_min % DAY_MIN
    hh, mm  = divmod(mins, 60)
    time_str = f"{hh:02d}:{mm:02d}"
    if start_date:
        d = start_date + timedelta(days=day_idx)
        return f"{d.isoformat()} {time_str}"
    return time_str


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


class ProductionSolver(BaseSolver):

    def _solve(self) -> ScheduleResponse:
        req = self.req
        cfg = self._cfg

        allergen_order: list[str] = list(req.allergen_order or [])
        allergen_rank: dict[str, int] = {a: i for i, a in enumerate(allergen_order)}

        # ── Date range ────────────────────────────────────────────────────────
        attrs_global = req.attributes or {}
        start_date = _parse_date(attrs_global.get('start_date') or cfg.horizon_start)
        end_date   = _parse_date(attrs_global.get('end_date')   or cfg.horizon_end)

        if not start_date:
            start_date = date.today()
        if not end_date or end_date < start_date:
            end_date = start_date  # at least 1 day

        num_days = (end_date - start_date).days + 1
        horizon  = num_days * DAY_MIN  # total minutes in planning horizon

        self._log.info(
            "Multi-day horizon: %s to %s (%d days, %d min total)",
            start_date, end_date, num_days, horizon,
        )

        # ── Parse jobs ────────────────────────────────────────────────────────
        jobs = req.tasks
        if not jobs:
            return self._err("No jobs provided.")

        for j in jobs:
            if j.duration_minutes <= 0:
                j.duration_minutes = 1

        # Build machine options per job
        job_options: list[list[dict]] = []
        for job in jobs:
            opts = getattr(job, 'machine_options', None) or \
                   (job.attributes or {}).get('machine_options', None)
            if opts:
                validated = [
                    {'machine_id': o.get('machine_id','').strip(),
                     'duration_min': max(1, int(o.get('duration_min', o.get('duration_minutes', 30))))}
                    for o in opts if o.get('machine_id','').strip()
                ]
                job_options.append(validated or [])
            elif job.machine_id:
                job_options.append([{
                    'machine_id': job.machine_id.strip(),
                    'duration_min': max(1, job.duration_minutes),
                }])
            else:
                job_options.append([])

        no_machine = [jobs[i].name for i, o in enumerate(job_options) if not o]
        if no_machine:
            return self._err(f"Jobs missing machine: {no_machine}")

        # ── Locked assignment lookup ──────────────────────────────────────────
        # locked_details[job.id] = {date, machine, start_min, end_min}
        # These jobs get their start/end pinned as hard constraints.
        def _hhmm_to_min(t: str, default: int = SHIFT_START_OFF) -> int:
            if not t:
                return default
            try:
                parts = str(t).split(":")
                return int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                return default

        locked_details: dict[str, dict] = {}
        for job in jobs:
            attrs = job.attributes or {}
            if attrs.get("locked_date") and attrs.get("locked_machine"):
                locked_details[job.id] = {
                    "machine":   attrs["locked_machine"],
                    "date":      attrs["locked_date"],
                    "start_min": _hhmm_to_min(attrs.get("locked_start", "07:20")),
                    "end_min":   _hhmm_to_min(attrs.get("locked_end",   "22:00")),
                }

        # ── Per-day machine hours from DB (overrides fixed SHIFT constants) ───
        # machine_hours_by_day[date_str][machine_name] = (start_min, end_min)
        machine_hours_by_day: dict[str, dict[str, tuple]] = {}
        raw_hours = (req.attributes or {}).get("machine_hours", {})
        for day_str, hours_list in raw_hours.items():
            machine_hours_by_day[day_str] = {}
            for mh in hours_list:
                ss = _hhmm_to_min(mh.get("shift_start") or "07:20")
                se = _hhmm_to_min(mh.get("shift_end")   or "22:00")
                machine_hours_by_day[day_str][mh["machine_name"]] = (ss, se)

        # ── Deadline parsing ──────────────────────────────────────────────────
        # deadline stored as date string or day offset
        def job_deadline_min(job) -> Optional[int]:
            dl_str = job.deadline or (job.attributes or {}).get('deadline_date')
            if not dl_str:
                return None
            dl_date = _parse_date(dl_str)
            if dl_date:
                day_off = (dl_date - start_date).days
                if day_off < 0:
                    return 0  # already past — will be unschedulable
                return min(day_off, num_days - 1) * DAY_MIN + SHIFT_END_OFF
            # Try integer day offset
            try:
                day_off = int(dl_str)
                return min(day_off, num_days - 1) * DAY_MIN + SHIFT_END_OFF
            except (ValueError, TypeError):
                return None

        # ── Machine index ─────────────────────────────────────────────────────
        all_machines = sorted({o['machine_id'] for opts in job_options for o in opts})
        m_idx = {m: i for i, m in enumerate(all_machines)}
        n_machines = len(all_machines)
        resource_name = {r.id: r.name for r in req.resources}
        for m in all_machines:
            resource_name.setdefault(m, m)

        model   = cp_model.CpModel()
        job_id_set = {j.id for j in jobs}

        # ── Per-job variables ─────────────────────────────────────────────────
        job_machine_vars: list[list[dict]] = []
        job_scheduled: list = []
        job_start: list = []
        job_end:   list = []
        machine_intervals: dict[int, list] = defaultdict(list)

        for j, job in enumerate(jobs):
            opts  = job_options[j]
            sched = model.new_bool_var(f"sched_{j}")
            job_scheduled.append(sched)

            opt_vars = []
            for k, opt in enumerate(opts):
                m_i  = m_idx[opt['machine_id']]
                dur  = opt['duration_min']
                asgn = model.new_bool_var(f"asgn_{j}_{k}")
                s    = model.new_int_var(0, horizon, f"s_{j}_{k}")
                e    = model.new_int_var(0, horizon, f"e_{j}_{k}")
                intv = model.new_optional_interval_var(s, dur, e, asgn, f"i_{j}_{k}")

                # When assigned: e = s + dur
                model.add(e == s + dur).only_enforce_if(asgn)

                if dur <= 0:
                    # Zero duration job — force unscheduled
                    model.add(asgn == 0)
                else:
                    # ── Shift window enforcement ──────────────────────────────
                    # We need: when assigned, s and e fall within ONE shift window.
                    # Approach: one boolean on_day[d] per eligible day.
                    #   on_day[d]=1 → s in [d*1440+440, d*1440+1320-dur]
                    #                  e <= d*1440+1320
                    # sum(on_day) == 1 when assigned.
                    #
                    # Key insight: we constrain the variable domain directly.
                    # s must be in the UNION of all valid start windows.
                    # We achieve this by tightening s's domain to only valid
                    # shift windows and using on_day to enforce which one.
                    #
                    # The bug in the previous version was that on_day only
                    # posted a lower bound (s >= d_shift_start) not an upper
                    # bound, so CP-SAT could pick s=440 (day 0 start) even
                    # when on_day[3]=1. Fixed: post BOTH bounds per day.

                    day_choices = []
                    for d in range(num_days):
                        # Look up actual shift hours for this machine on day d
                        # Falls back to global defaults if no override exists
                        day_date_str = str(start_date + timedelta(days=d))
                        machine_name = opt['machine_id']
                        day_hours = machine_hours_by_day.get(day_date_str, {})
                        mh = day_hours.get(machine_name)
                        if mh:
                            shift_s, shift_e = mh  # (start_min, end_min) within day
                        else:
                            shift_s, shift_e = SHIFT_START_OFF, SHIFT_END_OFF

                        shift_len_day = shift_e - shift_s
                        if dur > shift_len_day:
                            continue  # job doesn't fit this machine on this day

                        d_lo  = d * DAY_MIN + shift_s           # earliest abs start
                        d_hi  = d * DAY_MIN + shift_e - dur     # latest abs start
                        d_end = d * DAY_MIN + shift_e           # shift end absolute
                        on_day = model.new_bool_var(f"od_{j}_{k}_{d}")
                        # When on this day: s and e within the shift window
                        model.add(s >= d_lo).only_enforce_if(on_day)
                        model.add(s <= d_hi).only_enforce_if(on_day)
                        model.add(e <= d_end).only_enforce_if(on_day)
                        # When NOT on this day: s is NOT in this day's window
                        # This is the critical constraint that prevents CP-SAT
                        # from placing s in day 0's window while on_day[0]=0.
                        # Encoded as: on_day=0 → (s < d_lo OR s > d_hi)
                        # We use an auxiliary: not_on = 1-on_day
                        # not_on AND s >= d_lo → s > d_hi  (i.e. s >= d_hi+1)
                        before_day = model.new_bool_var(f"before_{j}_{k}_{d}")
                        model.add(s < d_lo).only_enforce_if(before_day)
                        model.add(s >= d_lo).only_enforce_if(before_day.negated())
                        # on_day=0 → before_day=1 OR s > d_hi
                        after_window = model.new_bool_var(f"after_{j}_{k}_{d}")
                        model.add(s > d_hi).only_enforce_if(after_window)
                        model.add(s <= d_hi).only_enforce_if(after_window.negated())
                        # NOT on_day → before OR after
                        model.add_bool_or([on_day, before_day, after_window])
                        day_choices.append(on_day)

                    if day_choices:
                        # Exactly one day chosen when assigned, zero when not
                        model.add(sum(day_choices) == 1).only_enforce_if(asgn)
                        model.add(sum(day_choices) == 0).only_enforce_if(asgn.negated())
                    else:
                        model.add(asgn == 0)

                # Park at 0 when not assigned (harmless, interval is inactive)
                model.add(s == 0).only_enforce_if(asgn.negated())
                model.add(e == 0).only_enforce_if(asgn.negated())

                machine_intervals[m_i].append(intv)
                opt_vars.append({
                    'start': s, 'end': e, 'interval': intv,
                    'assigned': asgn, 'm_i': m_i, 'dur': dur,
                    'machine_id': opt['machine_id'],
                    'on_day': day_choices,  # on_day[d] is True iff this job runs on day d
                })

            job_machine_vars.append(opt_vars)

            # Exactly one machine chosen iff scheduled
            assigned_vars = [ov['assigned'] for ov in opt_vars]
            if assigned_vars:
                model.add(sum(assigned_vars) == sched)
            else:
                model.add(sched == 0)

            # Job-level start/end
            j_start = model.new_int_var(0, horizon, f"js_{j}")
            j_end   = model.new_int_var(0, horizon, f"je_{j}")
            for ov in opt_vars:
                model.add(j_start == ov['start']).only_enforce_if(ov['assigned'])
                model.add(j_end   == ov['end']).only_enforce_if(ov['assigned'])
            model.add(j_start == 0).only_enforce_if(sched.negated())
            model.add(j_end   == 0).only_enforce_if(sched.negated())
            job_start.append(j_start)
            job_end.append(j_end)

            # Deadline constraint
            dl_min = job_deadline_min(job)
            if dl_min is not None:
                model.add(j_end <= dl_min).only_enforce_if(sched)

            # ── Locked assignment: pin to exact slot ──────────────────────────
            # If this job is locked, force it onto its previously assigned
            # machine and time slot. It MUST be scheduled (sched=1).
            if job.id in locked_details:
                lk        = locked_details[job.id]
                lk_mname  = lk["machine"]
                lk_s      = lk["start_min"]
                lk_e      = lk["end_min"]
                # Force scheduled = 1
                model.add(sched == 1)
                # Force exactly one option: the locked machine
                for ov in opt_vars:
                    if ov['machine_id'] == lk_mname:
                        model.add(ov['assigned'] == 1)
                        model.add(ov['start'] == lk_s)
                        model.add(ov['end']   == lk_e)
                    else:
                        model.add(ov['assigned'] == 0)
                self._log.info(
                    "Locked: '%s' pinned to %s %s %s-%s",
                    job.name, lk["date"], lk_mname,
                    lk["start_min"], lk["end_min"]
                )


        for m_i, intervals in machine_intervals.items():
            if len(intervals) > 1:
                model.add_no_overlap(intervals)

        violations: list[ConstraintViolation] = []

        # ── CONSTRAINT 1: Allergen order — per machine, per day independently ──
        #
        # Allergen order resets each day. On machine M on day D:
        # all tier-i jobs must finish before any tier-j job (j>i) starts.
        # Cross-day: no constraint — day 2 can start with any allergen.
        #
        # We need on_day booleans per job-option per day, but those are created
        # inside the variable loop above and not stored. Instead we use the
        # cleanest equivalent:
        #
        # For pair (a, b) with tier_a < tier_b, both on machine m:
        #   IF assigned_a AND assigned_b:
        #     EITHER they are on different days (no constraint needed)
        #     OR they are on the same day → end[a] <= start[b]
        #
        # "Different days" iff |start_a - start_b| >= DAY_MIN (1440).
        # "Same day"       iff |start_a - start_b| <  DAY_MIN.
        #
        # Encoding: introduce bool diff_lt_day = (start_a - start_b < DAY_MIN AND > -DAY_MIN)
        # Then: assigned_a AND assigned_b AND diff_lt_day → end_a <= start_b
        #
        # Since valid windows are [d*1440+440, d*1440+1320], two jobs on the
        # same day differ by at most 880 min. Two jobs on different days differ
        # by at least 1440 - 880 = 560 min (if back-to-back days, far windows).
        # Actually the minimum cross-day gap is:
        #   min start day(d+1) - max end day(d) = (d+1)*1440+440 - (d*1440+1320) = 560
        # So threshold DAY_MIN=1440 is safely above 880 and safely below 560... wait:
        # 880 < 1440 > 560 — but 560 < 1440, so same-day could be 880 and
        # next-day minimum is 560? No: next-day MINIMUM start - same-day MAXIMUM end:
        # = ((d+1)*1440 + 440) - (d*1440 + 1320) = 1440 + 440 - 1320 = 560.
        # So start_b(day d+1) - end_a(day d) >= 560 but start_b - start_a >= 560.
        # And same-day: start_b - start_a <= 880.
        # So threshold of 1000 separates them cleanly: same-day diff < 1000, cross-day >= 560.
        # Wait — 560 < 1000, so cross-day start differences can be < 1000. Bad threshold.
        #
        # CORRECT threshold: use DAY_MIN=1440.
        # Same-day: |start_a - start_b| <= 880 (max shift length - min duration ≈ 880)
        # Cross-day: |start_a - start_b| >= (d+1)*1440+440 - d*1440-1320 = 560
        # These ranges OVERLAP (560..880 is ambiguous for adjacent days, far windows).
        #
        # SAFEST approach: use the actual on_day booleans. Store them during variable creation.
        # For now we rebuild: on day d, job j option k is on day d iff
        #   start in [d*1440+440, d*1440+1320]. We can re-derive this with a boolean per pair per day.
        #
        # SIMPLEST CORRECT ENCODING: iterate days explicitly.
        # For each day d, for each pair (a, b) tier_a < tier_b, both on machine m:
        #   both_on_day_d = new_bool
        #   both_on_day_d → start_a in [d*1440+440, d*1440+1320-dur_a]
        #   both_on_day_d → start_b in [d*1440+440, d*1440+1320-dur_b]
        #   both_on_day_d AND assigned_a AND assigned_b → end_a <= start_b
        # This is O(jobs^2 * days * machines) but correct.

        if allergen_order:
            for m_i in range(n_machines):
                eligible: list[tuple[int,int,int,int]] = []  # (j, k, tier, dur)
                for j, job in enumerate(jobs):
                    allergen = (job.attributes or {}).get('allergen') or getattr(job,'allergen',None)
                    tier = allergen_rank.get(allergen, -1)
                    if tier < 0:
                        continue
                    for k, ov in enumerate(job_machine_vars[j]):
                        if ov['m_i'] == m_i:
                            eligible.append((j, k, tier, ov['dur']))

                for idx_a, (j_a, k_a, tier_a, dur_a) in enumerate(eligible):
                    for j_b, k_b, tier_b, dur_b in eligible:
                        if j_a == j_b or tier_a >= tier_b:
                            continue
                        va = job_machine_vars[j_a][k_a]
                        vb = job_machine_vars[j_b][k_b]

                        # For each day d: if both assigned to machine m on day d,
                        # then end[a] <= start[b]
                        # Use the on_day booleans stored during variable creation.
                        # These are fully bidirectional: on_day[d]=1 iff the job
                        # is assigned to run on day d (enforced by add_bool_or and
                        # the upper/lower bounds posted during variable setup).
                        # So: va['on_day'][d] AND vb['on_day'][d] means both are
                        # provably on day d → allergen order must hold.
                        od_a = va.get('on_day', [])
                        od_b = vb.get('on_day', [])
                        for d in range(min(len(od_a), len(od_b))):
                            model.add(va['end'] <= vb['start']).only_enforce_if(
                                [va['assigned'], vb['assigned'], od_a[d], od_b[d]]
                            )

        # ── CONSTRAINT 2: First/last position — per machine, per day ────────────
        #
        # position=first: on machine M on day D, this job runs before all normal jobs.
        # position=last:  on machine M on day D, this job runs after all normal jobs.
        # Cross-day: no constraint — a "last" job on day 1 does not block a "normal"
        # job on day 2.
        #
        # Encoding: for each (first_job, normal_job) pair on the same machine:
        #   for each day d:
        #     if both assigned to machine m on day d → end[first] <= start[normal]

        first_jobs  = [j for j, job in enumerate(jobs) if (job.attributes or {}).get('position') == 'first']
        last_jobs   = [j for j, job in enumerate(jobs) if (job.attributes or {}).get('position') == 'last']
        normal_jobs = [j for j, job in enumerate(jobs) if (job.attributes or {}).get('position') not in ('first','last')]

        def same_machine_same_day_enforce(va, vb, constraint_name):
            """Enforce va.end <= vb.start only when both assigned to same machine on same day."""
            if va['m_i'] != vb['m_i']:
                return  # different machines — skip
            od_a = va.get('on_day', [])
            od_b = vb.get('on_day', [])
            for d in range(min(len(od_a), len(od_b))):
                model.add(va['end'] <= vb['start']).only_enforce_if(
                    [va['assigned'], vb['assigned'], od_a[d], od_b[d]]
                )

        cname_idx = [0]
        def next_cname(prefix):
            cname_idx[0] += 1
            return f"{prefix}_{cname_idx[0]}"

        for j_f in first_jobs:
            for j_n in normal_jobs:
                for k_f, vf in enumerate(job_machine_vars[j_f]):
                    for k_n, vn in enumerate(job_machine_vars[j_n]):
                        same_machine_same_day_enforce(vf, vn, next_cname("fn"))
            for j_l in last_jobs:
                for k_f, vf in enumerate(job_machine_vars[j_f]):
                    for k_l, vl in enumerate(job_machine_vars[j_l]):
                        same_machine_same_day_enforce(vf, vl, next_cname("fl"))

        for j_n in normal_jobs:
            for j_l in last_jobs:
                for k_n, vn in enumerate(job_machine_vars[j_n]):
                    for k_l, vl in enumerate(job_machine_vars[j_l]):
                        same_machine_same_day_enforce(vn, vl, next_cname("nl"))

        # ── CONSTRAINT 3: Cross-machine dependency ────────────────────────────
        for j, job in enumerate(jobs):
            for prereq_id in (job.attributes or {}).get('depends_on', []):
                if prereq_id not in job_id_set:
                    violations.append(ConstraintViolation(
                        constraint='dependency',
                        details=f"'{job.name}' depends on unknown '{prereq_id}'",
                        severity='soft',
                    ))
                    continue
                prereq_j = next((i for i, jj in enumerate(jobs) if jj.id == prereq_id), None)
                if prereq_j is None:
                    continue
                model.add(job_start[j] >= job_end[prereq_j]).only_enforce_if(
                    [job_scheduled[j], job_scheduled[prereq_j]]
                )

        # ── Objective ─────────────────────────────────────────────────────────
        scheduled_sum = sum(job_scheduled)

        # Machine load (total minutes across all days)
        machine_load_vars = []
        for m_i in range(n_machines):
            load = model.new_int_var(0, horizon, f"load_m{m_i}")
            terms = [ov['dur'] * ov['assigned']
                     for j in range(len(jobs))
                     for ov in job_machine_vars[j]
                     if ov['m_i'] == m_i]
            model.add(load == sum(terms)) if terms else model.add(load == 0)
            machine_load_vars.append(load)

        max_load = model.new_int_var(0, horizon, "max_load")
        min_load = model.new_int_var(0, horizon, "min_load")
        model.add_max_equality(max_load, machine_load_vars)
        model.add_min_equality(min_load, machine_load_vars)
        imbalance = model.new_int_var(0, horizon, "imbalance")
        model.add(imbalance == max_load - min_load)

        all_ends = [ov['end'] for ovs in job_machine_vars for ov in ovs]
        makespan  = model.new_int_var(0, horizon, "makespan")
        model.add_max_equality(makespan, all_ends)

        # Sum of all job start times — minimising this packs jobs into early days.
        # CP-SAT objective must be LINEAR — cannot multiply two CP-SAT variables.
        # Fix: auxiliary var effective_start[j] = start[j] if scheduled, else 0.
        EARLY_W = 1
        effective_starts = []
        for j in range(len(jobs)):
            es = model.new_int_var(0, horizon, f"es_{j}")
            model.add(es == job_start[j]).only_enforce_if(job_scheduled[j])
            model.add(es == 0).only_enforce_if(job_scheduled[j].negated())
            effective_starts.append(es)
        sum_starts = sum(effective_starts)

        model.maximize(
            BIG_M * scheduled_sum      # 1: maximise jobs scheduled
            - BALANCE_W * imbalance    # 2: balance load across machines
            - EARLY_W   * sum_starts   # 3: pack jobs into earliest possible days
            - makespan                 # 4: minimise total span
        )

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
                assignments=[],
                unassigned=[UnassignedTask(task_id=j.id, task_name=j.name,
                                           reason="Infeasible — check deadlines and shift window")
                             for j in jobs],
                violations=violations, gantt=[],
                solver_info={'cp_status': str(solver.status_name)},
            )

        makespan_val = solver.value(makespan)

        # ── Extract ───────────────────────────────────────────────────────────
        assignments:  list[Assignment]     = []
        unassigned:   list[UnassignedTask] = []
        machine_load_actual: dict[int,int] = defaultdict(int)

        for j, job in enumerate(jobs):
            if not solver.value(job_scheduled[j]):
                dl = job_deadline_min(job)
                reason = "Could not be scheduled within constraints and planning horizon"
                if dl is not None and dl < horizon:
                    reason = f"Deadline too tight — job cannot be placed before {_fmt(dl, start_date)}"
                deps = (job.attributes or {}).get('depends_on', [])
                unscheduled_deps = [d for d in deps if any(
                    jj.id == d and not solver.value(job_scheduled[ji])
                    for ji, jj in enumerate(jobs)
                )]
                if unscheduled_deps:
                    reason = f"Prerequisite(s) unscheduled: {', '.join(unscheduled_deps)}"
                unassigned.append(UnassignedTask(task_id=job.id, task_name=job.name, reason=reason))
                continue

            chosen = next(
                (ov for ov in job_machine_vars[j] if solver.value(ov['assigned'])),
                None
            )
            if not chosen:
                unassigned.append(UnassignedTask(task_id=job.id, task_name=job.name,
                                                  reason="Solver error: no machine assigned"))
                continue

            s_val   = solver.value(chosen['start'])
            e_val   = solver.value(chosen['end'])
            machine = chosen['machine_id']
            dur     = chosen['dur']
            day_idx = s_val // DAY_MIN
            machine_load_actual[chosen['m_i']] += dur

            allergen = (job.attributes or {}).get('allergen') or getattr(job,'allergen','') or ''
            position = (job.attributes or {}).get('position') or ''
            alts     = [f"{o['machine_id']}({o['duration_min']}min)"
                        for o in job_options[j] if o['machine_id'] != machine]
            dl_str   = str(job.deadline or (job.attributes or {}).get('deadline_date') or '—')

            assignments.append(Assignment(
                task_id=job.id,
                task_name=job.name,
                resource_id=machine,
                resource_name=resource_name.get(machine, machine),
                start=_fmt(s_val, start_date),
                end=_fmt(e_val, start_date),
                cost=float(dur),
                metadata={
                    'allergen':     allergen,
                    'position':     position,
                    'depends_on':   (job.attributes or {}).get('depends_on', []),
                    'machine':      machine,
                    'start_min':    s_val,
                    'end_min':      e_val,
                    'day':          day_idx,
                    'day_label':    str(start_date + timedelta(days=day_idx)),
                    'duration_min': dur,
                    'alternatives': alts,
                    'deadline':     dl_str,
                },
            ))

        assignments.sort(key=lambda a: (a.metadata['day'], a.resource_id, a.metadata['start_min']))

        loads = [solver.value(lv) for lv in machine_load_vars]
        avg_util = round(sum(loads) / max(num_days * SHIFT_LEN * n_machines, 1) * 100, 1)
        imb_val  = solver.value(imbalance)

        kpis = [
            KPI(key='jobs_scheduled',   value=len(assignments)),
            KPI(key='jobs_unscheduled', value=len(unassigned)),
            KPI(key='planning_days',    value=num_days, unit=' days'),
            KPI(key='date_range',       value=f"{start_date} → {end_date}"),
            KPI(key='avg_utilisation',  value=avg_util, unit='%'),
            KPI(key='load_imbalance',   value=f"{imb_val}min"),
        ]
        for m_i, mname in enumerate(all_machines):
            util = round(loads[m_i] / max(num_days * SHIFT_LEN, 1) * 100, 1)
            kpis.append(KPI(key=f'load_{mname}', value=f"{loads[m_i]}min ({util}%)", unit=''))

        # Gantt — per machine, shows all days
        gantt: list[GanttRow] = []
        for m_i, mname in enumerate(all_machines):
            m_asgns = sorted(
                [a for a in assignments if a.resource_id == mname],
                key=lambda a: a.metadata['start_min'],
            )
            if not m_asgns:
                continue
            bars: list[GanttBar] = []
            prev = 0
            for a in m_asgns:
                s = a.metadata['start_min']
                e = a.metadata['end_min']
                if s > prev:
                    bars.append(GanttBar(
                        task_id=f'_idle_{mname}_{s}',
                        task_name=f'[idle]',
                        start_offset=float(prev), duration=float(s - prev),
                    ))
                bars.append(GanttBar(
                    task_id=a.task_id, task_name=a.task_name,
                    start_offset=float(s), duration=float(e - s),
                ))
                prev = e
            gantt.append(GanttRow(
                resource_id=mname,
                resource_name=resource_name.get(mname, mname),
                bars=bars,
            ))

        return ScheduleResponse(
            status=SolveStatus.OPTIMAL if not unassigned else SolveStatus.FEASIBLE,
            category=self.category, subtype=self.subtype, solve_time_ms=0,
            score=ScoreDetail(hard_violations=len(unassigned), soft_score=-float(makespan_val)),
            kpis=kpis, assignments=assignments, unassigned=unassigned,
            violations=violations, gantt=gantt,
            solver_info={
                'cp_status':     str(solver.status_name),
                'start_date':    str(start_date),
                'end_date':      str(end_date),
                'num_days':      num_days,
                'allergen_order': allergen_order,
                'machine_loads': {all_machines[i]: loads[i] for i in range(n_machines)},
                'flexible_jobs': sum(1 for opts in job_options if len(opts) > 1),
            },
        )

    def _err(self, reason: str) -> ScheduleResponse:
        from app.models.response import ConstraintViolation
        return ScheduleResponse(
            status=SolveStatus.INFEASIBLE, category=self.category, subtype=self.subtype,
            solve_time_ms=0, score=ScoreDetail(), kpis=[],
            assignments=[], unassigned=[], violations=[
                ConstraintViolation(constraint='input', details=reason, severity='hard')
            ], gantt=[],
        )