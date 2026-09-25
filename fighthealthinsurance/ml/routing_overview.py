"""Which models each request path would use, as this process sees it.

The staff Model Backend Status page shows this so nobody has to read the
router to know which model serves what. It calls the router's own
synchronous selectors instead of restating their rules, so the page can't
drift from what requests actually do. None of those selectors calls a model
or the network: they read in-memory signals only (quality, the in-memory
availability flags, and the last cached health sweep). Reading the sweep
result starts this pod's background sweep if nothing has started it yet,
the same as the first routed request would.

All of it is per process. Each pod builds its own router and runs its own
health sweep, so two pods can disagree until the next sweep.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple, Type

from fighthealthinsurance.env_utils import get_env_variable
from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml.ml_models import RemoteModel, RemoteModelLike
from fighthealthinsurance.ml.ml_router import MLRouter

KIND_INTERNAL = "internal"
# An internal fine-tune that only writes appeal text (fhi-legacy). The router
# keeps it out of chat, questions and summaries.
KIND_APPEAL_ONLY = "appeal-only fine-tune"
KIND_EXTERNAL = "external"
# Reserved for building context such as citations (Perplexity's sonar).
KIND_CONTEXT_ONLY = "context only"

# When a role applies, across the two settings of use_external.
WHEN_EITHER = ""
WHEN_EXTERNAL_ALLOWED = "external allowed"
WHEN_INTERNAL_ONLY = "internal only"

PATH_APPEALS = "Appeals"
PATH_CHAT = "Chat"
PATH_QUESTIONS = "Questions"
PATH_SUMMARIES = "Summaries"


@dataclass(frozen=True)
class RouteRole:
    """One way a request path uses a model, e.g. Summaries: 1st."""

    path: str
    role: str
    when: str = WHEN_EITHER

    @property
    def external_allowed_only(self) -> bool:
        return self.when == WHEN_EXTERNAL_ALLOWED

    @property
    def label(self) -> str:
        text = f"{self.path}: {self.role}"
        return f"{text} ({self.when})" if self.when else text


@dataclass(frozen=True)
class PlanEntry:
    """A model name in a path's list, with a short note such as "retry only"."""

    name: str
    note: str = ""


@dataclass
class PathPlan:
    """One request path's ordered models with use_external off and on."""

    title: str
    how: str
    internal_only: List[PlanEntry]
    external_allowed: List[PlanEntry]


@dataclass
class ModelTraits:
    """What kind of model a backend is and how the router ranks it."""

    quality: Optional[int]
    tier: str
    kind: str
    # True when read off the instance the router registered. False when read
    # off a stand-in built from the catalog, for a model this pod doesn't
    # route to.
    routed: bool


@dataclass
class RoutingOverview:
    """Every path's models, each model's roles, and the overrides in force."""

    paths: List[PathPlan]
    # (name, quality), best first: the externals the appeals backup pass and
    # chat add when external models are allowed.
    top_external: List[Tuple[str, int]]
    roles: Dict[str, List[RouteRole]]
    force_model: Optional[str]
    force_model_registered: bool
    enabled_remote_models: Optional[List[str]]

    def top_external_rank(self, name: str) -> Optional[int]:
        for rank, (top_name, _quality) in enumerate(self.top_external, start=1):
            if top_name == name:
                return rank
        return None


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _names_by_instance(router: MLRouter) -> Dict[int, str]:
    """The registry name of every registered instance, by identity."""
    names: Dict[int, str] = {}
    for name, instances in router.models_by_name.items():
        for m in instances:
            names.setdefault(id(m), name)
    return names


class _RoleCollector:
    """Gathers (path, role) per model name under each use_external setting,
    then merges them so a role both settings share is shown once."""

    def __init__(self) -> None:
        self._seen: Dict[str, Dict[Tuple[str, str], Set[bool]]] = {}

    def add(self, name: str, path: str, role: str, modes: Sequence[bool]) -> None:
        by_role = self._seen.setdefault(name, {})
        by_role.setdefault((path, role), set()).update(modes)

    def roles(self) -> Dict[str, List[RouteRole]]:
        merged: Dict[str, List[RouteRole]] = {}
        for name, by_role in self._seen.items():
            merged[name] = [
                RouteRole(path, role, _when(modes))
                for (path, role), modes in by_role.items()
            ]
        return merged


def _when(modes: Set[bool]) -> str:
    if modes == {True}:
        return WHEN_EXTERNAL_ALLOWED
    if modes == {False}:
        return WHEN_INTERNAL_ONLY
    return WHEN_EITHER


