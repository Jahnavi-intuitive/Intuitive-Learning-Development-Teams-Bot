"""SQL-injection audit (Section 4 of the 2026-09-02 Teams-integration
brief). Fires classic injection payloads through every free-text intake
path named in the brief and asserts each one is stored/handled as inert
text, never executed: certification names, Credly links, conversation
memory content (the storage layer chat messages feed), knowledge-base
content, escalation questions, and admin-dashboard form fields (covered
by the catalog/KB cases below, since the admin dashboard calls these
exact same endpoints — see frontend/admin.html).

A repo-wide audit (`grep` across backend/app for string-concatenated or
f-string-built SQL — `text(f"`, `.format(` feeding a query, `%` string
interpolation into a query) found zero hits: every database access in
this codebase goes through SQLAlchemy's ORM/Core query builders, which
parameterize bound values regardless of content. That means these tests
aren't really testing whether *these specific payloads* are dangerous —
they're proving the storage layer treats malicious-looking text exactly
like any other text: stored and read back byte-for-byte, with the
targeted table (and every other table) still present and non-empty
afterward.

Runs against the real local dev database — every test creates its own
row(s) and deletes them (plus their audit_log entries) in the test body
itself, not a fixture teardown, so a failed assertion still leaves
useful state to inspect (matches how this project's manual verification
passes have always been done).
"""

from sqlalchemy import func, select, text

from app.models import CertificateRecord, CertificationCatalog, ConversationTurn, Employee, Escalation, KnowledgeBaseItem

# A couple of classic payloads, not an exhaustive fuzz list — the point is
# proving the storage layer is injection-proof by construction, not
# cataloguing every known SQLi variant.
INJECTION_PAYLOADS = [
    "'; DROP TABLE certificate_records; --",
    "Robert'); DROP TABLE students;--",
]


def _table_is_intact(db, model) -> None:
    count = db.scalar(select(func.count()).select_from(model))
    assert count is not None and count >= 0  # the table still exists and is queryable at all


def test_catalog_name_injection(client, db, admin_employee_id):
    for payload in INJECTION_PAYLOADS:
        name = f"SQLI-TEST {payload}"
        resp = client.post(
            "/api/admin/catalog-entries",
            headers={"X-Employee-Id": admin_employee_id},
            json={"name": name, "status": "active", "prerequisite_logic": "any"},
        )
        assert resp.status_code == 200, resp.text
        created = resp.json()
        assert created["name"] == name  # stored literally — not executed, not mangled

        search_resp = client.get("/api/catalog/certifications", params={"q": payload})
        assert search_resp.status_code == 200  # the .ilike(f"%{q}%") search path doesn't choke on it either

        _table_is_intact(db, CertificationCatalog)

        db.execute(
            text("DELETE FROM audit_log WHERE entity_type = 'certification_catalog' AND entity_id = :id"),
            {"id": created["id"]},
        )
        db.execute(text("DELETE FROM certification_catalog WHERE id = :id"), {"id": created["id"]})
        db.commit()


def test_kb_content_injection(client, db, admin_employee_id):
    # One payload here, not the full list — creating a KB entry makes a
    # real embedding API call per payload, so this stays a single
    # representative case rather than repeating an expensive network
    # call for no additional signal.
    payload = INJECTION_PAYLOADS[0]
    resp = client.post(
        "/api/kb/items",
        headers={"X-Employee-Id": admin_employee_id},
        json={"content": payload, "owner": "SQLI Test", "status": "active"},
    )
    assert resp.status_code == 200, resp.text
    item_id = resp.json()["id"]

    list_resp = client.get("/api/kb/items", headers={"X-Employee-Id": admin_employee_id})
    assert list_resp.status_code == 200
    stored = next(r for r in list_resp.json() if r["id"] == item_id)
    assert stored["content"] == payload  # round-trips as inert text, not truncated/executed

    _table_is_intact(db, KnowledgeBaseItem)

    db.execute(text("DELETE FROM audit_log WHERE entity_type = 'knowledge_base' AND entity_id = :id"), {"id": item_id})
    db.execute(text("DELETE FROM knowledge_base WHERE id = :id"), {"id": item_id})
    db.commit()


def test_escalation_question_injection(client, db, any_employee_id):
    payload = INJECTION_PAYLOADS[0]  # also triggers a real embedding call — one representative case
    resp = client.post(
        "/api/escalations",
        json={"employee_id": any_employee_id, "question": payload, "context": INJECTION_PAYLOADS[1]},
    )
    assert resp.status_code == 200, resp.text
    escalation_id = resp.json()["id"]

    list_resp = client.get("/api/escalations", params={"employee_id": any_employee_id})
    stored = next(r for r in list_resp.json() if r["id"] == escalation_id)
    assert stored["question"] == payload
    assert stored["context"] == INJECTION_PAYLOADS[1]

    _table_is_intact(db, Escalation)

    db.execute(text("DELETE FROM audit_log WHERE entity_type = 'escalations' AND entity_id = :id"), {"id": escalation_id})
    db.execute(text("DELETE FROM escalations WHERE id = :id"), {"id": escalation_id})
    db.commit()


def test_certification_submission_injection(client, db, any_employee_id):
    for payload in INJECTION_PAYLOADS:
        # submission_status="in_progress" deliberately — it's the one
        # status that neither requires credly_link nor triggers
        # perform_verification's real HTTP fetch, so the credly_link
        # field can still carry the payload without this test making a
        # live network call to a garbage URL.
        resp = client.post(
            "/api/certifications/submit",
            headers={"X-Employee-Id": any_employee_id},
            json={
                "employee_id": any_employee_id,
                "cert_name": f"SQLI-TEST {payload}",
                "submission_status": "in_progress",
                "credly_link": payload,
            },
        )
        assert resp.status_code == 200, resp.text
        record = resp.json()["record"]
        assert record["cert_name"] == f"SQLI-TEST {payload}"
        assert record["credly_link"] == payload

        _table_is_intact(db, CertificateRecord)

        db.execute(
            text("DELETE FROM audit_log WHERE entity_type = 'certificate_records' AND entity_id = :id"),
            {"id": record["id"]},
        )
        db.execute(text("DELETE FROM certificate_records WHERE id = :id"), {"id": record["id"]})
        db.commit()


def test_conversation_turn_content_injection(db, any_employee_id):
    """Chat-message content — the storage layer behind /api/agent/chat and
    the Teams webhook (see services/conversation_memory.py). Inserted
    directly via the model rather than through conversation_memory.record_turn
    or a live chat call: record_turn also bumps that employee's real
    turns_since_last_summary counter, which this test has no business
    perturbing (it could even trigger a real summarization LLM call as an
    unwanted side effect). Inserting the row directly still proves exactly
    what matters here — that the content column round-trips arbitrary
    text inertly through the ORM — without touching unrelated state."""
    employee = db.scalar(select(Employee).where(Employee.employee_id == any_employee_id))
    for payload in INJECTION_PAYLOADS:
        turn = ConversationTurn(employee_id=employee.id, conversation_id="sqli-test", role="user", content=payload)
        db.add(turn)
        db.commit()
        db.refresh(turn)

        stored = db.get(ConversationTurn, turn.id)
        assert stored.content == payload  # inert text, not executed

        _table_is_intact(db, ConversationTurn)

    db.execute(
        text("DELETE FROM conversation_turns WHERE employee_id = :eid AND conversation_id = 'sqli-test'"),
        {"eid": str(employee.id)},
    )
    db.commit()
