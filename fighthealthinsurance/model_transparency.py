"""The data behind the About Our AI page: which models write appeal letters,
and what each test of them found.

Two kinds of files in static/model_transparency/, so the page changes by
editing data, never page code:

* ``models.json``: one entry per model, with what it does and where it runs,
  and the judges whose scores the test used.
  A model is retired by setting ``retired`` (a date) and ``retired_reason``;
  entries are never deleted, so the page can always say what changed.
* ``runs/<id>.json``: one file per test run, never edited after it is
  published: what was tested, how, and the results, as totals only.

``show_details`` in ``models.json`` decides how much the page says. While it
is false, a model we run ourselves is shown as an FHI model, with no base
model, maker or setup, and the tests, judges and scores stay off the page.
Setting it true shows them all again; no page code changes.

Both are public. They carry scores, timings and costs, never a letter, a
denial or anything about a person; the loaders refuse any field they do
not know, so nothing else can reach the page by accident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fighthealthinsurance.static_data import read_static_text

DATA_DIR = "model_transparency"

# How a model we run ourselves is named while show_details is off.
OUR_MODEL_NAME = "FHI model"
OUR_MODEL_FROM = "Fight Health Insurance"

TOP_FIELDS = frozenset({"about", "show_details", "models", "changes", "judges"})
CHANGE_FIELDS = frozenset({"date", "what", "detail"})

MODEL_FIELDS = frozenset(
    {
        "id",
        "name",
        "maker",
        "role",
        "runs_on",
        "setup",
        "ours",
        "routed_as",
        "tested_in",
        "retired",
        "retired_reason",
    }
)
RESULT_FIELDS = frozenset(
    {
        "rank",
        "name",
        "setup",
        "quality",
        "quality_is_estimate",
        "opus",
        "jev",
        "median_seconds",
        "cost_per_letter_usd",
        "cost_is_assumed",
        "decision",
    }
)
JUDGE_FIELDS = frozenset(
    {
        "name",
        "agreed_of_20",
        "strictness",
        "letters_scored",
        "flagged_made_up_facts",
    }
)
CURRENT_JUDGE_FIELDS = frozenset({"name", "made_by", "why"})
METHOD_FIELDS = frozenset(
    {
        "cases",
        "case_sources",
        "models_tested",
        "setups_tested",
        "setups_listed",
        "letters_written",
        "judgments",
        "quality",
        "cost",
        "writeup_url",
        "notes",
        "summary",
    }
)
RUN_FIELDS = frozenset(
    {
        "id",
        "title",
        "task",
        "date",
        "comparable_with",
        "method",
        "results",
        "judges",
    }
)


class DataError(ValueError):
    """A data file has a field the page doesn't know."""


_PLAIN = (str, int, float, bool, type(None))


def _only(
    fields: frozenset,
    row: Dict[str, Any],
    where: str,
    lists: frozenset = frozenset(),
    nested: frozenset = frozenset(),
) -> Dict[str, Any]:
    """The row, if every field is known and holds text, a number or null."""
    if not isinstance(row, dict):
        raise DataError(f"{where}: expected an object")
    unknown = set(row) - fields
    if unknown:
        raise DataError(f"{where}: unknown fields {sorted(unknown)}")
    for key, value in row.items():
        if key in nested:
            continue
        values = value if key in lists and isinstance(value, list) else [value]
        if not all(isinstance(v, _PLAIN) for v in values):
            raise DataError(f"{where}: {key} must hold plain values")
    return row


def _of_100(score: Optional[float]) -> str:
    return "not scored" if score is None else str(round(score * 100))


@dataclass(frozen=True)
class Result:
    rank: int
    name: str
    setup: str
    quality: float
    quality_is_estimate: bool
    opus: float
    jev: float
    median_seconds: float
    cost_per_letter_usd: Optional[float]
    cost_is_assumed: bool
    decision: str

    @property
    def quality_text(self) -> str:
        return _of_100(self.quality)

    @property
    def opus_text(self) -> str:
        return _of_100(self.opus)

    @property
    def jev_text(self) -> str:
        return _of_100(self.jev)

    @property
    def cost_text(self) -> str:
        cost = self.cost_per_letter_usd
        if cost is None:
            return "not recorded"
        if cost < 0.001:
            return "under $0.001"
        return f"${cost:.3f}"


@dataclass(frozen=True)
class Judge:
    name: str
    agreed_of_20: int
    strictness: str
    letters_scored: int
    flagged_made_up_facts: Optional[float]

    @property
    def flagged_text(self) -> str:
        rate = self.flagged_made_up_facts
        if rate is None:
            return "not counted"
        return f"{round(rate * 100)} of 100"


