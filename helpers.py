"""
Bon Home — Shared helper utilities.
Small pure functions used across multiple route modules.
"""
import os
import re
import logging
from datetime import datetime, timezone

from flask import jsonify, request

log = logging.getLogger('lou-app')


# ---------------------------------------------------------------------------
# Pydantic request models + validation
# ---------------------------------------------------------------------------
try:
    from pydantic import BaseModel, Field, field_validator, ValidationError

    _EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

    class SignupRequest(BaseModel):
        email: str
        password: str
        name: str = ''
        captcha_token: str = ''
        criteria: dict = {}

        @field_validator('email')
        @classmethod
        def _check_email(cls, v):
            v = (v or '').strip().lower()
            if not _EMAIL_RE.match(v):
                raise ValueError('Email invalide')
            return v

        @field_validator('password')
        @classmethod
        def _check_password(cls, v):
            if len(v or '') < 8:
                raise ValueError('Mot de passe trop court (8 car. min)')
            if len(v) > 200:
                raise ValueError('Mot de passe trop long')
            return v

        @field_validator('name')
        @classmethod
        def _trim_name(cls, v):
            return (v or '').strip()[:120]

    class LoginRequest(BaseModel):
        email: str
        password: str

        @field_validator('email')
        @classmethod
        def _lower_email(cls, v):
            return (v or '').strip().lower()

        @field_validator('password')
        @classmethod
        def _check_pw_len(cls, v):
            if not v or len(v) > 200:
                raise ValueError('Identifiants incorrects')
            return v

    class ChatRequest(BaseModel):
        message: str = Field(min_length=1, max_length=4000)
        conversation_id: str = ''
        existing_criteria: dict = {}

    class ZoneModel(BaseModel):
        city: str = ''
        canton: str = ''
        radius_km: float = 3.0
        latitude: float | None = None
        longitude: float | None = None

    class ProfileUpdateRequest(BaseModel):
        transaction: str | None = None
        property_types: list[str] | None = None
        budget_min: int | None = None
        budget_max: int | None = None
        surface_min: int | None = None
        surface_max: int | None = None
        rooms_min: float | None = None
        rooms_max: float | None = None
        priorities: list[str] | None = None
        zones: list[dict] | None = None
        alert_email_enabled: bool | None = None
        alert_email_frequency: str | None = None
        alert_min_score: int | None = None

        @field_validator('transaction')
        @classmethod
        def _check_tx(cls, v):
            if v is not None and v not in ('location', 'achat'):
                raise ValueError("transaction doit être 'location' ou 'achat'")
            return v

        @field_validator('alert_email_frequency')
        @classmethod
        def _check_freq(cls, v):
            if v is not None and v not in ('instant', 'daily', 'weekly'):
                raise ValueError("alert_email_frequency invalide")
            return v

    _HAS_PYDANTIC = True
except Exception as _pydantic_err:
    log.warning(f"pydantic unavailable, falling back to manual validation: {_pydantic_err}")
    _HAS_PYDANTIC = False
    ValidationError = Exception  # type: ignore
    SignupRequest = LoginRequest = ChatRequest = ProfileUpdateRequest = None  # type: ignore


def validate_json(model_cls):
    """Validate request.json against a Pydantic model. Returns (obj, None) on
    success, or (None, (json_response, status)) on failure."""
    data = request.json or {}
    if not _HAS_PYDANTIC:
        from types import SimpleNamespace
        return SimpleNamespace(**data), None
    try:
        return model_cls(**data), None
    except ValidationError as e:
        try:
            first = (e.errors() or [{}])[0]
            raw_msg = first.get('msg', 'Données invalides')
        except Exception:
            raw_msg = 'Données invalides'
        msg = re.sub(r'^Value error,\s*', '', str(raw_msg))
        return None, (jsonify({"error": msg}), 400)


# ---------------------------------------------------------------------------
# Small parsers / date helpers
# ---------------------------------------------------------------------------

def parse_budget(s):
    """Extract numeric budget from string like '2000-2500 CHF' or '2500'."""
    if not s:
        return None
    nums = re.findall(r'\d+', str(s).replace("'", "").replace(",", ""))
    if nums:
        return int(nums[-1])
    return None


def parse_rooms(s):
    """Extract room count from string like '3+' or '3.5'."""
    if not s:
        return None
    nums = re.findall(r'[\d.]+', str(s))
    if nums:
        return float(nums[0])
    return None


def days_since(dt):
    """Calculate days since a datetime, handling timezone-naive datetimes."""
    if not dt:
        return None
    try:
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return None
