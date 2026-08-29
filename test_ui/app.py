"""Local dev test UI — not a Teams client, just enough to exercise the
backend and the ADK agent by hand. Talks to two separate local servers:
  - the FastAPI backend (BACKEND_URL, default :8000)
  - `adk api_server` serving the agent (AGENT_API_URL, default :8001) —
    started separately; see agent/README.md
"""

import os
from datetime import date

import httpx
import streamlit as st

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8001")
AGENT_APP_NAME = "adk_agent"

st.set_page_config(page_title="L&D & Certification Agent — Dev UI", layout="wide")
st.title("L&D & Certification Agent — Dev UI")


@st.cache_data(ttl=15)
def fetch_employees():
    resp = httpx.get(f"{BACKEND_URL}/api/employees", timeout=10)
    resp.raise_for_status()
    return resp.json()


try:
    employees = fetch_employees()
except httpx.HTTPError as exc:
    st.error(f"Can't reach the backend at {BACKEND_URL}: {exc}")
    st.stop()

if not employees:
    st.warning("No employees yet. Run scripts/seed_test_employees.py first.")
    st.stop()

labels = {e["employee_id"]: f'{e["name"]} — {e["employee_id"]} — {e["practice"]}' for e in employees}
selected_id = st.sidebar.selectbox("Acting as", options=list(labels.keys()), format_func=lambda k: labels[k])
current_employee = next(e for e in employees if e["employee_id"] == selected_id)
st.sidebar.json(current_employee)

page = st.sidebar.radio("View", ["Chat", "Submit Certification", "Tracker"])

# st.tabs() renders every tab's body on every rerun regardless of which one
# is visible — that meant switching "acting as" or sending a chat message
# was also firing the Tracker tab's 3 backend calls and the Submit tab's
# prefill call, every single time. A radio + if/elif only runs the code for
# the page actually being viewed.

# --- Chat -----------------------------------------------------------------
if page == "Chat":
    st.caption(
        "Talks to `adk api_server` (agent/), which talks to the backend as tools. "
        "Session id = employee_id, so context is retained across turns per employee."
    )

    session_key = f"adk_session_ready::{selected_id}"
    history_key = f"chat_history::{selected_id}"
    if history_key not in st.session_state:
        st.session_state[history_key] = []

    if not st.session_state.get(session_key):
        try:
            httpx.post(
                f"{AGENT_API_URL}/apps/{AGENT_APP_NAME}/users/{selected_id}/sessions/{selected_id}",
                json={"employee_id": selected_id},
                timeout=10,
            )
        except httpx.HTTPError:
            pass  # may already exist, or the agent server isn't up yet — surfaced on send instead
        st.session_state[session_key] = True

    for role, text in st.session_state[history_key]:
        with st.chat_message(role):
            st.write(text)

    user_msg = st.chat_input("Ask about certifications, exam prep, your tracker status...")
    if user_msg:
        st.session_state[history_key].append(("user", user_msg))
        with st.chat_message("user"):
            st.write(user_msg)

        with st.chat_message("assistant"):
            try:
                with st.spinner(
                    "Waiting on Gemini — every call is relayed through a curl subprocess "
                    "on this network (see README's TLS-blocker note), so a single turn can "
                    "take anywhere from a few seconds to a couple of minutes."
                ):
                    resp = httpx.post(
                        f"{AGENT_API_URL}/run",
                        json={
                            "appName": AGENT_APP_NAME,
                            "userId": selected_id,
                            "sessionId": selected_id,
                            "newMessage": {"role": "user", "parts": [{"text": user_msg}]},
                        },
                        timeout=240,
                    )
                resp.raise_for_status()
                events = resp.json()
                reply_parts = []
                for event in events:
                    content = event.get("content") or event
                    if content.get("role") == "model":
                        for part in content.get("parts", []):
                            if part.get("text"):
                                reply_parts.append(part["text"])
                reply = "\n".join(reply_parts) if reply_parts else "(no text in response — see raw events below)"
                st.write(reply)
                with st.expander("Raw agent events"):
                    st.json(events)
            except httpx.HTTPError as exc:
                reply = f"Couldn't reach the agent at {AGENT_API_URL}: {exc}"
                st.error(reply)

        st.session_state[history_key].append(("assistant", reply))

