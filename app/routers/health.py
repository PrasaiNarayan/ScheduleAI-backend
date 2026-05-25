from fastapi import APIRouter
router = APIRouter()

@router.get("/health", tags=["system"])
def health():
    return {"status": "ok", "service": "ScheduleAI"}

@router.get("/", tags=["system"])
def root():
    return {
        "service": "ScheduleAI — General Purpose Scheduling API",
        "docs": "/docs",
        "endpoints": {
            "POST /solve": "Submit a scheduling problem and receive an optimised schedule",
            "POST /classify": "Classify a problem without solving",
            "GET /health": "Health check",
        }
    }
