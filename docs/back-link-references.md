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

- What this scheme adds is a plaintext copy of the email address and of the
  case's permanent `semi_sekret`, in the session store, which is base64 JSON
  in `django_session` and is not encrypted.
- What the database already holds, for comparison, because an earlier draft of
  this file got it wrong and said `Denial` keeps only a hashed email. It does
  not. `Denial.hashed_email` is stored for every case, `Denial.semi_sekret` is
  stored in plaintext for every case (`models.py`), and
  `Denial.raw_email` is a plaintext `TextField` (`models.py:2387`) holding the
  address for anybody who opted into follow-up contact. So the `semi_sekret` in
  the session is a second copy of something already in plaintext, and the email
  is the part that is genuinely new: the session holds it for everyone who
  walks the flow, opt-in or not, and `email_polling_actor`'s sweep that clears
  `raw_email` after follow-ups are sent does not reach it.
- No `SESSION_ENGINE` is set, on `Prod` or anywhere else, so Django's
  database backend applies (`global_settings.py` default
  `django.contrib.sessions.backends.db`).
- No `SESSION_COOKIE_AGE` is set, on `Prod` or anywhere else, so Django's two
  week default applies. The row's `expire_date` is stamped from that on each
  save, which means two weeks from the last write, not from the first.
- The database backend stops honouring an expired row. It does not delete it.
  Deletion is what `manage.py clearsessions` does, and nothing in this
  checkout runs it, anywhere git can see.

`RetentionClaimTest` pins those bullets, so the paragraph fails out loud
rather than rotting. What each assertion actually covers, stated narrowly
because a test that is believed to cover more than it does is worse than no
test:

- Two assertions read the value that applies, on `Prod` and on the running
  configuration. `django.conf.settings` alone is not enough: the test process
  runs `TestSync`, and setting a cookie age on `class Prod(Base)` alone once
  left every assertion here passing. Neither can answer "is it set", because
  django-configurations copies Django's global defaults into every
  configuration class body, so a third assertion asks that of the text of
  `settings.py` instead.
- One assertion exercises the behaviour rather than the setting: it drives a
  real session through the test client, checks `expire_date` lands two weeks
  out, ages the row by hand, and shows the session store stops honouring it
  while the row itself is still in the table.
- The purge search looks for a `clearsessions` invocation, any use of the
  `Session` model, and a raw `DELETE FROM django_session`, over every file
  `git ls-files --cached --others --exclude-standard` reports. Files a purge
  could be written in are read whole however large; only opaque blobs are
  capped. It cannot rule out a purge that spells the same thing some third
  way, or one that lives outside this repo. Tripwire, not proof.

So for somebody who abandons the flow, the plaintext email and the permanent
case secret sit in `django_session` until something purges them, and today
nothing in this repo does. The honest sentence is "until the session row is
purged, and nothing purges it".

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

`RetentionClaimTest` is shaped for the same reason. A first version read
`settings.SESSION_COOKIE_AGE` and searched five named directories, and a
reviewer got all of it to pass with a six hour cookie age on `class
Prod(Base)` and a `clearsessions` step in `.github/workflows/`: both facts
this paragraph rests on were false and nothing failed. It now reads the
configuration classes directly, tests the expiry behaviour rather than only
the number, and searches every text file git reports. It also asserts that the
listing reached `.github/workflows/ci.yml`, so a listing that comes back short
fails instead of passing empty.

One consequence worth knowing before it surprises somebody: this class fails
on a change to infrastructure rather than to application code. The day a
purge job lands, or a cookie age is set, `tests/sync/test_back_url_token.py`
goes red and the retention paragraph above has to be rewritten in the same
pull request. That is the intent, not an accident, but it means an infra
change carries a docs edit with it.