# --- Submit Certification --------------------------------------------------
elif page == "Submit Certification":
    st.subheader("Submit or update a certification")

    cert_query = st.text_input("Search certification catalog", key="cert_query")
    catalog_matches = []
    if cert_query:
        try:
            r = httpx.get(f"{BACKEND_URL}/api/catalog/certifications", params={"q": cert_query}, timeout=10)
            catalog_matches = [c["name"] for c in r.json()]
        except httpx.HTTPError as exc:
            st.warning(f"Catalog search failed: {exc}")

    cert_name = st.selectbox(
        "Matched catalog entries (or type the exact name below if new)",
        options=["—"] + catalog_matches,
    )
    cert_name_manual = st.text_input("Cert name (exact — required)")
    final_cert_name = cert_name_manual.strip() or (cert_name if cert_name != "—" else "")

    status_display = st.selectbox(
        "Status", ["In Progress", "Completed", "Gave Exam but Failed"]
    )
    status_map = {
        "In Progress": "in_progress",
        "Completed": "completed",
        "Gave Exam but Failed": "gave_exam_but_failed",
    }
    submission_status = status_map[status_display]

    try:
        prefill = httpx.get(f"{BACKEND_URL}/api/employees/{selected_id}/prefill", timeout=10).json()
    except httpx.HTTPError:
        prefill = {}

    manager_name = st.text_input("Manager", value=prefill.get("manager_name") or "")
    pd_name = st.text_input("Practice Director", value=prefill.get("practice_director_name") or "")
    st.text_input("Practice (auto-populated, read-only)", value=current_employee["practice"], disabled=True)

    col1, col2 = st.columns(2)
    with col1:
        prep_start_date = st.date_input("Prep Start Date", value=date.today())
    with col2:
        exam_end_date = st.date_input("End Date / Exam Date", value=date.today())

    credly_link = None
    if submission_status == "completed":
        credly_link = st.text_input("Credly Link (required for Completed — triggers verification)")

    expiration_date = st.date_input("Expiration Date (optional)", value=None)

    if st.button("Submit certification"):
        if not final_cert_name:
            st.error("Cert name is required.")
        elif submission_status == "completed" and not credly_link:
            st.error("Credly Link is required when Status is Completed.")
        else:
            payload = {
                "employee_id": selected_id,
                "cert_name": final_cert_name,
                "submission_status": submission_status,
                "manager_name": manager_name or None,
                "practice_director_name": pd_name or None,
                "prep_start_date": prep_start_date.isoformat() if prep_start_date else None,
                "exam_end_date": exam_end_date.isoformat() if exam_end_date else None,
                "credly_link": credly_link or None,
                "expiration_date": expiration_date.isoformat() if expiration_date else None,
            }
            resp = httpx.post(f"{BACKEND_URL}/api/certifications/submit", json=payload, timeout=30)
            if resp.status_code >= 400:
                st.error(resp.json())
            else:
                st.success("Submitted.")
                st.json(resp.json())

# --- Tracker ----------------------------------------------------------------
elif page == "Tracker":
    st.subheader(f"Tracker — scoped to what {current_employee['name']} can see")
    headers = {"X-Employee-Id": selected_id}

    resp = httpx.get(f"{BACKEND_URL}/api/tracker/records", headers=headers, timeout=10)
    if resp.status_code >= 400:
        st.error(resp.json())
    else:
        data = resp.json()
        st.caption(f"Data as of {data['data_as_of']}")
        st.dataframe(data["records"], use_container_width=True)

    st.subheader("At-risk / expiring within 30 days")
    resp2 = httpx.get(
        f"{BACKEND_URL}/api/tracker/at-risk", params={"window_days": 30}, headers=headers, timeout=10
    )
    if resp2.status_code >= 400:
        st.error(resp2.json())
    else:
        st.dataframe(resp2.json()["records"], use_container_width=True)

    st.subheader("Full certification catalog")
    resp3 = httpx.get(f"{BACKEND_URL}/api/catalog/certifications", timeout=10)
    st.dataframe(resp3.json(), use_container_width=True)

    st.subheader("Trigger Excel tracker sync")
    if st.button("Run sync now"):
        resp4 = httpx.post(f"{BACKEND_URL}/api/admin/sync-tracker", timeout=60)
        st.json(resp4.json())
