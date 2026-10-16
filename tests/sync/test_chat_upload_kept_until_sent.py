"""A chat upload is never thrown away because the connection was down.

The chat reads a document on the person's device, then sends the text over
the chat's WebSocket. If that socket wasn't open when reading finished, the
document used to be dropped without a word: the "I've uploaded a document"
bubble and the spinner still appeared, but nothing was sent, and the
eventual Retry sent the bubble's own text instead of the document. With no
socket at all, picking a file did nothing.

Now the read document is kept, the person is told it hasn't gone, and Retry
sends it. Nothing is sent until the history replay that every reconnect asks
for has landed, since the replay replaces the message list. A document that
finishes reading after New Chat, or after a newer file was picked, is
dropped.

Only one retry can be in flight at a time. The server answers a
connection's turns in order, so a retry stays in flight until the socket
owes no more replies (or closes), not merely until the first reply lands.

There is no harness that runs the React chat component, so these read the
source, like ``tests/async-unit/test_appeal_sends_what_was_edited.py``.
"""

import pathlib
import re

CHAT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
    / "chat_interface.tsx"
)


def _source() -> str:
    return CHAT.read_text()


def _brace_block(src: str, open_at: int) -> str:
    assert src[open_at] == "{", src[open_at : open_at + 20]
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def _block_after(src: str, head: str) -> str:
    """The brace block opened by the one occurrence of ``head``."""
    assert src.count(head) == 1, f"expected one {head!r}, found {src.count(head)}"
    return _brace_block(src, src.index(head) + len(head) - 1)


def _arrow_body(src: str, name: str) -> str:
    """The body of ``const name = ... => {`` (a useCallback wrapper too)."""
    heads = list(re.finditer(r"\bconst\s+" + re.escape(name) + r"\s*=", src))
    assert len(heads) == 1, f"expected one `const {name}`, found {len(heads)}"
    arrow = src.index("=>", heads[0].end())
    return _brace_block(src, src.index("{", arrow))


def _arrow_expression(src: str, name: str) -> str:
    """The text of ``const name = ...;`` for an expression-bodied arrow."""
    head = re.search(r"\bconst\s+" + re.escape(name) + r"\b[^=]*=", src)
    assert head is not None, f"no `const {name}`"
    return src[head.end() : src.index(";", head.end())]


def _socket_handler(src: str, event: str) -> str:
    head = re.search(r"\bws\.on" + event + r"\s*=\s*\([^)]*\)\s*=>\s*\{", src)
    assert head is not None, f"the socket no longer has an on{event} handler"
    return _brace_block(src, head.end() - 1)


def test_an_upload_is_read_even_when_there_is_no_socket():
    body = _arrow_body(_source(), "handleFileUpload")
    assert "wsRef" not in body, (
        "the upload handler looks at the socket itself again, so it can skip "
        "or drop a document without going through sendUpload"
    )


def test_an_upload_that_cannot_send_is_kept_and_the_person_told():
    body = _arrow_body(_source(), "handleFileUpload")
    branch = _block_after(body, "if (!sendUpload(upload)) {")
    assert "unsentUploadRef.current = upload;" in branch, (
        "the read document is thrown away when it can't be sent"
    )
    assert re.search(r"error:\s*`[^`]*wasn't sent[^`]*Retry", branch), (
        "the person isn't told the document didn't send and that Retry sends it"
    )


def test_an_upload_is_sent_only_on_a_ready_socket():
    body = _arrow_body(_source(), "sendUpload")
    assert re.match(
        r"\{\s*(?://[^\n]*\n\s*)*const ws = readySocket\(\);\s*"
        r"if \(!ws\) \{\s*return false;\s*\}",
        body,
    ), "sendUpload no longer refuses before the socket is ready"


def test_the_upload_shows_in_the_chat_only_after_it_is_sent():
    body = _arrow_body(_source(), "sendUpload")
    sent = body.find("ws.send(")
    bubble = body.find("I've uploaded a document")
    assert -1 < sent < bubble, (
        "the upload bubble and spinner can appear for a document that never went"
    )


def test_a_sent_upload_is_no_longer_kept_for_retry():
    body = _arrow_body(_source(), "sendUpload")
    sent = body.find("ws.send(")
    cleared = body.find("unsentUploadRef.current = null;")
    assert -1 < sent < cleared, (
        "a document that went is still kept, so Retry would send it again"
    )


def test_nothing_is_sent_before_the_socket_is_open_and_replayed():
    ready = _arrow_expression(_source(), "readySocket")
    assert "wsRef.current.readyState === WebSocket.OPEN" in ready, (
        "a socket that isn't open counts as ready"
    )
    assert "!replayPendingRef.current" in ready, (
        "a send can go out before the history replay that would wipe it from view"
    )


def test_messages_and_uploads_both_wait_for_a_ready_socket():
    src = _source()
    for name in ("sendChatMessage", "sendUpload"):
        body = _arrow_body(src, name)
        assert "readySocket()" in body and "wsRef" not in body, (
            f"{name} sends without asking whether the socket is ready"
        )


