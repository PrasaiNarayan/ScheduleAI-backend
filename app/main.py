"""
ScheduleAI — FastAPI main application
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import time

from app.logger  import get_logger
from app.routers import health, solve
from app.routers.data     import router as data_router
from app.routers.planning import router as planning_router

log = get_logger(__name__)

app = FastAPI(
    title="ScheduleAI",
    description=(
        "General-purpose scheduling API. Auto-detects problem type "
        "(workforce, job shop, routing, timetabling) and solves with OR-Tools CP-SAT."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Lightweight request logging (no body streaming — avoids anyio conflict) ──
@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    log.info("→ %s %s", request.method, request.url.path)
    response = await call_next(request)
    ms = int((time.perf_counter() - t0) * 1000)
    level = "error" if response.status_code >= 500 else "info"
    getattr(log, level)(
        "← %s %s  status=%s  %dms",
        request.method, request.url.path, response.status_code, ms,
    )
    return response

app.include_router(health.router)
app.include_router(solve.router)
app.include_router(data_router,     prefix="/api")
app.include_router(planning_router, prefix="/api")

log.info("ScheduleAI API ready.")