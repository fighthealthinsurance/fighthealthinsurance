"""Which models each request path would use, as this process sees it.

The staff Model Backend Status page shows this so nobody has to read the
router to know which model serves what. It calls the router's own
synchronous selectors instead of restating their rules, so the page can't
drift from what requests actually do. None of those selectors calls a model,
and none waits on the network. They read cached signals only: each model's
quality, its own is_available() and, for backends without a live signal,
the last health sweep's result. Like any routed request, the first read on
a pod whose background health sweep hasn't started yet starts it, and that
sweep probes the backends' /models endpoints in a background thread.

Appeals route by registry name: the caller tries every backend registered
under the name, in turn. Chat, questions and summaries route to backend
instances, and two backends can share a name (two internal servers set to
the same model path, say) while only one of them is picked. So appeal roles
are kept by name and the others by instance.

All of it is per process. Each pod builds its own router and runs its own
health sweep, so two pods can disagree until the next sweep.
"""

from dataclasses import dataclass
from typing import (
    Dict,
    Generic,
    Hashable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
)

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
    """A model in a path's list, with a short note such as "retry only".

    ``calls`` is how many times the selector's list names this model, which
    is how many calls it gets on that path.
    """

    name: str
    note: str = ""
    calls: int = 1

    @property
    def detail(self) -> str:
        parts = [self.note] if self.note else []
        if self.calls > 1:
            parts.append(f"{self.calls} calls")
        return ", ".join(parts)


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
    # The same models, by instance identity, in the same order.
    top_external_ids: List[int]
    # Roles on paths that route by registry name (appeals).
    roles_by_name: Dict[str, List[RouteRole]]
    # Roles on paths that route to one backend instance, keyed by id().
    roles_by_instance: Dict[int, List[RouteRole]]
    force_model: Optional[str]
    force_model_registered: bool
    # True when every backend registered under the forced name is external,
    # so a request with external models off skips it.
    force_model_external: bool
    enabled_remote_models: Optional[List[str]]

    def roles_for(
        self, name: str, instance: Optional[RemoteModelLike]
    ) -> List[RouteRole]:
        """The roles of one registered backend: those its name gets on the
        appeal paths, then those the instance itself gets."""
        roles = list(self.roles_by_name.get(name, []))
        if instance is not None:
            roles += self.roles_by_instance.get(id(instance), [])
        return roles

    def top_external_rank(self, instance: Optional[RemoteModelLike]) -> Optional[int]:
        if instance is None:
            return None
        for rank, top_id in enumerate(self.top_external_ids, start=1):
            if top_id == id(instance):
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


K = TypeVar("K", bound=Hashable)


