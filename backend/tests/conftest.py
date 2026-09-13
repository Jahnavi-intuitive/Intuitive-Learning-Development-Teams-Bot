"""Fixtures shared by the test suite. There is no separate test database in
this project (see DEVELOPMENT_LOG.md's own account of local dev setup) —
every fixture here operates against the same real local Postgres the rest
of this project uses, so every test that writes anything must clean up
after itself (delete its own rows plus their audit_log entries), the same
discipline already used throughout this project's manual verification
passes."""

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Employee


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_employee_id(db):
    emp = db.scalar(select(Employee).where(Employee.is_admin.is_(True)))
    assert emp is not None, "No admin employee exists in the local dev DB — seed one before running this suite."
    return emp.employee_id


@pytest.fixture
def any_employee_id(db):
    emp = db.scalar(select(Employee).where(Employee.is_admin.is_(False)).limit(1))
    assert emp is not None, "No non-admin employee exists in the local dev DB — seed one before running this suite."
    return emp.employee_id
