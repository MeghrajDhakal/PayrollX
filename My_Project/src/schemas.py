"""
PayrollX — Strict Input Validation Schemas (Pydantic v2)
Enforces types, string lengths, numeric ranges, formats, and rejects unexpected fields.
"""
import re
from datetime import date
from typing import Optional, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, EmailStr

# Regex patterns for Indian statutory & identity formats
PAN_REGEX = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$')
AADHAAR_REGEX = re.compile(r'^[0-9]{12}$')
IFSC_REGEX = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
PHONE_REGEX = re.compile(r'^\+?[0-9]{10,15}$')
BANK_ACCOUNT_REGEX = re.compile(r'^[0-9]{9,30}$')
NAME_REGEX = re.compile(r'^[A-Za-z\s\.\'-]{1,80}$')


class StrictBaseSchema(BaseModel):
    """Base schema that strictly forbids extra/unexpected fields."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class LoginSchema(StrictBaseSchema):
    login_mode: Literal['admin', 'employee'] = 'admin'
    username: Optional[str] = Field(None, max_length=80)
    employee_id: Optional[str] = Field(None, max_length=80)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator('password')
    @classmethod
    def validate_password_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Password cannot be blank")
        return v


class RegisterSchema(StrictBaseSchema):
    username: str = Field(..., min_length=3, max_length=80)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    organization: str = Field(..., min_length=2, max_length=200)

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError("Username can only contain alphanumeric characters, underscores, and hyphens.")
        return v


class EmployeeCreateSchema(StrictBaseSchema):
    first_name: str = Field(..., min_length=1, max_length=80)
    last_name: str = Field(..., min_length=1, max_length=80)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=20)
    pan_number: Optional[str] = Field(None, max_length=10)
    aadhaar_number: Optional[str] = Field(None, max_length=12)
    bank_account: Optional[str] = Field(None, max_length=30)
    bank_ifsc: Optional[str] = Field(None, max_length=11)
    bank_name: Optional[str] = Field(None, max_length=100)
    department: str = Field('General', min_length=1, max_length=80)
    designation: str = Field('Staff', min_length=1, max_length=80)
    ctc: float = Field(..., gt=0, le=1000000000, description="Annual CTC must be positive and reasonable")
    idempotency_key: Optional[str] = Field(None, max_length=128)

    @field_validator('first_name', 'last_name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not NAME_REGEX.match(v):
            raise ValueError("Name can only contain letters, spaces, hyphens, and periods.")
        return v

    @field_validator('pan_number')
    @classmethod
    def validate_pan(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v_upper = v.upper()
            if not PAN_REGEX.match(v_upper):
                raise ValueError("Invalid PAN format. Must be 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).")
            return v_upper
        return v

    @field_validator('aadhaar_number')
    @classmethod
    def validate_aadhaar(cls, v: Optional[str]) -> Optional[str]:
        if v and not AADHAAR_REGEX.match(v):
            raise ValueError("Invalid Aadhaar format. Must be exactly 12 digits.")
        return v

    @field_validator('bank_ifsc')
    @classmethod
    def validate_ifsc(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v_upper = v.upper()
            if not IFSC_REGEX.match(v_upper):
                raise ValueError("Invalid IFSC format. Must be 4 letters, 0, and 6 alphanumeric characters.")
            return v_upper
        return v

    @field_validator('bank_account')
    @classmethod
    def validate_account(cls, v: Optional[str]) -> Optional[str]:
        if v and not BANK_ACCOUNT_REGEX.match(v):
            raise ValueError("Invalid bank account format. Must be between 9 and 30 digits.")
        return v

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v and not PHONE_REGEX.match(v):
            raise ValueError("Invalid phone format.")
        return v


class RunPayrollSchema(StrictBaseSchema):
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2000, le=2100)
    idempotency_key: Optional[str] = Field(None, max_length=128)


class AttendanceUpdateSchema(StrictBaseSchema):
    emp_id: int = Field(..., gt=0)
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2000, le=2100)
    total_working_days: int = Field(22, ge=0, le=31)
    days_present: int = Field(22, ge=0, le=31)
    days_absent: int = Field(0, ge=0, le=31)
    lop_days: int = Field(0, ge=0, le=31)
    overtime_hours: float = Field(0.0, ge=0.0, le=300.0)

    @field_validator('days_present')
    @classmethod
    def validate_present_vs_total(cls, v: int, info) -> int:
        total = info.data.get('total_working_days')
        if total is not None and v > total:
            raise ValueError("Days present cannot exceed total working days.")
        return v


class ReimbursementCreateSchema(StrictBaseSchema):
    category: Literal['travel', 'medical', 'food', 'other']
    amount: float = Field(..., gt=0, le=10000000)
    description: str = Field(..., min_length=1, max_length=300)
    receipt_date: date


class TaxDeclarationSchema(StrictBaseSchema):
    financial_year: str = Field(..., max_length=9)
    regime: Literal['new', 'old'] = 'new'
    section_80c: float = Field(0.0, ge=0.0, le=150000.0)
    section_80d: float = Field(0.0, ge=0.0, le=100000.0)
    section_80e: float = Field(0.0, ge=0.0)
    section_80g: float = Field(0.0, ge=0.0)
    hra_exemption: float = Field(0.0, ge=0.0)
    lta_claimed: float = Field(0.0, ge=0.0)
    other_income: float = Field(0.0, ge=0.0)
