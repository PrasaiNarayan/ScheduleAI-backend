# ScheduleAI — Backend

FastAPI backend for **ScheduleAI**, a production scheduling system for food manufacturing. Uses Google OR-Tools CP-SAT solver to generate optimal, constraint-satisfying production schedules.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI |
| Language | Python 3.9+ |
| Solver | Google OR-Tools CP-SAT |
| Database | SQL Server 2022 |
| ORM / DB Driver | SQLAlchemy + pyodbc |
| Environment | Anaconda (env: `adeka`) |
| Certifications | AWS DEA-C01, AWS MLA-C01 |

---

## Features

### Scheduling Engine (CP-SAT Solver)
Constraints modelled:

| # | Constraint | Description |
|---|---|---|
| 1 | Fixed duration | Each step takes exactly its defined minutes |
| 2 | Shift window | Steps must start and end within machine working hours |
| 3 | No machine overlap | Two steps cannot share a machine simultaneously |
| 4 | Sequential steps | Mix → Bake → Pack must run in order |
| 5 | Cooling gaps | 30min after Bake, 15min after Fry, 60min for Cold Desserts |
| 6 | Allergen ordering | A before B before C on the same machine same day |
| 7 | Deadline | Last step must finish before order line deadline |
| 8 | First-only position | Product runs before all normal products on assigned machine |
| 9 | Last-only position | Product runs after all normal products on assigned machine |
| 10 | Hard locks | Exact time and machine preserved on replan |
| 11 | Soft locks | Same day and machine, time flexible on replan |

### API Endpoints
- `POST /api/plans` — create and solve a new plan
- `POST /api/plans/{id}/replan` — replan with locked assignments preserved
- `GET /api/plans/{id}/assignments` — get all assignments (by product, by machine)
- `PATCH /api/plans/{id}/lock/step` — lock a single step
- `PATCH /api/plans/{id}/lock/product/{product_id}` — lock all steps of a product
- `PATCH /api/plans/{id}/lock/dates` — lock all assignments in a date range
- `PATCH /api/plans/{id}/soft-lock/product/{product_id}` — soft lock (day+machine fixed, time flexible)
- `PATCH /api/plans/{id}/approve` — approve a plan
- `GET /api/products` — master product list
- `GET /api/order-lines` — open order lines with deadlines
- `GET /api/machines` — machine list with shift hours

### Database Schema
```
M_  prefix = Master tables (products, categories, allergens, machines)
T_  prefix = Transaction tables (orders, plans, assignments)
No prefix  = Operational tables (machines, holidays, shift overrides)

Key tables:
  M_Product           — products with allergen, priority, position (first/last/null)
  M_CategoryStep      — step definitions per category (Mix, Bake, Fry, Pack)
  T_Order             — customer orders
  T_OrderLine         — order line items with deadlines
  T_Plan              — production plans
  T_PlanAssignment    — solver output: step assignments with lock_scope
  T_PlanUnscheduled   — products that could not be scheduled
```

---

## Project Structure

```
scheduleai-v6/
├── app/
│   ├── main.py                    # FastAPI app, CORS, request logging
│   ├── db.py                      # Database layer (SQLAlchemy + pyodbc)
│   ├── logger.py                  # Structured logging setup
│   ├── routers/
│   │   ├── data.py                # Product, machine, order CRUD endpoints
│   │   ├── planning.py            # Plan creation, replan, locking endpoints
│   │   ├── health.py              # Health check endpoint
│   │   └── solve.py               # Generic solve endpoint
│   └── solvers/
│       └── multi_step.py          # CP-SAT solver — all constraints modelled here
└── requirements.txt
```

---

## Getting Started

### Prerequisites

- Python 3.9+ 
- SQL Server 2022 (Express or Developer edition is fine)
- ODBC Driver 17 for SQL Server
- Google OR-Tools (`pip install ortools`)

### Installation

```bash
git clone https://github.com/PrasaiNarayan/ScheduleAI-backend.git
cd ScheduleAI-backend

# Create conda environment
conda create -n adeka python=3.9
pip install -r requirements.txt
```


### Configuration

Create `app/config.py` (not committed — add your connection string):

```python
DB_CONNECTION_STRING = (
    "mssql+pyodbc://localhost/ScheduleAI"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
)
```

### Running

```bash
uvicorn app.main:app --reload --port 8000
```

API docs available at: `http://127.0.0.1:8000/docs`

---

## Solver Design

The solver (`app/solvers/multi_step.py`) uses **CP-SAT** from Google OR-Tools.

### Objective Function

```
Maximize:
  PRIORITY_W * Σ(priority × scheduled[p])   # primary: schedule all products
  - EARLY_W  * Σ(last_step_end[p])          # secondary: complete products early
```

Where `PRIORITY_W = 100,000` and `EARLY_W = 10` — scheduling completeness
always dominates over earliness.

### Lock Handling

On replan, locked assignments are read from `T_PlanAssignment`:

- **Hard locks** (`step`, `product`, `date`) → injected as fixed constant intervals into CP-SAT's `no_overlap` pool. Solver cannot schedule anything into those slots.
- **Soft locks** (`soft`) → day and machine are fixed per step; solver picks the best time within the shift window on that machine.

### Status Mapping

| CP-SAT Status | Meaning | Plan Status |
|---|---|---|
| OPTIMAL | Best solution, mathematically proven | `solved` |
| FEASIBLE | Good solution, time limit reached | `solved` or `partial` |
| INFEASIBLE | Constraints cannot be satisfied | `failed` |
| UNKNOWN | Time limit reached before any solution | `failed` |

---

## Environment

```
Python:    3.9+
OR-Tools:  9.x
FastAPI:   0.110+
SQLAlchemy: 2.x
pyodbc:    4.x
uvicorn:   0.29+
```

