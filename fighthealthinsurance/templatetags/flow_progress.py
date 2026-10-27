"""The appeal flow's progress rail, partials/flow_progress.html."""

from django import template

register = template.Library()

# Each stage the rail shows, and the flow's pages (current_step) inside it.
STAGES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("Upload", (1, 2, 3)),
    ("Check details", (4, 5)),
    ("Answer questions", (6,)),
    ("Get your appeal", (7,)),
    ("Send", (8,)),
)


@register.simple_tag
def flow_stages(current_step) -> list[dict[str, str]]:
    """Each stage's label and state: "done", "current" or "upcoming"."""
    try:
        step = int(current_step)
    except (TypeError, ValueError):
        step = 0
    here = next(
        (index for index, (_, pages) in enumerate(STAGES) if step in pages), None
    )
    stages = []
    for index, (label, _) in enumerate(STAGES):
        if here is None or index > here:
            state = "upcoming"
        elif index == here:
            state = "current"
        else:
            state = "done"
        stages.append({"label": label, "state": state})
    return stages
