from google.adk.agents.llm_agent import Agent

from .prompts import INSTRUCTION
from .tools import (
    create_escalation,
    get_at_risk_certifications,
    get_learning_path_overview,
    get_my_drive_obligations,
    get_next_certification_for_practice,
    get_tracker_records,
    open_certification_form,
    query_knowledge_base,
    search_certification_catalog,
    submit_certification,
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
        get_next_certification_for_practice,
        get_learning_path_overview,
        open_certification_form,
        submit_certification,
        get_tracker_records,
        get_at_risk_certifications,
        query_knowledge_base,
        create_escalation,
        get_my_drive_obligations,
    ],
)