def test_the_replay_is_pending_from_its_request_until_it_lands():
    src = _source()
    onopen = _socket_handler(src, "open")
    asks = onopen.find("replayPendingRef.current = true;")
    replay_send = onopen.find("replay: true")
    assert -1 < asks < replay_send, "the replay request isn't marked pending"
    replayed = _block_after(src, "} else if (data.messages) {")
    assert "replayPendingRef.current = false;" in replayed, (
        "a landed replay keeps every send blocked"
    )
    failed = _block_after(src, "if (data.error) {")
    assert "replayPendingRef.current = false;" in failed, (
        "a replay that fails keeps every send blocked"
    )


def test_a_document_read_after_new_chat_or_a_newer_file_is_dropped():
    src = _source()
    body = _arrow_body(src, "handleFileUpload")
    assert "const generation = ++uploadGenerationRef.current;" in body, (
        "a new file doesn't outdate one still being read"
    )
    read = body.find("await recognize(")
    dropped = body.find("if (!current()) return;")
    sent = body.find("sendUpload(upload)")
    assert -1 < read < dropped < sent, (
        "a document that finished reading late is still sent or kept"
    )
    assert "uploadGenerationRef.current += 1;" in _arrow_body(src, "startNewChat"), (
        "New Chat doesn't outdate a document still being read"
    )


def test_retry_sends_a_kept_upload_before_anything_else():
    body = _arrow_body(_source(), "handleRetryLastMessage")
    held = body.find("unsentUploadRef.current")
    sent = body.find("sendUpload(unsentUpload)")
    resend_text = body.find("sendChatMessage(")
    assert -1 < held < sent < resend_text, (
        "Retry resends the last message's text instead of the kept document"
    )


def test_retry_while_disconnected_says_so_instead_of_doing_nothing():
    body = _arrow_body(_source(), "handleRetryLastMessage")
    assert not re.search(r"if \(!wsRef\.current\) return;", body), (
        "Retry silently does nothing while the socket is gone"
    )


def test_a_second_retry_cannot_start_while_one_is_in_flight():
    body = _arrow_body(_source(), "handleRetryLastMessage")
    first_statement = re.search(r"\{\s*(?://[^\n]*\n\s*)*(?P<stmt>[^\n]+)", body)
    assert first_statement is not None
    assert first_statement.group("stmt").strip() == (
        "if (retryInFlightRef.current) return;"
    ), "Retry no longer refuses while a retry is still in flight"
    assert body.count("setRetryInFlight(true);") == 2, (
        "a retry that went out (document or message) isn't marked in flight"
    )


def test_every_turn_sent_is_counted_as_owed_a_reply():
    src = _source()
    for name in ("sendChatMessage", "sendUpload"):
        body = _arrow_body(src, name)
        sent = body.find("ws.send(")
        owed = body.find("repliesOwedRef.current += 1;")
        assert -1 < sent < owed, f"a turn sent by {name} isn't counted as owed"
    onopen = _socket_handler(src, "open")
    assert onopen.count("repliesOwedRef.current += 1;") == 2, (
        "the opening message a landing page sends isn't counted as owed"
    )


def test_every_reply_to_a_turn_is_counted():
    src = _source()
    answered = _block_after(src, "if (data.content && data.role) {")
    assert "noteReply();" in answered, "an answer doesn't pay off its turn"
    failed = _block_after(src, "if (data.error) {")
    assert re.search(
        r"if \(replayPendingRef\.current\) \{\s*replayPendingRef\.current = false;"
        r"\s*\} else \{\s*noteReply\(\);\s*\}",
        failed,
    ), "an error doesn't pay off its turn, or pays one off for the replay"


def test_a_retry_is_in_flight_until_no_reply_is_owed():
    src = _source()
    body = _arrow_body(src, "noteReply")
    cleared = _block_after(body, "if (repliesOwedRef.current === 0) {")
    assert "setRetryInFlight(false);" in cleared, (
        "a retry stays locked after every owed reply has landed"
    )
    assert body.count("setRetryInFlight(false);") == 1, (
        "a reply clears the retry while the socket still owes the retry's own"
    )
    assert not re.search(
        r"if \(!state\.isLoading\)\s*\{\s*setRetryInFlight\(false\)", src
    ), "the first reply to land ends the retry, even when it answers an earlier turn"


def test_a_retry_stops_counting_as_in_flight_when_the_socket_closes():
    onclose = _socket_handler(_source(), "close")
    for reset in (
        "repliesOwedRef.current = 0;",
        "replayPendingRef.current = false;",
        "setRetryInFlight(false);",
    ):
        assert reset in onclose, f"a closed socket leaves `{reset}` undone"


def test_both_retry_buttons_show_a_retry_in_flight():
    buttons = [
        b
        for b in re.findall(r"<Button\b[\s\S]*?</Button>", _source())
        if "onClick={handleRetryLastMessage}" in b
    ]
    assert len(buttons) == 2, f"expected two Retry buttons, found {len(buttons)}"
    for button in buttons:
        assert "loading={retryInFlight}" in button, (
            "a Retry button looks ready while a retry is still in flight"
        )


def test_a_new_chat_drops_the_old_chats_unsent_upload():
    body = _arrow_body(_source(), "startNewChat")
    assert "unsentUploadRef.current = null;" in body, (
        "a new chat's Retry could send the previous chat's document"
    )
