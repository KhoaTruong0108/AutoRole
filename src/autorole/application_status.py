from __future__ import annotations

KNOWN_APPLICATION_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "submitted",
    "submitted_dryrun",
    "submitted_dryrun_submit_failed",
    "assessment_invite",
    "assessment_invited",
    "rejected",
    "hired",
    "withdrawn",
    "offer",
    "offer_extended",
    "accepted",
)

TERMINAL_APPLICATION_STATUSES = frozenset(
    {
        "submitted",
        "submitted_dryrun",
        "submitted_dryrun_submit_failed",
        "assessment_invite",
        "assessment_invited",
        "rejected",
        "hired",
        "withdrawn",
        "offer",
        "offer_extended",
        "accepted",
    }
)


def normalize_application_status(status: str | None) -> str:
    if status is None:
        return ""
    return status.strip().lower()


def is_terminal_application_status(status: str | None) -> bool:
    return normalize_application_status(status) in TERMINAL_APPLICATION_STATUSES