"""
Base solver — all four solvers inherit this.
Wraps _solve() with timing, structured logging, and error capture.
"""
from __future__ import annotations
import time
import traceback
from abc import ABC, abstractmethod

from app.logger import get_logger
from app.models.request  import ScheduleRequest
from app.models.response import ScheduleResponse, SolveStatus, ScoreDetail, ConstraintViolation


class BaseSolver(ABC):

    def __init__(self, req: ScheduleRequest, category: str, subtype: str):
        self.req      = req
        self.category = category
        self.subtype  = subtype
        self._cfg     = req.solver_config
        self._log     = get_logger(f"solver.{category}")

    def run(self) -> ScheduleResponse:
        self._log.info(
            "START solve  category=%s  subtype=%s  resources=%d  tasks=%d  "
            "timeslots=%d  time_limit=%ds",
            self.category, self.subtype,
            len(self.req.resources), len(self.req.tasks),
            len(self.req.timeslots), self._cfg.time_limit_seconds,
        )
        t0 = time.perf_counter()
        try:
            result = self._solve()
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            tb = traceback.format_exc()
            self._log.error(
                "CRASH in %s/%s after %dms:\n%s",
                self.category, self.subtype, elapsed_ms, tb,
            )
            return ScheduleResponse(
                status=SolveStatus.ERROR,
                category=self.category,
                subtype=self.subtype,
                solve_time_ms=elapsed_ms,
                score=ScoreDetail(),
                kpis=[], assignments=[], unassigned=[],
                violations=[
                    ConstraintViolation(
                        constraint="solver_crash",
                        details=tb.strip().splitlines()[-1],  # last line of traceback
                        severity="hard",
                    )
                ],
                gantt=[],
                solver_info={"traceback": tb},
            )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        result.solve_time_ms = elapsed_ms

        self._log.info(
            "END   solve  category=%s  subtype=%s  status=%s  "
            "assigned=%d  unassigned=%d  solve_time=%dms",
            self.category, self.subtype, result.status,
            len(result.assignments), len(result.unassigned), elapsed_ms,
        )

        if result.status == SolveStatus.ERROR:
            self._log.error(
                "Solver returned ERROR  category=%s  subtype=%s  info=%s",
                self.category, self.subtype, result.solver_info,
            )
        elif result.unassigned:
            self._log.warning(
                "%d task(s) unassigned  category=%s  subtype=%s  reasons=%s",
                len(result.unassigned), self.category, self.subtype,
                [u.reason for u in result.unassigned],
            )

        return result

    @abstractmethod
    def _solve(self) -> ScheduleResponse:
        ...

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _is_enabled(constraints, ctype: str) -> bool:
        return any(c.type == ctype and c.enabled for c in constraints)

    @staticmethod
    def _weight(constraints, ctype: str, default: float = 1.0) -> float:
        for c in constraints:
            if c.type == ctype:
                return c.weight
        return default
