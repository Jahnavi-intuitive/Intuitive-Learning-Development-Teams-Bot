"""The one place a notification actually gets "sent."

SEAM: there is no real Teams bot/proactive-messaging integration in this
build (explicitly out of scope from the very first session — see
DEVELOPMENT_LOG.md's 2026-08-23 scoping entry: "not building yet: ...
Teams SDK/Azure Bot Service"). Until that exists, "sending" means logging
the message — enough to prove the sweep's idempotency/retry mechanics
work for real, which is what this build's definition of done actually
asks for. Swap the body of `send_notification` for a real Microsoft Graph
/ Bot Framework proactive-message call later; every caller in
services/notification_sweep.py already treats this as fallible (returns
False on failure) so nothing upstream needs to change.
"""

import logging

from app.models import Employee

logger = logging.getLogger("notifications")


def send_notification(recipient: Employee, message: str, *, force_fail: bool = False) -> bool:
    """Returns True on a successful send, False on failure. `force_fail`
    exists only for the admin dashboard's on-demand sweep trigger to test
    the retry pass deterministically (see schemas.SweepTriggerIn) — never
    set from anywhere else."""
    if force_fail:
        logger.warning("SIMULATED FAILURE sending to %s (%s): %s", recipient.name, recipient.employee_id, message)
        return False
    logger.info("[notification -> %s (%s)] %s", recipient.name, recipient.employee_id, message)
    return True
