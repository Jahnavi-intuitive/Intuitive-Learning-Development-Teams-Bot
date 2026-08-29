"""Admin-gating for the L&D admin surface (certification drives, catalog
CRUD, knowledge-base CRUD).

`require_admin` builds on services/access_scope.py's `get_current_employee`
and checks one thing: is this caller an admin. Which check that means
depends on settings.auth_mode, exactly like get_current_employee itself:
- "header" (dev): the local `employees.is_admin` boolean.
- "entra" (production): the `LnD.Admin` app-role claim from the caller's
  verified Entra ID token (stashed onto the Employee instance by
  get_current_employee as `_entra_is_admin` — see that function). The
  local is_admin column is not consulted in this mode at all, so it can't
  become a stale, forgotten-about backdoor once real roles are live.
"""

from fastapi import Depends, HTTPException

from app.config import settings
from app.models import Employee
from app.services.access_scope import get_current_employee


def require_admin(employee: Employee = Depends(get_current_employee)) -> Employee:
    is_admin = getattr(employee, "_entra_is_admin", False) if settings.auth_mode == "entra" else employee.is_admin
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return employee
