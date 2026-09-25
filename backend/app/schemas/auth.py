"""Pydantic schemas for auth — the typed request/response contracts.

Schemas are the app's boundary: FastAPI validates every incoming body against the
`*Request` models (a bad payload becomes an automatic 422, so handlers only ever
see valid data) and serializes responses through the `*Out`/`*Response` models
(so we return exactly the fields we mean to — a model can't accidentally leak
`password_hash`). `EmailStr` checks address shape; `Field(min_length=…)` enforces
basic input rules declaratively.
"""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """Signup payload. Username/password bounds are enforced here, pre-handler."""

    email: EmailStr
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    remember: bool = True  # persistent cookie (default) vs. session-only


class UserOut(BaseModel):
    """The safe public view of a user — note there's no password field of any kind.

    `from_attributes=True` lets `model_validate(user)` read straight off the ORM
    object's attributes, so a route can return the `User` and get exactly these
    fields serialized (an allowlist, not the whole row).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    username: str
    email_verified: bool
    totp_enabled: bool = False  # two-factor auth is on (Settings shows manage vs enable)


class LoginResponse(BaseModel):
    """Login result: the access token (kept in memory) + the user. The refresh
    token isn't here — it's set as an httpOnly cookie the JS never sees."""

    access_token: str
    user: UserOut


class MfaChallenge(BaseModel):
    """Login result when the account has two-factor auth on: the password was right, but no
    session exists yet. Redeem `mfa_token` with a code at `/auth/login/2fa`."""

    mfa_required: Literal[True] = True
    mfa_token: str


class Login2FARequest(BaseModel):
    """Second login step. `code` is a 6-digit authenticator code or a recovery code."""

    mfa_token: str
    code: str = Field(min_length=6, max_length=32)
    remember: bool = True


class TotpSetupResponse(BaseModel):
    secret: str  # base32, for manual entry when the QR code can't be scanned
    otpauth_uri: str  # what the QR code encodes


class TotpCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class TotpDisableRequest(BaseModel):
    password: str
    code: str = Field(min_length=6, max_length=32)


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]  # plaintext, shown once


class AccessTokenResponse(BaseModel):
    """What /auth/refresh returns — just a new access token (cookie set separately)."""

    access_token: str


class VerifyEmailRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=200)


class ChangePasswordRequest(BaseModel):
    """Authenticated password change: prove the current password, then set a new one."""

    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class MessageResponse(BaseModel):
    message: str
