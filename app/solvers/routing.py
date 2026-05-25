"""
Vehicle Routing Solver — OR-Tools Routing Library (robust rewrite)
Supports: vrp | vrptw | tsp
"""
from __future__ import annotations
import math
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from app.models.response import (
    Assignment, GanttBar, GanttRow, KPI, ScheduleResponse,
    ScoreDetail, SolveStatus, UnassignedTask, ConstraintViolation,
)
from app.solvers.base import BaseSolver


def _to_min(t) -> int | None:
    if not t:
        return None
    s = str(t).strip()
    if "T" in s:
        s = s.split("T")[-1]
    elif " " in s and len(s) > 8:
        s = s.rsplit(" ", 1)[-1]
    parts = s.split(":")
    try:
        result = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
        return result if result >= 0 else None
    except (ValueError, IndexError):
        return None


def _haversine_min(a: dict, b: dict, speed_kmh: float = 40) -> int:
    R = 6371
    dlat = math.radians(b["lat"] - a["lat"])
    dlng = math.radians(b["lng"] - a["lng"])
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a["lat"]))
         * math.cos(math.radians(b["lat"]))
         * math.sin(dlng / 2) ** 2)
    dist_km = 2 * R * math.asin(math.sqrt(h))
    return max(1, int(dist_km / speed_kmh * 60))


# ── OR-Tools API compatibility shim ──────────────────────────────────────────
# Different OR-Tools versions use different casing for these methods.
def _index_to_node(manager, idx: int) -> int:
    """Works with both index_to_node and IndexToNode."""
    fn = getattr(manager, "IndexToNode", None) or getattr(manager, "index_to_node", None)
    if fn is None:
        raise AttributeError("RoutingIndexManager has no IndexToNode / index_to_node method")
    return fn(idx)


def _node_to_index(manager, node: int) -> int:
    fn = getattr(manager, "NodeToIndex", None) or getattr(manager, "node_to_index", None)
    if fn is None:
        raise AttributeError("RoutingIndexManager has no NodeToIndex / node_to_index method")
    return fn(node)