@dataclass(frozen=True)
class Run:
    id: str
    title: str
    task: str
    date: str
    comparable_with: List[str]
    method: Dict[str, Any]
    results: List[Result]
    judges: List[Judge]

    @property
    def letters_written_text(self) -> str:
        return f"{int(self.method.get('letters_written', 0)):,}"

    @property
    def judgments_text(self) -> str:
        return f"{int(self.method.get('judgments', 0)):,}"

    @property
    def chosen(self) -> Optional[Result]:
        return next((r for r in self.results if r.decision.startswith("Chosen")), None)


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    maker: str
    role: str
    runs_on: str
    setup: str
    ours: bool
    routed_as: str
    tested_in: Optional[str]
    retired: Optional[str]
    retired_reason: str

    def without_details(self) -> "Model":
        """Ours as an FHI model, with no base model, maker or setup."""
        if not self.ours:
            return self
        return replace(
            self,
            name=OUR_MODEL_NAME,
            maker=OUR_MODEL_FROM,
            setup="",
            routed_as="",
            tested_in=None,
        )


@dataclass(frozen=True)
class CurrentJudge:
    name: str
    made_by: str
    why: str


@dataclass(frozen=True)
class Transparency:
    models: List[Model]
    changes: List[Dict[str, Any]]
    runs: List[Run] = field(default_factory=list)
    judges: List[CurrentJudge] = field(default_factory=list)
    show_details: bool = False

    def _shown(self, models: List[Model]) -> List[Model]:
        if self.show_details:
            return models
        return [m.without_details() for m in models]

    @property
    def in_use(self) -> List[Model]:
        return self._shown([m for m in self.models if not m.retired])

    @property
    def retired(self) -> List[Model]:
        return self._shown([m for m in self.models if m.retired])

    @property
    def shown_changes(self) -> List[Dict[str, Any]]:
        """The changes log, without detail entries while details are off."""
        return [c for c in self.changes if self.show_details or not c.get("detail")]

    @property
    def latest_run(self) -> Optional[Run]:
        return self.runs[-1] if self.runs else None


def _read(name: str) -> Any:
    text = read_static_text(f"{DATA_DIR}/{name}")
    if text is None:
        raise FileNotFoundError(f"{DATA_DIR}/{name}")
    return json.loads(text)


def parse_run(data: Dict[str, Any]) -> Run:
    where = f"run {data.get('id', '?')}"
    _only(
        RUN_FIELDS,
        data,
        where,
        lists=frozenset({"comparable_with"}),
        nested=frozenset({"method", "results", "judges"}),
    )
    _only(
        METHOD_FIELDS,
        data.get("method", {}),
        f"{where} method",
        lists=frozenset({"notes"}),
    )
    results = [
        Result(**_only(RESULT_FIELDS, r, f"{where} result"))
        for r in data.get("results", [])
    ]
    judges = [
        Judge(**_only(JUDGE_FIELDS, j, f"{where} judge"))
        for j in data.get("judges", [])
    ]
    return Run(
        id=data["id"],
        title=data["title"],
        task=data["task"],
        date=data["date"],
        comparable_with=list(data.get("comparable_with", [])),
        method=data.get("method", {}),
        results=sorted(results, key=lambda r: r.rank),
        judges=judges,
    )


# The published runs, oldest first. A new run is a new file plus a line here.
RUN_FILES = ("runs/2026-09-letter-test.json",)


def parse_models(data: Dict[str, Any], runs: List[Run]) -> Transparency:
    _only(
        TOP_FIELDS,
        data,
        "models.json",
        nested=frozenset({"models", "changes", "judges"}),
    )
    models = [
        Model(**_only(MODEL_FIELDS, m, "models.json")) for m in data.get("models", [])
    ]
    judges = [
        CurrentJudge(**_only(CURRENT_JUDGE_FIELDS, j, "models.json judge"))
        for j in data.get("judges", [])
    ]
    changes = [
        _only(CHANGE_FIELDS, c, "models.json change") for c in data.get("changes", [])
    ]
    return Transparency(
        models=models,
        changes=changes,
        runs=runs,
        judges=judges,
        show_details=data.get("show_details") is True,
    )


@lru_cache(maxsize=1)
def load() -> Transparency:
    """Everything the page shows. Cached: the files change only on deploy."""
    runs = sorted((parse_run(_read(name)) for name in RUN_FILES), key=lambda r: r.date)
    return parse_models(_read("models.json"), runs)
