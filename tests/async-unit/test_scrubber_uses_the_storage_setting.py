"""What the scrubber recognises is stored the way the rest of the page stores.

The upload page offers "Remember form data" and warns that it should be
unticked on a shared computer. Every field on the page honours that through
the helpers in shared.ts, which also carry an expiry. The scrubber did not:
when a rule recognised a name, a subscriber id or a group id in the denial
letter, it wrote the value with a bare localStorage.setItem, so it was kept
whatever the person had chosen and never expired. The clear that runs when
the setting is turned off did not cover those keys either, because they do
not start with the prefix it looks for.

These pin the write path, the clear, and the one thing that keeps them from
drifting apart: the rule table's key column is a union type, so a new rule
cannot introduce a key the clear does not know about.
"""

import pathlib
import re

JS = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"


def _scrubber() -> str:
    return (JS / "scrub_scrub.ts").read_text()


def _shared() -> str:
    return (JS / "shared.ts").read_text()


def test_the_scrubber_stores_through_the_shared_helper():
    src = _scrubber()
    assert "setLocalStorageItemWithTTL(scrubRegex[i][1], match[1]);" in src, (
        "the scrubber no longer stores through the helper that honours the setting"
    )
    assert not re.search(r"localStorage\.setItem", src), (
        "a bare localStorage write is back in the scrubber; it would ignore the setting and the expiry"
    )


def test_the_helper_it_uses_actually_honours_the_setting():
    """Worth pinning here: if the helper stopped checking, the scrubber's fix
    would be silently undone from the other side."""
    shared = _shared()
    body = shared[shared.index("function setLocalStorageItemWithTTL") :]
    body = body[: body.index("\n}\n") + 3]
    assert re.search(r"if \(!isPersistenceEnabled\(\)\) \{\s*return;", body), (
        "the shared write helper no longer honours the persistence setting"
    )
    assert "expiry" in body, "the shared write helper no longer sets an expiry"


def test_turning_the_setting_off_clears_what_the_scrubber_stored():
    shared = _shared()
    body = shared[shared.index("function clearFormData") :]
    body = body[: body.index("\n}\n") + 3]
    assert "(SCRUBBER_STORAGE_KEYS as string[]).includes(key)" in body, (
        "the clear no longer covers the keys the scrubber writes"
    )
    assert 'key.startsWith("store_")' in body, "the clear stopped covering the form fields"


def test_a_new_rule_cannot_store_under_a_key_the_clear_does_not_know():
    """The rule table's key column is the union declared beside the clear, so
    a rule with a new key fails the type check rather than quietly leaving a
    value behind on a shared computer."""
    shared = _shared()
    assert re.search(
        r'type ScrubberStorageKey =\s*"name" \| "subscriber_id" \| "group_id";', shared
    ), "the key union is gone or has drifted"
    assert re.search(r"const SCRUBBER_STORAGE_KEYS: ScrubberStorageKey\[\] = \[", shared)
    assert "type ScrubRegex = [RegExp, ScrubberStorageKey, string];" in _scrubber(), (
        "the rule table takes any string as a key again"
    )
    # Every key actually used by a rule is in the union.
    keys = set(re.findall(r'\n    "([a-z_]+)",\n    "', _scrubber()))
    declared = set(re.findall(r'"([a-z_]+)"', shared[shared.index("const SCRUBBER_STORAGE_KEYS") : shared.index("function clearFormData")]))
    assert keys <= declared, f"rules store under keys the clear does not know: {keys - declared}"
