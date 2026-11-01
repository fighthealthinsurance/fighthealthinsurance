"""Experimental feature flags, as the staff Experiments page lists them.

An experiment is a setting in settings.py that is read from the
environment, off by default, and gates an unfinished feature (AGENTS.md).
Each entry says what its flag turns on and what else must be set before it
does anything, so the page can tell a flag that is on from one that is set
but inert (a TypeSafe switch without TYPESAFE_API_KEY, say).

scripts/run_local.sh keeps its own EXPERIMENTAL_FLAGS list for
--all-experiments, since a shell script can't import this;
tests/async-unit/test_run_local_experimental_flags.py fails when the two
disagree.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.conf import settings
from django.utils.module_loading import import_string
from loguru import logger

ON = "on"
OFF = "off"
# Set, but something it needs is missing, so it does nothing.
INERT = "inert"
# Its live check raised; the page says so instead of guessing.
UNKNOWN = "unknown"
STATES = (ON, INERT, OFF, UNKNOWN)

PAGES = "Pages and site features"
MCP = "AI assistants (MCP)"
MODELS = "Models and chat"
TEMPORAL = "Temporal (needs a Temporal server and worker)"

_TYPESAFE_KEY = ("TYPESAFE_API_KEY",)


@dataclass(frozen=True)
class Experiment:
    setting: str
    label: str
    description: str
    group: str
    # Other settings that must be set as well before this one does anything.
    # Only their names are ever shown, never their values (some are keys).
    needs: Tuple[str, ...] = ()
    # Dotted path to the function the code itself asks whether the feature
    # is live, where there is one. ON needs it and every name in needs.
    live_check: Optional[str] = None
    # Stops a feature rather than starting one.
    kill_switch: bool = False
    # Whether run_local.sh --all-experiments turns it on; False for the ones
    # its "Left out on purpose" comment names.
    all_experiments: bool = True


EXPERIMENTS: Tuple[Experiment, ...] = (
    Experiment(
        "MEDICAID_ELIGIBILITY_PAGE_ENABLED",
        "Medicaid eligibility page",
        "The Medicaid eligibility landing page. Off, it serves 404 and stays "
        "out of the sitemap.",
        PAGES,
        live_check="fighthealthinsurance.utils.medicaid_eligibility_page_enabled",
    ),
    Experiment(
        "DENIAL_WATCH_PAGE_ENABLED",
        "Denial Watch",
        "The public-data denial trends page. Off, it serves 404 and stays out "
        "of the sitemap.",
        PAGES,
        live_check="fighthealthinsurance.utils.denial_watch_page_enabled",
    ),
    Experiment(
        "ARENA_RANKINGS_PAGE_ENABLED",
        "Appeal Arena rankings",
        "The public rankings page, /chooser/rankings/, and its JSON. Off, both "
        "serve 404 and nothing links to them. The Arena itself is not behind "
        "this flag.",
        PAGES,
        live_check="fighthealthinsurance.utils.arena_rankings_page_enabled",
    ),
    Experiment(
        "PRO_VERSION_AVAILABLE",
        "Professional version available",
        "/pro_version shows the professional version with trial and learn-more "
        "links instead of the coming-soon interest form.",
        PAGES,
    ),
    Experiment(
        "ADVANCED_OCR_OFFERED",
        "Advanced OCR option",
        "The upload page offers the on-device advanced OCR option. The "
        "checkbox itself still starts unticked.",
        PAGES,
    ),
    Experiment(
        "FIGHT_PAPERWORK_ENABLED",
        "Fight Paperwork",
        "Fight Paperwork's accounts and their emails, its sign-in pages and "
        "REST routes (all but the chooser), the appeal PDF route, "
        "/ws/prior-auth/, and professional and trial chats.",
        PAGES,
    ),
    Experiment(
        "MCP_SERVER_ENABLED",
        "MCP server",
        "The MCP server for AI assistants at /mcp, with its read-only tools. "
        "Off, /mcp is a 404. Read when the app starts, so a change needs a "
        "restart.",
        MCP,
    ),
    Experiment(
        "MCP_PREPARE_APPEAL_ENABLED",
        "prepare_appeal tool",
        "The MCP tool that takes a denial letter for the site's form, and "
        "/from-your-assistant, the page its links open.",
        MCP,
        needs=("MCP_SERVER_ENABLED",),
        live_check="fighthealthinsurance.mcp_server.prepare_appeal_enabled",
    ),
    Experiment(
        "MCP_HANDOFF_V2_ENABLED",
        "Handoff links v2",
        "A handoff link says what kind of link it is and which client made "
        "it, and binds to the first browser that opens it. Older links still "
        "open.",
        MCP,
    ),
    Experiment(
        "MCP_DRAFT_IN_CHAT_ENABLED",
        "Draft appeals in chat",
        "The chat path: letters drafted in the background for an assistant to "
        "bring back into the conversation.",
        MCP,
        needs=(
            "MCP_SERVER_ENABLED",
            "MCP_PREPARE_APPEAL_ENABLED",
            "MCP_HANDOFF_V2_ENABLED",
            "TEMPORAL_ENABLED",
            "TEMPORAL_APPEAL_JOURNEY_ENABLED",
            "TEMPORAL_PAYLOAD_KEY",
        ),
        live_check="fighthealthinsurance.mcp_server.chat_path_enabled",
    ),
    Experiment(
        "MCP_DRAFT_IN_CHAT_PAUSED",
        "Pause drafting in chat",
        "Pauses the chat path without taking its tools away: "
        "draft_appeal_in_chat answers with a link to the site's form instead.",
        MCP,
        kill_switch=True,
        all_experiments=False,
    ),
    Experiment(
        "FHI_CHAT_POLICY_APPLY",
        "Follow the chat routing policy",
        "Chat routes by the newest computed routing policy instead of the "
        "default order. Off, policies are still computed and shown on the ML "
        "Model Usage Dashboard.",
        MODELS,
    ),
    Experiment(
        "FHI_CHAT_JEV_GATE_ENABLED",
        "Chat reply check (Jev)",
        "Jev checks our own chat reply before the paid outside models are "
        "asked, in chats that allowed outside models.",
        MODELS,
        needs=_TYPESAFE_KEY,
        live_check="fighthealthinsurance.ml.chat_gate.enabled",
    ),
    Experiment(
        "TYPESAFE_LETTER_RANKING_ENABLED",
        "Letter scoring (Jev)",
        "Jev scores appeal drafts so they can be ranked.",
        MODELS,
        needs=_TYPESAFE_KEY,
        live_check="fighthealthinsurance.ml.letter_quality.enabled",
    ),
    Experiment(
        "TYPESAFE_DENIAL_TRIAGE_ENABLED",
        "Denial triage (Jev)",
        "Jev triages each denial letter. Also the kill switch for triages "
        "already stored.",
        MODELS,
        needs=_TYPESAFE_KEY,
        live_check="fighthealthinsurance.ml.denial_triage.enabled",
    ),
    Experiment(
        "TYPESAFE_DEADLINE_SHOW_ENABLED",
        "Show appeal deadlines",
        "Readers are told the appeal deadline triage read from their letter, "
        "on the review and questions pages and in the REST triage field.",
        MODELS,
        needs=_TYPESAFE_KEY + ("TYPESAFE_DENIAL_TRIAGE_ENABLED",),
    ),
    Experiment(
        "TYPESAFE_RESEARCH_JUDGING_ENABLED",
        "Research judging (Jev)",
        "Jev judges each PubMed article against the denial's treatment; off "
        "topic or contrary articles are left out of the appeal's research.",
        MODELS,
        needs=_TYPESAFE_KEY,
        live_check="fighthealthinsurance.ml.research_judging.enabled",
    ),
    Experiment(
        "TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED",
        "Follow-up questions by Jev",
        "Jev decides whether the questions page's answers are enough for the "
        "appeal, for people who allowed outside models. Off, one of our own "
        "models decides.",
        MODELS,
        needs=_TYPESAFE_KEY + ("FHI_FOLLOWUP_QUESTIONS_ENABLED",),
        live_check="fighthealthinsurance.ml.followup_questions.jev_enabled",
    ),
    Experiment(
        "TYPESAFE_CHAT_SHADOW_ENABLED",
        "Chat shadow scores (Jev)",
        "A background task scores delivered chat replies for the staff "
        "dashboard, in chats that allowed outside models. Never on the "
        "reply's own path.",
        MODELS,
        needs=_TYPESAFE_KEY,
        live_check="fighthealthinsurance.ml.chat_shadow.enabled",
    ),
    Experiment(
        "TEMPORAL_ENABLED",
        "Temporal",
        "Fax sending runs as a durable Temporal workflow instead of on the Ray "
        "fax actor. Every other Temporal flow needs it too.",
        TEMPORAL,
        all_experiments=False,
    ),
    Experiment(
        "TEMPORAL_APPEAL_JOURNEY_ENABLED",
        "Appeal journey on Temporal",
        "Queued appeal generation runs as GenerateAppealWorkflow.",
        TEMPORAL,
        needs=("TEMPORAL_ENABLED",),
        all_experiments=False,
    ),
    Experiment(
        "TEMPORAL_INTAKE_JOURNEY_ENABLED",
        "Intake journey on Temporal",
        "The intake journey, from the first screen, runs as " "IntakeJourneyWorkflow.",
        TEMPORAL,
        needs=("TEMPORAL_ENABLED", "TEMPORAL_APPEAL_JOURNEY_ENABLED"),
        all_experiments=False,
    ),
    Experiment(
        "INTAKE_CLOSED_CASE_CLEARS_HEALTH_HISTORY",
        "Clear health history of closed intakes",
        "When an intake journey closes unfinished, its case's health history "
        "and the caches made from it are cleared too.",
        TEMPORAL,
        needs=(
            "TEMPORAL_ENABLED",
            "TEMPORAL_APPEAL_JOURNEY_ENABLED",
            "TEMPORAL_INTAKE_JOURNEY_ENABLED",
        ),
        all_experiments=False,
    ),
    Experiment(
        "TEMPORAL_CHAT_POLICY_ENABLED",
        "Chat policy schedule on Temporal",
        "A daily Temporal Schedule computes the chat routing policy.",
        TEMPORAL,
        needs=("TEMPORAL_ENABLED",),
        live_check="fighthealthinsurance.temporal_client.chat_policy_schedule_enabled",
        all_experiments=False,
    ),
    Experiment(
        "TEMPORAL_PLAN_DOCUMENTS_ENABLED",
        "Plan documents on Temporal",
        "Shared plan documents are read, and the hourly plan documents sweep "
        "runs, on Temporal in the research namespace.",
        TEMPORAL,
        needs=("TEMPORAL_ENABLED", "TEMPORAL_RESEARCH_PAYLOAD_KEY"),
        live_check="fighthealthinsurance.temporal_client.plan_documents_enabled",
        all_experiments=False,
    ),
)


def _state(experiment: Experiment) -> Dict[str, Any]:
    missing = [name for name in experiment.needs if not getattr(settings, name, None)]
    row: Dict[str, Any] = {
        "experiment": experiment,
        "state": ON,
        "missing": missing,
        "error": None,
    }
    if not getattr(settings, experiment.setting, False):
        row["state"] = OFF
    elif missing:
        row["state"] = INERT
    elif experiment.live_check:
        try:
            live = bool(import_string(experiment.live_check)())
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Experiment live check {experiment.live_check} failed"
            )
            row["state"] = UNKNOWN
            row["error"] = type(e).__name__
        else:
            row["state"] = ON if live else INERT
    return row


def experiment_states() -> List[Dict[str, Any]]:
    """One row per experiment, in EXPERIMENTS order, as this process loaded
    its settings: the experiment, its state (ON, INERT, OFF or UNKNOWN),
    the names in its needs that are unset, and the exception's name when
    its live check raised. Settings only; never raises."""
    return [_state(experiment) for experiment in EXPERIMENTS]


def grouped(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows by group, groups in the order they first appear."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["experiment"].group, []).append(row)
    return [{"name": name, "rows": members} for name, members in groups.items()]


def summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """How many rows are in each state, every state present."""
    counts = {state: 0 for state in STATES}
    for row in rows:
        counts[row["state"]] += 1
    return counts
