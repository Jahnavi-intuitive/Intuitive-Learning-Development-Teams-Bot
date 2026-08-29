"""Server-side access-scope resolution (BR-A-03, BR-D-24, Design Principle 5).

Two auth modes, switched by settings.auth_mode (app/config.py) — never by
anything a caller sends:
- "header" (local dev default): the caller is identified by the
  `X-Employee-Id` header, sent by the test UI/admin dashboard and by the
  agent's tool calls, trusted at face value. This is a stand-in for "the
  authenticated user's subject claim", not a real credential.
- "entra" (production): a real Microsoft Entra ID JWT (Authorization:
  Bearer), verified in services/entra_auth.py (signature, issuer,
  audience, expiry), matched to an employees row by email. `X-Employee-Id`
  alone is never trusted in this mode. app/main.py's startup guard refuses
  to boot with auth_mode="entra" unless entra_tenant_id/entra_audience
  are set.

  One narrow exception, for one specific internal hop: the agent
  (adk_agent/tools.py) calls back into this same backend to execute a
  tool, on behalf of whichever employee it's already serving — it has no
  Entra user token to present (it's a separate service, not a browser).
  For that hop only, a request carrying BOTH a valid Google-signed ID
  token from a trusted service account (X-Internal-Service-Token — see
  entra_auth.validate_service_id_token, which fails closed if no service
  accounts are allow-listed) AND an X-Employee-Id header is trusted to
  act as that employee — never as an admin (see admin_auth.py). This is
  not a backdoor into the X-Employee-Id header in general: without a
  valid token from an allow-listed service account, the header is still
  ignored entirely in "entra" mode.

Scope itself (who a given employee is allowed to see) is always computed
here, server-side, from the employees table's manager/practice_director
relationships. It is never taken from a request body, query param, or the
agent's prompt (Design Principle 5) — a caller cannot ask to see someone
else's records by passing a different employee_id; they can only query
whatever `resolve_visible_employee_ids` already permits for them.
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Employee
from app.services.entra_auth import validate_bearer_token, validate_service_id_token


def get_current_employee(
    x_employee_id: str | None = Header(default=None, alias="X-Employee-Id"),
    x_internal_service_token: str | None = Header(default=None, alias="X-Internal-Service-Token"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Employee:
    if settings.auth_mode == "entra":
        if x_internal_service_token and x_employee_id:
            validate_service_id_token(x_internal_service_token)  # raises if not on the allow-list
            employee = db.scalar(select(Employee).where(Employee.employee_id == x_employee_id))
            if not employee:
                raise HTTPException(status_code=401, detail=f"Unknown employee_id: {x_employee_id}")
            employee._entra_is_admin = False  # relay path never grants admin — see admin_auth.py
            return employee

        identity = validate_bearer_token(authorization)
        employee = db.scalar(select(Employee).where(Employee.email == identity.email))
        if not employee:
            raise HTTPException(status_code=401, detail=f"No employee record for {identity.email}")
        # Transient, request-scoped only — never persisted. admin_auth.py
        # reads this instead of re-validating the token or trusting a
        # client-supplied flag; it disappears when the ORM instance does.
        employee._entra_is_admin = identity.is_admin
        return employee

    if not x_employee_id:
        raise HTTPException(status_code=401, detail="Missing X-Employee-Id (dev auth stand-in)")
    employee = db.scalar(select(Employee).where(Employee.employee_id == x_employee_id))
    if not employee:
        raise HTTPException(status_code=401, detail=f"Unknown employee_id: {x_employee_id}")
    return employee


@dataclass
class Scope:
    requester: Employee
    visible_employee_ids: set[str]  # employee_id values the requester may query
    is_practice_director: bool


def resolve_scope(db: Session, requester: Employee) -> Scope:
    visible = {requester.employee_id}

    direct_reports = db.scalars(
        select(Employee.employee_id).where(Employee.manager_name == requester.name)
    ).all()
    visible.update(direct_reports)

    led_practice = db.scalars(
        select(Employee.employee_id).where(Employee.practice_director_name == requester.name)
    ).all()
    visible.update(led_practice)

    return Scope(
        requester=requester,
        visible_employee_ids=visible,
        is_practice_director=len(led_practice) > 0,
    )


def require_visible(scope: Scope, target_employee_id: str) -> None:
    if target_employee_id not in scope.visible_employee_ids:
        raise HTTPException(
            status_code=403,
            detail="Not permitted to view records for this employee.",
        )
