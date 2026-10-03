# Back links carry a reference, not the case credential

The triple `(denial_id, email, semi_sekret)` is the whole credential on a
case, so it never goes in a link's query string, where it would reach browser
history on a shared device, reverse proxy access logs, and any screenshot or
pasted link.

A back link carries one parameter, `ref`. Its value is the case id and
the case's permanent secret, encrypted (Fernet) with a key derived from the
site secret and a random secret kept in that browser's session, so it decrypts
only there; it carries its own minting time, and stops resolving twelve hours
after it. The email the later pages post is kept in the session, per case,
written when the case is started in that session. Nothing else is stored, so
two requests minting at once have nothing to lose to each other's save. The
code is in `fighthealthinsurance/views.py`: `issue_denial_ref_token`,
`resolve_denial_ref_token`, `remember_denial_ref_email`,
`denial_ref_from_query` and `unresolved_denial_ref_response`.

`ref` is the only way a GET to a page in the appeal flow reaches a case. A
query that names the case directly, with `denial_id`, `email` and
`semi_sekret`, opens nothing on any of those pages, and neither does a bare
`denial_id`. The forms in the flow post the triple in hidden fields, which
keeps it in request bodies and out of the address bar.

This file exists so the pull request body and whoever signs off can quote the
two things that are easy to state wrongly: how long the copy of the triple
actually lives, and what the change takes away from patients.

## Retention: it is not twelve hours

`DENIAL_REF_IDLE_TTL_SECONDS` is twelve hours, and that number decides one
thing only: whether a reference still resolves. A reference carries its own
minting time, and every page render mints fresh ones for the links it shows,
so a person working an appeal across a day always holds links minted within
the last twelve hours. An old link in a history keeps the window it was minted
under. It is not a retention period and must not be quoted as one.

What the session holds lives longer than that: until two weeks after the
session was last saved, and then until the next daily purge deletes it.

- A reference is not stored. It is the case id and its permanent
  `semi_sekret`, encrypted with a key derived from the site secret and a
  random secret kept in the session, so it decrypts only in the browser that
  minted it. The session keeps that random secret, and, per case, a plaintext
  copy of the email address the later pages post, in the session store, which
  is base64 JSON in `django_session` and is not encrypted. The `semi_sekret`
  is not in the session.
- What the database already holds, for comparison, because an earlier draft of
  this file got it wrong and said `Denial` keeps only a hashed email. It does
  not. `Denial.hashed_email` is stored for every case, `Denial.semi_sekret` is
  stored in plaintext for every case (`models.py`), and
  `Denial.raw_email` is a plaintext `TextField` (`models.py`) holding the
  address for anybody who opted into follow-up contact. So the email in the
  session is the part that is genuinely new: the session holds it for everyone
  who walks the flow, opt-in or not. `email_polling_actor`'s sweep that
  clears `raw_email` after follow-ups are sent does not reach it; the session
  purge below does.
- No `SESSION_ENGINE` is set, on `Prod` or anywhere else, so Django's
  database backend applies (`global_settings.py` default
  `django.contrib.sessions.backends.db`).
- No `SESSION_COOKIE_AGE` is set, on `Prod` or anywhere else, so Django's two
  week default applies. The row's `expire_date` is stamped from that on each
  save, which means two weeks from the last write, not from the first.
- The database backend stops honouring an expired row and leaves it in the
  table. `EmailPollingActor._clear_expired_sessions` deletes it: it runs the
  session engine's `clear_expired`, the same purge as
  `manage.py clearsessions`, on the actor's first pass and every 24 hours
  after, and logs only how many rows it removed. The actor checks it at the
  top of each pass of its loop, before any email work, and once a minute
  through the loop's long waits, so neither a failing follow-up step nor the
  pacing between follow-up sends holds it up. The actor runs in production
  whether or not `TEMPORAL_ENABLED` is on (`polling_actor_setup.py`). Each
  deploy recreates it, so each deploy starts with a purge, and
  `reconcile_polling_actors` relaunches it if it goes missing.

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
- Two assertions exercise behaviour rather than settings. One saves a real
  session, checks `expire_date` lands two weeks out, ages the row by hand,
  and shows the session store stops honouring it while the row itself is
  still in the table. The other runs the actor's purge on an aged row and
  shows the row is gone. When the purge runs, that a failure in it leaves
  the expired email clearing alone, and that the email work does not hold it
  up, is tested with the actor in
  `tests/async-unit/test_expired_sessions_are_cleared.py`.
- The purge search looks for a `clearsessions` invocation, a `clear_expired`
  call, any use of the `Session` model, and a raw `DELETE FROM django_session`,
  over every file `git ls-files --cached --others --exclude-standard` reports
  outside `tests/`, and expects exactly one:
  `fighthealthinsurance/email_polling_actor.py`. Files a purge could be
  written in are read whole however large; only opaque blobs are capped. It
  cannot rule out a purge that spells the same thing some third way, or one
  that lives outside this repo. Tripwire, not proof.

So for somebody who abandons the flow, the plaintext email and the session's
random secret stay in `django_session` for two weeks after the session was
last saved, and the next daily purge deletes them. The honest sentence is
"two weeks after the session was last saved, then deleted within a day", not
"twelve hours".