class RoutingSolver(BaseSolver):

    def _solve(self) -> ScheduleResponse:
        req  = self.req
        cons = req.constraints
        cfg  = self._cfg

        tasks = req.tasks
        if not tasks:
            return self._err("No delivery locations provided.")

        n_vehicles = req.num_vehicles or sum(
            1 for r in req.resources if r.type == "vehicle"
        ) or (1 if self.subtype == "tsp" else 3)

        capacity = req.vehicle_capacity or next(
            (int(r.attributes.get("capacity", 9999))
             for r in req.resources if r.type == "vehicle"),
            9999,
        )

        n_nodes = len(tasks) + 1  # index 0 = depot

        def travel(i: int, j: int) -> int:
            if i == j:
                return 0
            if i == 0 or j == 0:
                t = tasks[j - 1] if i == 0 else tasks[i - 1]
                return int(t.attributes.get("travel_time_min", 15))
            ta, tb = tasks[i - 1], tasks[j - 1]
            if ta.location and tb.location:
                return _haversine_min(ta.location, tb.location)
            return abs(
                int(ta.attributes.get("travel_time_min", 15)) -
                int(tb.attributes.get("travel_time_min", 15))
            ) + 5

        dist_matrix = [[travel(i, j) for j in range(n_nodes)] for i in range(n_nodes)]

        # ── Build routing model ───────────────────────────────────────────────
        manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, 0)
        routing = pywrapcp.RoutingModel(manager)

        def dist_callback(from_idx, to_idx):
            return dist_matrix[_index_to_node(manager, from_idx)][_index_to_node(manager, to_idx)]

        transit_cb = routing.RegisterTransitCallback(dist_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

        # ── Capacity dimension ────────────────────────────────────────────────
        has_demand = any(t.demand and t.demand > 0 for t in tasks)
        if self._is_enabled(cons, "capacity") and has_demand:
            def demand_callback(from_idx):
                node = _index_to_node(manager, from_idx)
                return int(tasks[node - 1].demand) if node > 0 else 0

            demand_cb = routing.RegisterUnaryTransitCallback(demand_callback)
            routing.AddDimensionWithVehicleCapacity(
                demand_cb,
                0,                          # no slack
                [capacity] * n_vehicles,    # per-vehicle capacity
                True,                       # fix start cumul to zero
                "Capacity",
            )

        # ── Time window dimension ─────────────────────────────────────────────
        has_tw = any(t.time_window_open for t in tasks)
        if self._is_enabled(cons, "time_window") and has_tw:
            routing.AddDimension(transit_cb, 60, 24 * 60, False, "Time")
            time_dim = routing.GetDimensionOrDie("Time")
            for idx, task in enumerate(tasks):
                node  = idx + 1
                ridx  = _node_to_index(manager, node)
                tw_o  = _to_min(task.time_window_open)  or 0
                tw_c  = _to_min(task.time_window_close) or 24 * 60
                time_dim.CumulVar(ridx).SetRange(tw_o, tw_c)

        # ── Allow dropping nodes ──────────────────────────────────────────────
        if not self._is_enabled(cons, "all_visited"):
            penalty = 100_000
            for node in range(1, n_nodes):
                routing.AddDisjunction([_node_to_index(manager, node)], penalty)

        # ── Search parameters ─────────────────────────────────────────────────
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        params.time_limit.seconds = cfg.time_limit_seconds

        solution = routing.SolveWithParameters(params)

        if not solution:
            return ScheduleResponse(
                status=SolveStatus.INFEASIBLE,
                category=self.category, subtype=self.subtype,
                solve_time_ms=0, score=ScoreDetail(), kpis=[],
                assignments=[],
                unassigned=[
                    UnassignedTask(task_id=t.id, task_name=t.name,
                                   reason="No feasible route found")
                    for t in tasks
                ],
                violations=[], gantt=[],
                solver_info={"status": "no_solution"},
            )

        # ── Extract routes ────────────────────────────────────────────────────
        vehicle_resources = [r for r in req.resources if r.type == "vehicle"]

        def vname(v: int) -> str:
            if v < len(vehicle_resources):
                return vehicle_resources[v].name
            return f"Vehicle {v + 1}"

        def vid(v: int) -> str:
            if v < len(vehicle_resources):
                return vehicle_resources[v].id
            return f"vehicle-{v + 1}"

        assignments:  list[Assignment]     = []
        unassigned:   list[UnassignedTask] = []
        gantt:        list[GanttRow]       = []
        served_nodes: set[int]             = set()
        vehicles_used = 0
        total_travel  = int(solution.ObjectiveValue())

        for v in range(n_vehicles):
            idx      = routing.Start(v)
            cum_time = 0
            bars:    list[GanttBar] = []
            stop_order = 0

            while not routing.IsEnd(idx):
                node = _index_to_node(manager, idx)
                next_idx = solution.Value(routing.NextVar(idx))

                if node > 0:
                    task     = tasks[node - 1]
                    travel_t = dist_matrix[node][_index_to_node(manager, next_idx)] \
                               if not routing.IsEnd(next_idx) else 0
                    stop_order += 1
                    cum_time   += int(dist_matrix[
                        _index_to_node(manager, routing.Start(v)) if stop_order == 1 else node
                    ][node]) if stop_order == 1 else travel_t

                    start_str = f"{cum_time // 60:02d}:{cum_time % 60:02d}"
                    end_str   = f"{(cum_time + 10) // 60:02d}:{(cum_time + 10) % 60:02d}"

                    assignments.append(Assignment(
                        task_id=task.id,
                        task_name=task.name,
                        resource_id=vid(v),
                        resource_name=vname(v),
                        start=start_str,
                        end=end_str,
                        cost=float(task.demand or 0),
                        metadata={"stop_order": stop_order},
                    ))
                    bars.append(GanttBar(
                        task_id=task.id, task_name=task.name,
                        start_offset=float(cum_time), duration=10.0,
                    ))
                    served_nodes.add(node)
                    cum_time += travel_t

                idx = next_idx

            if bars:
                vehicles_used += 1
                gantt.append(GanttRow(
                    resource_id=vid(v), resource_name=vname(v), bars=bars,
                ))

        for i, task in enumerate(tasks):
            if (i + 1) not in served_nodes:
                unassigned.append(UnassignedTask(
                    task_id=task.id, task_name=task.name,
                    reason="Not included in any route",
                ))

        kpis = [
            KPI(key="stops_served",   value=len(served_nodes)),
            KPI(key="unserved",       value=len(unassigned)),
            KPI(key="vehicles_used",  value=vehicles_used),
            KPI(key="total_travel",   value=total_travel, unit="min"),
        ]

        return ScheduleResponse(
            status=SolveStatus.OPTIMAL if not unassigned else SolveStatus.FEASIBLE,
            category=self.category, subtype=self.subtype,
            solve_time_ms=0,
            score=ScoreDetail(soft_score=-float(total_travel)),
            kpis=kpis, assignments=assignments, unassigned=unassigned,
            violations=[], gantt=gantt,
            solver_info={"objective_travel_min": total_travel},
        )

    def _err(self, reason: str) -> ScheduleResponse:
        return ScheduleResponse(
            status=SolveStatus.INFEASIBLE,
            category=self.category, subtype=self.subtype,
            solve_time_ms=0, score=ScoreDetail(), kpis=[],
            assignments=[], unassigned=[], violations=[
                ConstraintViolation(constraint="input", details=reason, severity="hard")
            ], gantt=[],
        )
