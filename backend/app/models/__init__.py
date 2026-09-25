"""Model package — import all models so metadata is complete for Alembic."""
from app.models.base import Base
from app.models.email_token import EmailToken
from app.models.problem import Problem
from app.models.recovery_code import RecoveryCode
from app.models.refresh_token import RefreshToken
from app.models.solution import Solution
from app.models.submission import Submission
from app.models.test_case import TestCase
from app.models.user import User

__all__ = [
    "Base", "EmailToken", "Problem", "RecoveryCode", "RefreshToken", "Solution", "Submission",
    "TestCase", "User",
]