That is a trade worth taking. What this removes is a live credential in the
address bar, in history and in access logs, reachable by anyone who gets the
link. What it adds is a row in a database this application already controls,
kept for two weeks after the session was last saved and then deleted.

## What patients lose, and what they are told

A reference resolves only in its own session, so a back link does not carry
an appeal to another browser or device. Somebody who starts on their phone
cannot open the same link on a laptop, or text it to themselves. That is the
point: a link sitting in a history opens nothing. It still lands on a person
who is mid appeal, so the failure has to explain itself.

A reference from another session, an expired one, a tampered one, and a link
that names the case in its query string all redirect to the upload page
with a `resume` marker, and `scrub.html` explains what happened: a back link
works only in the browser it was made in, it goes stale about twelve hours
after last use, the appeal is still there, reopening the link in the original
browser gets them back to it, and support can help otherwise. Someone who
followed no back link is told nothing, because they have nothing to explain.

Release note for this ship: back links no longer work across browsers or
devices. A link opened on a second phone or computer, or one that has sat
unused for about twelve hours, will not reopen the appeal. It lands on the
upload page with an explanation and a support address instead of a blank form.

## The way back from a cancelled fax payment

Stripe's `cancel_url` for a fax payment uses the same scheme. When Fax My
Appeal stages a fax, `StageFaxView` mints a reference with
`views.issue_fax_cancel_ref`: the staged fax's uuid and hashed email,
plus the two fax-form choices the staged fax does not keep (the insurer name
as typed and whether the health history went), encrypted with the same
session key and carrying its own minting time. The person's name is never in
it, so they type it again. Stripe
holds that address, and it reaches analytics and access logs once the person
is sent back, so it carries no id. `FaxPaymentCancelledView` shows the letter
only when the reference decrypts in this session and names a staged fax with
that uuid and hashed email. Anything else gets a page with no letter on it.
A reference resolves for one day (`FAX_CANCEL_REF_TTL_SECONDS`), because
Stripe keeps a checkout page open for up to a day. Nothing new is stored in
the session: it uses the random secret a back link uses, minted if the
session has none yet.

## The way back from the "you left before finishing" email

The intake journey's one reminder email (sent 24 hours in, only to people
who asked us to keep their email) links back to the case at the step it
reached. A back link works only in the browser that made it, and an email
is opened somewhere else, so this link is its own scheme, in
`fighthealthinsurance/intake_resume.py`:

- It is only for a case the person started on the patient form. A case a
  professional created, holds or was added to gets no reminder and no link,
  and no link opens one.
- The link is `/continue/<token>`, where the token is 32 random bytes. It
  holds no email address, hashed email, case id, uuid or case secret, and
  nothing can be decoded from it.
- The server stores only the token's SHA-256 digest, on the case's
  `IntakeResumePoint` row. The token itself is only in the email.
- It opens nothing on its own. The page asks for the email address the case
  was started with and compares its hash with the case's. Five wrong
  addresses revoke the link, however many tries arrive at once.
- It works for 48 hours from minting (`RESUME_LINK_TTL`): the nudge goes out
  at 24 hours and the journey closes at 3 days, and closing deletes the link
  too. It also stops working once the case reaches a later step, once the
  form is finished, and when the person deletes their data, because the row
  goes with the denial. Until then it can be opened again, from a second
  device too.
- `/continue/<token>` moves the digest into the session and redirects to
  `/continue`, so no page that renders, and nothing it loads, has the token
  in its address.

Once the address matches, the session key is cycled, the session is bound
to the case the way starting it binds it (so the session holds the email
the later pages post, as described above), and the person lands on their
step through an ordinary back link. Nothing here runs while the intake
journey is off: no step is recorded, no link is minted, and both pages
answer 404.

## Owner decisions this rests on

Melanie settled this on 2026-09-13 and it is not a reviewer call to reopen:

- The window covers one sitting plus a same day return. Twelve hours from
  minting is what that came to, with every page render minting the links it
  shows afresh, so a person who keeps working keeps holding fresh links. An
  absolute cap measured from the first link was rejected, because it puts a
  cliff in the middle of an active appeal and buys nothing for retention.

## Follow up list

1. `SESSION_COOKIE_HTTPONLY = False` and `SESSION_COOKIE_SAMESITE = "None"`
   are set on `Base` and inherited by `Prod` (`settings.py`). Both
   pre-date this change and neither is touched here, but the reference scheme
   now leans on that cookie, so they are worth a second look.

The referrer angle needs nothing: `SECURE_REFERRER_POLICY` is
`"strict-origin-when-cross-origin"` on `Prod` (`settings.py`), so the reference
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
`tests/sync/test_back_url_token.py` turns the gate off as `Prod` has it
(`DEBUG = False`, no `TESTING` in `os.environ`) and repeats every
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
fails instead of missing a purge in a file it never read.

One consequence worth knowing before it surprises somebody: this class can
fail on a change to infrastructure rather than to application code. The day a
second purge lands (a `clearsessions` CronJob, say), the purge moves out of
`EmailPollingActor`, or a cookie age is set,
`tests/sync/test_back_url_token.py` goes red and the retention paragraph above
has to be rewritten in the same pull request. That is the intent, not an
accident, but it means an infra change carries a docs edit with it.