def build_routing_overview(router: Optional[MLRouter] = None) -> RoutingOverview:
    """Ask the router's real selectors what each path would use right now.

    Each selector is called once with use_external off and once with it on.
    The router may log while it answers (a fail-open fallback logs an
    ERROR), exactly as it does on a request.
    """
    if router is None:
        router = ml_router_module._get_ml_router()
    names_by_id = _names_by_instance(router)
    collector = _RoleCollector()
    paths: List[PathPlan] = []

    def name_of(m: RemoteModelLike) -> str:
        return names_by_id.get(id(m), str(m))

    def entries(names: Sequence[str], notes: Dict[str, str]) -> List[PlanEntry]:
        out: List[PlanEntry] = []
        for name in names:
            if all(e.name != name for e in out):
                out.append(PlanEntry(name, notes.get(name, "")))
        return out

    # Appeals, primary pass. generate_appeal always asks for internal only
    # here, whatever the person chose, so both columns are the same list.
    primary = router.generate_text_backend_names(use_external=False)
    for name in primary:
        collector.add(name, PATH_APPEALS, "primary", (False, True))
    paths.append(
        PathPlan(
            "Appeals, primary pass",
            "Every model is asked at once and the first usable full letter "
            "wins. This pass is internal only whatever the person chose.",
            entries(primary, {}),
            entries(primary, {}),
        )
    )

    # Appeals, backup pass: only run when the primary pass gives nothing.
    backup = {
        flag: router.generate_text_backend_names(use_external=flag)
        for flag in (False, True)
    }
    for flag, names in backup.items():
        for name in names:
            collector.add(name, PATH_APPEALS, "backup", (flag,))
    paths.append(
        PathPlan(
            "Appeals, backup pass",
            "Asked only when the primary pass gives no usable letter. "
            "External models join only when the person allowed them.",
            entries(backup[False], {}),
            entries(backup[True], {}),
        )
    )

    # The single extra call that carries denial-type guidance.
    best = router.best_internal_model(general_only=False)
    hint = [name_of(best)] if best is not None else []
    for name in hint:
        collector.add(name, PATH_APPEALS, "best-internal hint", (False, True))
    paths.append(
        PathPlan(
            "Appeals, best-internal hint",
            "One extra call to the strongest internal model with denial-type "
            "guidance, made only when a specialized denial template matches.",
            entries(hint, {}),
            entries(hint, {}),
        )
    )

    chat: Dict[bool, List[PlanEntry]] = {}
    for flag in (False, True):
        chat_primary, chat_fallback = router.get_chat_backends_with_fallback(
            use_external=flag
        )
        # get_chat_backends lists its lead fhi backend twice up front (and
        # again among the internals), so an instance that repeats is the
        # doubled lead. Counted per instance, not per name: two backends
        # can share a name without either being the lead.
        repeats: Dict[int, int] = {}
        for m in chat_primary:
            repeats[id(m)] = repeats.get(id(m), 0) + 1
        doubled = {name_of(m) for m in chat_primary if repeats[id(m)] > 1}
        primary_names = list(dict.fromkeys(name_of(m) for m in chat_primary))
        notes: Dict[str, str] = {}
        for name in primary_names:
            if name in doubled:
                notes[name] = "doubled lead"
                collector.add(name, PATH_CHAT, "doubled lead", (flag,))
            else:
                collector.add(name, PATH_CHAT, "fan-out", (flag,))
        fallback_names = [name_of(m) for m in chat_fallback]
        for name in fallback_names:
            notes.setdefault(name, "retry only")
            collector.add(name, PATH_CHAT, "retry only", (flag,))
        chat[flag] = entries(primary_names + fallback_names, notes)
    paths.append(
        PathPlan(
            "Chat",
            "Asked at once; the best-scored reply wins and quality weighs "
            "heavily in the score. The doubled lead gets two calls.",
            chat[False],
            chat[True],
        )
    )

    questions = {
        flag: [name_of(m) for m in router.full_qa_backends(use_external=flag)]
        for flag in (False, True)
    }
    for flag, names in questions.items():
        for name in names:
            collector.add(name, PATH_QUESTIONS, "fan-out", (flag,))
    paths.append(
        PathPlan(
            "Appeal questions",
            "Asked at once; the answer with the best-shaped questions wins, "
            "with a small bonus for quality. Order here means nothing.",
            entries(questions[False], {}),
            entries(questions[True], {}),
        )
    )

    summaries = {
        flag: list(dict.fromkeys(name_of(m) for m in router.summarize_backends(flag)))
        for flag in (False, True)
    }
    for flag, names in summaries.items():
        for position, name in enumerate(names, start=1):
            collector.add(name, PATH_SUMMARIES, _ordinal(position), (flag,))
    paths.append(
        PathPlan(
            "Summaries",
            "Tried one at a time in this order; the first real answer wins.",
            entries(summaries[False], {}),
            entries(summaries[True], {}),
        )
    )

    top_external = [(name_of(m), m.quality()) for m in router.best_external_models()]

    force_model = get_env_variable("FORCE_MODEL") or None
    enabled = MLRouter._enabled_model_names()
    return RoutingOverview(
        paths=paths,
        top_external=top_external,
        roles=collector.roles(),
        force_model=force_model,
        force_model_registered=bool(
            force_model and force_model in router.models_by_name
        ),
        enabled_remote_models=sorted(enabled) if enabled is not None else None,
    )


def traits_of(model: RemoteModelLike, *, routed: bool) -> ModelTraits:
    """Kind, quality and tier, read off a backend instance."""
    get_tier = getattr(model, "get_tier", None)
    tier = get_tier() if callable(get_tier) else ""
    if model.context_only:
        kind = KIND_CONTEXT_ONLY
    elif model.external:
        kind = KIND_EXTERNAL
    elif model.supports_general_instructions():
        kind = KIND_INTERNAL
    else:
        kind = KIND_APPEAL_ONLY
    return ModelTraits(quality=model.quality(), tier=tier, kind=kind, routed=routed)


def row_traits(
    instance: Optional[RemoteModelLike],
    backend_cls: Optional[Type[RemoteModel]],
    internal_name: str,
) -> Optional[ModelTraits]:
    """Traits for one catalog row of the status page.

    A row the router registered is read off that very instance. Any other
    row (not configured, disabled, missing credentials) is read off a
    stand-in made without running the constructor, which would need the
    missing configuration. Quality and tier depend only on the class and the
    wire model name, so the stand-in answers the same way the real backend
    would. It is marked as not routed.
    """
    if instance is not None:
        return traits_of(instance, routed=True)
    if backend_cls is None:
        return None
    shadow = backend_cls.__new__(backend_cls)
    setattr(shadow, "model", internal_name)
    return traits_of(shadow, routed=False)
