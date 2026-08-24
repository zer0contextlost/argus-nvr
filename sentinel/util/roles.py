"""
sentinel/util/roles.py — Role-based access control dependency factories.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from sentinel.util.auth import get_current_user


def require_role(*roles: str):
    """Return a FastAPI dependency that requires the caller to have one of the given roles."""
    async def _check(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role: {' or '.join(roles)}",
            )
        return user
    return _check


def require_admin():
    return require_role("admin")


def require_operator_or_admin():
    return require_role("admin", "operator")
