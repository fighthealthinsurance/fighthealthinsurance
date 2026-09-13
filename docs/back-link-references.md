# Back links carry a reference, not the case credential

`build_back_url` used to urlencode `(denial_id, email, semi_sekret)` into the
query string of every back link from step 3 onward. That triple is the whole
credential on a case, so from step 3 to step 8 the address bar held a working
key to somebody's medical denial. It went into browser history on a shared
device, into reverse proxy access logs, and into any screenshot or pasted link.

A back link now carries one parameter, `ref`, whose value is
`secrets.token_urlsafe(32)`. The triple it stands for is kept server side in
that browser's session and resolves only there. The code is in
`fighthealthinsurance/views.py`: `issue_denial_ref_token`,
`resolve_denial_ref_token`, `denial_ref_from_query` and
`unresolved_denial_ref_response`.

This file exists so the pull request body and whoever signs off can quote the
two things that are easy to state wrongly: how long the copy of the triple
actually lives, and what the change takes away from patients.

## Retention: it is not twelve hours

`DENIAL_REF_IDLE_TTL_SECONDS` is twelve hours, and that number decides one
thing only: whether a reference still resolves. It is an idle window, pushed
out again on every resolve and every re-render of a link to the same case, so
a person working an appeal across a day keeps the same reference. It is not a
retention period and must not be quoted as one.

The stored copy of the triple lives longer than that, and today it lives
until somebody purges it by hand:

- `Denial` deliberately stores only a hashed email. This scheme puts the
  plaintext address and the case's permanent `semi_sekret` into the session
  store, which is base64 JSON in `django_session` and is not encrypted.
- No `SESSION_ENGINE` is set, so Django's database backend applies
  (`global_settings.py` default `django.contrib.sessions.backends.db`).
- No `SESSION_COOKIE_AGE` is set anywhere in this repo, so Django's two week
  default applies. The row's `expire_date` is stamped from that on each save,
  which means two weeks from the last write, not from the first.
- The database backend stops honouring an expired row. It does not delete it.
  Deletion is what `manage.py clearsessions` does, and nothing runs it: not
  `k8s/`, not `charts/`, not `scripts/`, not `conf/`, not the `Makefile`.

So for somebody who abandons the flow, the plaintext email and the permanent
case secret sit in `django_session` until something purges them, and today
nothing does. The honest sentence is "until the session row is purged, and
nothing purges it".

That is still a trade worth taking. What this removes is a live credential in
the address bar, in history and in access logs, reachable by anyone who gets
the link. What it adds is a row in a database this application already
controls. But the retention job is the fix, it is infrastructure rather than
application code, and it has not been written. It belongs on the follow up
list below, not in a sign off sentence that says "twelve hours".

## What patients lose, and what they are told

The old triple worked in any browser. Somebody could start an appeal on their
phone and open the same link on a laptop, or text the link to themselves. A
reference that resolves only in its own session ends that, which is the entire
point: a link sitting in a history is no longer a key. It is still a behaviour
change, and it lands on a person who is mid appeal.

A reference from another session, an expired one, a tampered one, and an old
style link once the transition window closes all redirect to the upload page
with a `resume` marker, and `scrub.html` explains what happened: a back link
works only in the browser it was made in, it goes stale about twelve hours
after last use, the appeal is still there, reopening the link in the original
browser gets them back to it, and support can help otherwise. Someone who
followed no back link is told nothing, because they have nothing to explain.

Release note for this ship: back links no longer work across browsers or
devices. A link opened on a second phone or computer, or one that has sat
unused for about twelve hours, will not reopen the appeal. It lands on the
upload page with an explanation and a support address instead of a blank form.

## Owner decisions this rests on

Melanie settled two of these on 2026-09-13 and they are not reviewer calls to
reopen:

- The window covers one sitting plus a same day return. Twelve hours idle,
  pushed out on every use, is what that came to. An absolute cap measured
  from first issue was rejected, because it puts a cliff in the middle of an
  active appeal and buys nothing for retention.
- Old style links are accepted for one more release, so the ones already in
  people's browser history keep working. That is item 2 below.

## Follow up list

1. A `clearsessions` job, or an equivalent purge. Until it exists the claim
   above holds and the retention sentence stays as written.
2. Set `LEGACY_DENIAL_REF_QUERY = False` in the next release. While it is True
   an old style link is still a working credential, which is the exposure this
   change exists to end. Links this code builds never contain the triple
   whatever the setting says, so the legacy path cannot be used to lift a
   secret out of a new style link.
3. `SESSION_COOKIE_HTTPONLY = False` and `SESSION_COOKIE_SAMESITE = "None"`
   are set on `Base` and inherited by `Prod` (`settings.py:285-286`). Both
   pre-date this change and neither is touched here, but the reference scheme
   now leans on that cookie, so they are worth a second look.

The referrer angle needs nothing: `SECURE_REFERRER_POLICY` is
`"strict-origin-when-cross-origin"` (`settings.py:1058`), so the reference
does not travel to third parties in `Referer`.

## Why the tests are shaped the way they are

`SessionRequiredMixin` has a session gate that production deliberately leaves
off, and for a while the refusal of an unresolvable back link sat behind it.
Under tox that gate is on, because the test configurations set
`os.environ["TESTING"]`, so a
test could assert a redirect from health history, plan documents or extraction
and pass while production served those same requests a 200 with an empty form
and no word about why. The patient on the second device retyped their
procedure and diagnosis, submitted, and only then got bounced.

The refusal is unconditional now, ahead of the gate rather than behind it, and
the expression it used to hide behind is a named function,
`views.session_gate_enforced`, so a test can assert it is off and then show the
refusal still happens. `ProductionShapedRefusalTest` in
`tests/sync/test_back_url_token.py` removes the gate the way `Prod` does
(`DEBUG = False`, `TESTING` deleted from `os.environ`) and repeats every
refusal across all seven pages a back link can land on. Two of its tests exist
only to prove the removal took, so the rest cannot go green for the wrong
reason.