class _RoleCollector(Generic[K]):
    """Gathers (path, role) per model under each use_external setting, then
    merges them so a role both settings share is shown once. A model is a
    registry name or an instance id, whichever the path routes by."""

    def __init__(self) -> None:
        self._seen: Dict[K, Dict[Tuple[str, str], Set[bool]]] = {}

    def add(self, key: K, path: str, role: str, modes: Sequence[bool]) -> None:
        by_role = self._seen.setdefault(key, {})
        by_role.setdefault((path, role), set()).update(modes)

    def roles(self) -> Dict[K, List[RouteRole]]:
        merged: Dict[K, List[RouteRole]] = {}
        for key, by_role in self._seen.items():
            merged[key] = [
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


def _provider_of(model: RemoteModelLike) -> str:
    return getattr(model, "PROVIDER_LABEL", "") or type(model).__name__


def build_routing_overview(router: Optional[MLRouter] = None) -> RoutingOverview:
    """Ask the router's real selectors what each path would use right now.

    Each selector is called once with use_external off and once with it on.
    The router may log while it answers (a fail-open fallback logs an
    ERROR), exactly as it does on a request.
    """
    if router is None:
        router = ml_router_module._get_ml_router()
    names_by_id = _names_by_instance(router)
    # Names more than one backend is registered under. In the lists that
    # route to instances, their entries also name the provider, so the two
    # can be told apart.
    shared_names = {
        name for name, instances in router.models_by_name.items() if len(instances) > 1
    }
    by_name: _RoleCollector[str] = _RoleCollector()
    by_instance: _RoleCollector[int] = _RoleCollector()
    paths: List[PathPlan] = []

    def name_of(m: RemoteModelLike) -> str:
        return names_by_id.get(id(m), str(m))

    def label_of(m: RemoteModelLike) -> str:
        name = name_of(m)
        if name in shared_names:
            return f"{name} on {_provider_of(m)}"
        return name

    def name_entries(names: Sequence[str]) -> List[PlanEntry]:
        """Entries for a path that routes by name. A name several backends
        share is tried on each of them in turn until one answers."""
        out: List[PlanEntry] = []
        for name in dict.fromkeys(names):
            count = len(router.models_by_name.get(name, []))
            note = f"{count} backends, tried in turn" if count > 1 else ""
            out.append(PlanEntry(name, note))
        return out

    def instance_entries(
        models: Sequence[RemoteModelLike], notes: Dict[int, str]
    ) -> List[PlanEntry]:
        """Entries for a path that routes to instances, in first-listed
        order, each with how many times the list names it."""
        counts: Dict[int, int] = {}
        firsts: List[RemoteModelLike] = []
        for m in models:
            if id(m) not in counts:
                firsts.append(m)
            counts[id(m)] = counts.get(id(m), 0) + 1
        return [
            PlanEntry(label_of(m), notes.get(id(m), ""), counts[id(m)]) for m in firsts
        ]

    # Appeals, primary pass. generate_appeal always asks for internal only
    # here, whatever the person chose, so both columns are the same list.
    primary = router.generate_text_backend_names(use_external=False)
    for name in primary:
        by_name.add(name, PATH_APPEALS, "primary", (False, True))
    paths.append(
        PathPlan(
            "Appeals, primary pass",
            "Every model is asked at once and the first usable full letter "
            "wins. This pass is internal only whatever the person chose.",
            name_entries(primary),
            name_entries(primary),
        )
    )

    # Appeals, backup pass: only run when the primary pass gives nothing.
    backup = {
        flag: router.generate_text_backend_names(use_external=flag)
        for flag in (False, True)
    }
    for flag, names in backup.items():
        for name in names:
            by_name.add(name, PATH_APPEALS, "backup", (flag,))
    paths.append(
        PathPlan(
            "Appeals, backup pass",
            "Asked only when the primary pass gives no usable letter. "
            "External models join only when the person allowed them.",
            name_entries(backup[False]),
            name_entries(backup[True]),
        )
    )

    # The single extra call that carries denial-type guidance. The router
    # picks an instance, but generate_appeal sends the call by its name.
    best = router.best_internal_model(general_only=False)
    hint = [name_of(best)] if best is not None else []
    for name in hint:
        by_name.add(name, PATH_APPEALS, "best-internal hint", (False, True))
    paths.append(
        PathPlan(
            "Appeals, best-internal hint",
            "One extra call to the strongest internal model with denial-type "
            "guidance, made only when a specialized denial template matches.",
            name_entries(hint),
            name_entries(hint),
        )
    )

    chat: Dict[bool, List[PlanEntry]] = {}
    for flag in (False, True):
        chat_primary, chat_fallback = router.get_chat_backends_with_fallback(
            use_external=flag
        )
        # get_chat_backends lists its lead fhi backend twice up front and
        # again among the strongest internals, so the lead is the instance
        # the list repeats. Counted per instance, not per name: two backends
        # can share a name without either being the lead.
        repeats: Dict[int, int] = {}
        for m in chat_primary:
            repeats[id(m)] = repeats.get(id(m), 0) + 1
        notes: Dict[int, str] = {}
        for m in chat_primary:
            if repeats[id(m)] > 1:
                notes[id(m)] = "lead"
                role = f"lead, {repeats[id(m)]} calls"
            else:
                role = "fan-out"
            by_instance.add(id(m), PATH_CHAT, role, (flag,))
        for m in chat_fallback:
            notes.setdefault(id(m), "retry only")
            by_instance.add(id(m), PATH_CHAT, "retry only", (flag,))
        chat[flag] = instance_entries(list(chat_primary) + list(chat_fallback), notes)
    paths.append(
        PathPlan(
            "Chat",
            "Asked at once; the best-scored reply wins and quality weighs "
            "heavily in the score. The lead is the fhi model whose name sorts "
            "first, passing over appeal-only fine-tunes when there is another. "
            "It is listed twice up front and again among the six strongest "
            "internals, so it usually gets three calls. A model called more "
            "than once shows its count. When a long chat's history was cut "
            "short, each call also goes out once more with the full history "
            "if the model can take it.",
            chat[False],
            chat[True],
        )
    )

    questions = {
        flag: list(router.full_qa_backends(use_external=flag)) for flag in (False, True)
    }
    for flag, models in questions.items():
        for m in models:
            by_instance.add(id(m), PATH_QUESTIONS, "fan-out", (flag,))
    paths.append(
        PathPlan(
            "Appeal questions",
            "Asked at once; the answer with the best-shaped questions wins, "
            "with a small bonus for quality. Order here means nothing.",
            instance_entries(questions[False], {}),
            instance_entries(questions[True], {}),
        )
    )

    summaries = {
        flag: list({id(m): m for m in router.summarize_backends(flag)}.values())
        for flag in (False, True)
    }
    for flag, models in summaries.items():
        for position, m in enumerate(models, start=1):
            by_instance.add(id(m), PATH_SUMMARIES, _ordinal(position), (flag,))
    paths.append(
        PathPlan(
            "Summaries",
            "Tried one at a time in this order; the first real answer wins.",
            instance_entries(summaries[False], {}),
            instance_entries(summaries[True], {}),
        )
    )

    best_external = router.best_external_models()
    top_external = [(label_of(m), m.quality()) for m in best_external]

    force_model = get_env_variable("FORCE_MODEL") or None
    forced = router.models_by_name.get(force_model, []) if force_model else []
    enabled = MLRouter._enabled_model_names()
    return RoutingOverview(
        paths=paths,
        top_external=top_external,
        top_external_ids=[id(m) for m in best_external],
        roles_by_name=by_name.roles(),
        roles_by_instance=by_instance.roles(),
        force_model=force_model,
        force_model_registered=bool(forced),
        force_model_external=bool(forced) and all(m.external for m in forced),
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
