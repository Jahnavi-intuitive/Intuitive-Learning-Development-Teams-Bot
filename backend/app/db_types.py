"""Column types that work on both PostgreSQL (local dev) and Databricks
(the live database). On Postgres each one keeps its native type exactly as
before; on Databricks — which has no UUID, JSONB, enum or vector types —
it is stored as plain text and converted back to the same Python value, so
model and router code never needs to know which database it's talking to.
"""

import json
import threading
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Integer, String, Text, event, func, select
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSON as PG_JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_pg(dialect) -> bool:
    return dialect.name == "postgresql"


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on both databases. Databricks stores
    TIMESTAMP in UTC but may hand values back without tzinfo; attaching UTC
    keeps comparisons with datetime.now(timezone.utc) working."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if _is_pg(dialect):
            return dialect.type_descriptor(DateTime(timezone=True))
        from databricks.sqlalchemy import TIMESTAMP

        return dialect.type_descriptor(TIMESTAMP())

    def process_bind_param(self, value, dialect):
        if value is None or _is_pg(dialect):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class GUID(TypeDecorator):
    """uuid.UUID in Python; native UUID on Postgres, 36-char text elsewhere."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(PGUUID(as_uuid=True) if _is_pg(dialect) else String(36))

    def process_bind_param(self, value, dialect):
        if value is None or _is_pg(dialect):
            return value
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONText(TypeDecorator):
    """dict/list in Python; JSONB (or JSON) on Postgres, JSON text elsewhere."""

    impl = Text
    cache_ok = True

    def __init__(self, binary: bool = True):
        super().__init__()
        self.binary = binary

    def load_dialect_impl(self, dialect):
        if _is_pg(dialect):
            return dialect.type_descriptor(JSONB() if self.binary else PG_JSON())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None or _is_pg(dialect):
            return value
        return json.dumps(value, default=str)

    def process_result_value(self, value, dialect):
        if value is None or not isinstance(value, str):
            return value
        return json.loads(value)


class EnumText(TypeDecorator):
    """A fixed set of string values; native enum type on Postgres, text elsewhere."""

    impl = String(64)
    cache_ok = True

    def __init__(self, *values, name: str):
        super().__init__()
        self.values = values
        self.name = name

    def load_dialect_impl(self, dialect):
        if _is_pg(dialect):
            return dialect.type_descriptor(PGEnum(*self.values, name=self.name, create_type=False))
        return dialect.type_descriptor(String(64))


class PyEnum(TypeDecorator):
    """A Python enum.Enum; native Postgres enum on Postgres, its .value as text elsewhere."""

    impl = String(64)
    cache_ok = True

    def __init__(self, enum_cls, name: str):
        super().__init__()
        self.enum_cls = enum_cls
        self.name = name

    def load_dialect_impl(self, dialect):
        if _is_pg(dialect):
            return dialect.type_descriptor(SAEnum(self.enum_cls, name=self.name))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value, dialect):
        if value is None or _is_pg(dialect):
            return value
        return self.enum_cls(value).value

    def process_result_value(self, value, dialect):
        if value is None or _is_pg(dialect):
            return value
        return self.enum_cls(value)


class Embedding(TypeDecorator):
    """list[float] in Python; pgvector on Postgres, JSON text elsewhere."""

    impl = Text
    cache_ok = True

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Vector(self.dim) if _is_pg(dialect) else Text())

    def process_bind_param(self, value, dialect):
        if value is None or _is_pg(dialect):
            return value
        return json.dumps([float(x) for x in value])

    def process_result_value(self, value, dialect):
        if value is None or not isinstance(value, str):
            return value
        return json.loads(value)


# Databricks tables have no auto-increment, so integer primary keys are
# assigned here before insert. The backend runs as a single process (one
# container replica), so an in-process counter seeded from max(id) is safe.
_id_lock = threading.Lock()
_next_ids: dict[str, int] = {}


def register_integer_id_assignment(base) -> None:
    @event.listens_for(base, "before_insert", propagate=True)
    def _assign_id(mapper, connection, target):
        if _is_pg(connection.dialect):
            return
        pk = mapper.primary_key
        if len(pk) != 1 or pk[0].name != "id" or not isinstance(pk[0].type, Integer):
            return
        if getattr(target, "id", None) is not None:
            return
        table = mapper.local_table
        with _id_lock:
            if table.name not in _next_ids:
                current = connection.execute(select(func.max(table.c.id))).scalar()
                _next_ids[table.name] = int(current or 0) + 1
            target.id = _next_ids[table.name]
            _next_ids[table.name] += 1
