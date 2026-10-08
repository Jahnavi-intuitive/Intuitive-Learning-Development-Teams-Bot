from google.adk.agents.llm_agent import Agent

from .prompts import INSTRUCTION
from .tools import (
    create_escalation,
    get_at_risk_certifications,
    get_my_certification_progress,
    get_my_drive_obligations,
    get_next_certification_recommendations,
    get_partnership_gaps,
    get_tracker_records,
    open_certification_form,
    query_knowledge_base,
    record_recommendation_feedback,
    search_certification_catalog,
    start_certification_prep,
    submit_certification,
    update_my_skill_profile,
)

root_agent = Agent(
    model="gemini-flash-latest",
    name="lnd_certification_agent",
    description=(
        "Helps employees discover certifications, prepare for exams, submit "
        "proof of completed certifications, and check certification/"
        "verification status — reading and writing the same tracker the "
        "leadership dashboard will use."
    ),
    instruction=INSTRUCTION,
    tools=[
        search_certification_catalog,
        get_my_certification_progress,
        start_certification_prep,
        get_next_certification_recommendations,
        update_my_skill_profile,
        record_recommendation_feedback,
        get_partnership_gaps,
        open_certification_form,
        submit_certification,
        get_tracker_records,
        get_at_risk_certifications,
        query_knowledge_base,
        create_escalation,
        get_my_drive_obligations,
    ],
)
