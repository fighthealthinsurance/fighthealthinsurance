"""Guards for prepare_appeal's alerts (k8s/assistant-handoff-alerts.yaml).

prepare_appeal's caps are global and /mcp is anonymous, so one caller can
fill them, and nothing errors when that happens: assistants are told to send
their person to paste the letter by hand. The alerts are the only way anyone
finds out, so they have to name metrics the app really exports, add them up
the right way across pods, and keep their threshold in step with the cap.
"""

import pathlib
import re

import yaml
from prometheus_client import Counter

from fighthealthinsurance import assistant_handoff

REPO = pathlib.Path(__file__).resolve().parents[2]
ALERTS = REPO / "k8s" / "assistant-handoff-alerts.yaml"
BUILD = REPO / "scripts" / "build.sh"
SETTINGS = REPO / "fighthealthinsurance" / "settings.py"
DEPLOY = REPO / "k8s" / "deploy.yaml"
DOCS = REPO / "docs" / "mcp-server.md"

AT_CAP = "FhiAssistantHandoffAtCap"
NEAR_CAP = "FhiAssistantHandoffNearCap"
METRIC = re.compile(r"\bfhi_assistant_handoff_\w+")
DURATION = re.compile(r"^\d+[smhdwy]$")


def rules() -> dict[str, dict]:
    doc = yaml.safe_load(ALERTS.read_text())
    assert doc["kind"] == "PrometheusRule", doc["kind"]
    found: dict[str, dict] = {}
    for group in doc["spec"]["groups"]:
        for rule in group["rules"]:
            found[rule["alert"]] = rule
    return found


def exported() -> dict[str, str]:
    """Every series name assistant_handoff.py exports, with its type."""
    names: dict[str, str] = {}
    for value in vars(assistant_handoff).values():
        if isinstance(value, Counter):
            for family in value.collect():
                for sample in family.samples:
                    names[sample.name] = family.type
    # describe(), not collect(): collect() counts the table.
    for family in assistant_handoff.AssistantHandoffCollector().describe():
        names[family.name] = family.type
    return names


def live_cap() -> int:
    """MCP_PREPARE_APPEAL_MAX_LIVE as production runs it: k8s/deploy.yaml's
    value when it sets one, otherwise the default in settings.py."""
    deployed = re.search(
        r"name:\s*MCP_PREPARE_APPEAL_MAX_LIVE\s*\n\s*value:\s*\"?(\d+)",
        DEPLOY.read_text(),
    )
    if deployed:
        return int(deployed.group(1))
    default = re.search(
        r"\"MCP_PREPARE_APPEAL_MAX_LIVE\",\s*([\d_]+)", SETTINGS.read_text()
    )
    assert default, "MCP_PREPARE_APPEAL_MAX_LIVE's default not found in settings.py"
    return int(default.group(1).replace("_", ""))


def balanced(expr: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_string = False
    for char in expr:
        if char == '"':
            in_string = not in_string
        elif in_string:
            continue
        elif char in "([{":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return False
    return not stack and not in_string


class TestTheRules:
    def test_there_are_the_two_warnings(self):
        found = rules()
        assert set(found) == {AT_CAP, NEAR_CAP}, set(found)
        for name, rule in found.items():
            assert rule["labels"]["severity"] == "warning", name
            assert DURATION.match(str(rule["for"])), (name, rule["for"])
            assert rule["annotations"]["summary"], name
            assert rule["annotations"]["description"], name

    def test_every_metric_named_is_one_the_app_exports(self):
        """In the expressions and in the runbook text: a typo in either is a
        query that returns nothing, and an alert that can never fire."""
        names = exported()
        for name, rule in rules().items():
            text = rule["expr"] + " " + rule["annotations"]["description"]
            used = set(METRIC.findall(text))
            assert used, name
            assert used <= set(names), (name, used - set(names))

    def test_the_expressions_are_well_formed(self):
        names = exported()
        for name, rule in rules().items():
            expr = rule["expr"].strip()
            assert balanced(expr), (name, expr)
            # Ends in a comparison, so it fires on a condition, not on any
            # series that exists.
            assert re.search(r"\)\s*>\s*\d+$", expr), (name, expr)
            for window in re.findall(r"\[([^\]]*)\]", expr):
                assert DURATION.match(window), (name, window)
            for metric in METRIC.findall(expr):
                kind = names[metric]
                if kind == "counter":
                    # A counter's raw value is a running total since its pod
                    # started; only its increase over a window means anything.
                    assert re.search(rf"\b(increase|rate)\({metric}\[", expr), (
                        name,
                        expr,
                    )
                else:
                    assert not re.search(rf"{metric}\s*\[", expr), (name, expr)

    def test_the_refusal_counter_is_summed_across_pods(self):
        """Each web pod counts its own refusals, so a refusal on any pod has
        to count. A bare increase() would fire per pod, and max() would hide
        refusals spread thinly across pods."""
        expr = rules()[AT_CAP]["expr"]
        assert "sum(increase(fhi_assistant_handoff_refused_at_cap_total[15m]))" in (
            expr.replace(" ", "")
        ), expr
        assert re.search(r">\s*0$", expr.strip()), expr
        assert rules()[AT_CAP]["for"] == "0m"

    def test_the_live_gauge_is_taken_once_not_summed(self):
        """Every pod reads the same table-wide count, so sum() would report
        six times the real number and fire at 40 live links, not 240."""
        expr = rules()[NEAR_CAP]["expr"]
        assert "max(fhi_assistant_handoff_live_links)" in expr.replace(" ", ""), expr
        assert "sum(" not in expr, expr
        assert rules()[NEAR_CAP]["for"] == "30m"

    def test_the_near_cap_threshold_is_80_percent_of_the_live_cap(self):
        expr = rules()[NEAR_CAP]["expr"]
        threshold = int(re.search(r">\s*(\d+)\s*$", expr).group(1))
        assert threshold == live_cap() * 80 // 100, (threshold, live_cap())
        # ...and the comment says which setting it stands for.
        assert "MCP_PREPARE_APPEAL_MAX_LIVE" in ALERTS.read_text()

    def test_the_runbook_says_what_to_do(self):
        for name, rule in rules().items():
            text = rule["annotations"]["description"]
            assert "fhi_assistant_handoff_links_made_total" in text, name
            assert "fhi_assistant_handoff_forms_opened_total" in text, name
            assert "Cloudflare" in text, name
            assert "MCP_PREPARE_APPEAL_MAX_LIVE" in text, name
            assert "docs/mcp-server.md" in text, name
        assert DOCS.exists()
        docs = DOCS.read_text()
        assert AT_CAP in docs and NEAR_CAP in docs


class TestTheDeploy:
    def test_build_applies_the_rules_behind_the_crd_guard(self):
        """Guarded like every other rule file, with crd_present(), so an API
        error is not mistaken for a cluster without the operator."""
        build = BUILD.read_text()
        at = build.index("kubectl apply -f k8s/assistant-handoff-alerts.yaml")
        guard = build.rindex("crd_present prometheusrules.monitoring.coreos.com", 0, at)
        assert at - guard < 200, build[guard:at]
