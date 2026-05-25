"""
schemas.py — Pydantic models for the data management API
All request bodies and response shapes live here.
"""
from __future__ import annotations
from datetime import date
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# ══════════════════════════════════════════════════════════════════════════════
# ALLERGENS
# ══════════════════════════════════════════════════════════════════════════════

class AllergenConfig(BaseModel):
    id:            int
    allergen_code: str
    allergen_name: str
    sort_order:    int
    colour_hex:    str
    active:        bool


# ══════════════════════════════════════════════════════════════════════════════
# MACHINES
# ══════════════════════════════════════════════════════════════════════════════

class MachineBase(BaseModel):
    name:                str   = Field(..., min_length=1, max_length=100)
    default_shift_start: str   = Field("07:20", pattern=r"^\d{2}:\d{2}$")
    default_shift_end:   str   = Field("22:00", pattern=r"^\d{2}:\d{2}$")
    notes:               str   = ""

    @field_validator("default_shift_start", "default_shift_end")
    @classmethod
    def validate_time(cls, v: str) -> str:
        h, m = map(int, v.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("Invalid time — use HH:MM format (00:00–23:59)")
        return v


class MachineCreate(MachineBase):
    pass


class MachineUpdate(BaseModel):
    name:                Optional[str] = Field(None, min_length=1, max_length=100)
    default_shift_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    default_shift_end:   Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    active:              Optional[bool] = None
    notes:               Optional[str]  = None


class MachineOut(BaseModel):
    id:                  int
    name:                str
    default_shift_start: str
    default_shift_end:   str
    active:              bool
    notes:               Optional[str]


# ══════════════════════════════════════════════════════════════════════════════
# SHIFT OVERRIDES
# ══════════════════════════════════════════════════════════════════════════════

class ShiftOverrideCreate(BaseModel):
    override_date: str   = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift_start:   Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    shift_end:     Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    closed:        bool  = False
    reason:        str   = ""

    @model_validator(mode="after")
    def check_times_when_open(self) -> "ShiftOverrideCreate":
        if not self.closed:
            if not self.shift_start or not self.shift_end:
                raise ValueError("shift_start and shift_end are required when closed=false")
        return self


class ShiftOverrideOut(BaseModel):
    id:            int
    machine_id:    int
    machine_name:  str
    override_date: str
    shift_start:   Optional[str]
    shift_end:     Optional[str]
    closed:        bool
    reason:        Optional[str]


# ══════════════════════════════════════════════════════════════════════════════
# HOLIDAYS
# ══════════════════════════════════════════════════════════════════════════════

HOLIDAY_TYPES = {"public", "factory", "custom"}

class HolidayCreate(BaseModel):
    holiday_date: str  = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    name:         str  = Field(..., min_length=1, max_length=200)
    holiday_type: str  = "public"
    notes:        str  = ""

    @field_validator("holiday_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in HOLIDAY_TYPES:
            raise ValueError(f"holiday_type must be one of {HOLIDAY_TYPES}")
        return v


class HolidayOut(BaseModel):
    id:           int
    holiday_date: str
    name:         str
    holiday_type: str
    notes:        Optional[str]


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

class MachineOption(BaseModel):
    machine_name: str = Field(..., min_length=1)
    duration_min: int = Field(..., gt=0)


class ProductCreate(BaseModel):
    name:             str            = Field(..., min_length=1, max_length=200)
    allergen:         Optional[str]  = Field(None, pattern=r"^[A-F]$")
    duration_minutes: Optional[int]  = Field(None, gt=0)
    machine_name:     Optional[str]  = None   # single machine
    machine_options:  Optional[list[MachineOption]] = None   # flexible
    position_slot:    Optional[str]  = None
    priority:         int            = Field(5, ge=1, le=10)
    deadline:         Optional[str]  = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    depends_on:       list[int]      = []     # list of product IDs
    notes:            str            = ""

    @field_validator("position_slot")
    @classmethod
    def validate_position(cls, v):
        if v is not None and v not in ("first", "last"):
            raise ValueError("position_slot must be 'first', 'last', or null")
        return v

    @model_validator(mode="after")
    def check_machine_defined(self) -> "ProductCreate":
        if not self.machine_name and not self.machine_options:
            raise ValueError("Either machine_name or machine_options must be provided")
        if self.machine_name and not self.duration_minutes:
            raise ValueError("duration_minutes is required when using a single machine_name")
        return self


class ProductUpdate(BaseModel):
    name:             Optional[str]             = Field(None, min_length=1)
    allergen:         Optional[str]             = Field(None, pattern=r"^[A-F]$")
    duration_minutes: Optional[int]             = Field(None, gt=0)
    machine_name:     Optional[str]             = None
    machine_options:  Optional[list[MachineOption]] = None
    position_slot:    Optional[str]             = None
    priority:         Optional[int]             = Field(None, ge=1, le=10)
    deadline:         Optional[str]             = None
    depends_on:       Optional[list[int]]       = None
    notes:            Optional[str]             = None
    active:           Optional[bool]            = None


class ProductOut(BaseModel):
    id:               int
    name:             str
    allergen:         Optional[str]
    duration_minutes: Optional[int]
    machine_id:       Optional[int]
    machine_name:     Optional[str]
    machine_options:  Optional[list[dict]]
    position_slot:    Optional[str]
    priority:         int
    deadline:         Optional[str]
    depends_on:       list[Any]
    notes:            Optional[str]
    active:           bool


# ══════════════════════════════════════════════════════════════════════════════
# CSV IMPORT
# ══════════════════════════════════════════════════════════════════════════════

class CsvImportPreviewRow(BaseModel):
    row_number:   int
    name:         str
    allergen:     Optional[str]
    duration_min: Optional[int]
    machine:      Optional[str]
    deadline:     Optional[str]
    priority:     int
    valid:        bool
    errors:       list[str]


class CsvImportPreview(BaseModel):
    total_rows:   int
    valid_rows:   int
    invalid_rows: int
    rows:         list[CsvImportPreviewRow]


class CsvImportResult(BaseModel):
    inserted: int
    updated:  int
    errors:   list[dict]


# ══════════════════════════════════════════════════════════════════════════════
# GENERIC
# ══════════════════════════════════════════════════════════════════════════════

class MessageOut(BaseModel):
    message: str
    id:      Optional[int] = None


class WorkingDayOut(BaseModel):
    work_date:    str
    day_name:     str
    is_holiday:   bool
    holiday_name: str