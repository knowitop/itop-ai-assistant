# Telemetry

The assistant sends one anonymous document a day about the installation it runs in: counts of what it did, which modules are on, and the versions it runs. It is on by default and switched off in one click.

This page describes that document in full, but the installation itself is the authority on it. **System → Show today's document** prints the exact JSON that would leave today, and `GET /api/telemetry/preview` returns the same thing to a script; both go through the code the sender uses. If this page and that document ever disagree, the document is right.

The preview answers with telemetry switched off as well — "show me what would go out if I turned this on" is a question asked before turning it on.

---

## Why it exists

You install this yourself, on your own servers, in a configuration we have never seen. That is the deployment model on purpose — but it means we cannot see a single thing about how the product actually runs unless you tell us.

Without that, decisions get made on guesses: which LLM providers to support properly, which of the twelve interface languages to keep current, whether the vector layer earns its complexity, what to fix next. A wrong guess costs a month of work on the wrong thing.

There is no other channel. The project is open source and has no sales team calling customers, no license server, no phone-home besides this. Fifteen integers a day is what we get instead.

---

## What is sent

One document per installation per UTC day: a day's counters, not a stream of events. A counter has no room for ticket text.

### Build

| Field | What it is |
|---|---|
| `version` | The release this installation runs |
| `commit` | The commit that release was built from |
| `python_version` | Major and minor only — `3.13`, never the patch |
| `containerized` | Whether it runs in a container |

### Environment

| Field | What it is |
|---|---|
| `qdrant` | Whether a Qdrant URL is configured |
| `vector_available` | Whether the vector layer actually answers — not the same question |
| `admin_language` | The language the admin UI was last used in, or absent if nobody has opened it |
| `utc_offset_minutes` | The installation's offset from UTC, as a number. It cannot name a city |

Country is **not** a field, and nothing derives one on arrival either — the receiver does that for signals from its web SDK, and these are not those. Where an installation is, we do not know; the nearest thing in the document is an offset from UTC.

Redis is not a field either: the document is assembled out of Redis, so the field could never hold `false`.

### Configuration

| Field | What it is |
|---|---|
| `dry_run` | Whether the installation is in [dry run](configuration.md#dry-run) |
| `llm_provider` | One of the providers this build ships, or `other` |
| `llm_model` | The model name, if it has the shape of a model name, else `other` |
| `settings` | Every `bool` and `int` any module's settings section declares |

`settings` is a rule, not a list. Every configurable section is scanned, and each field whose declared type is `bool` or `int` travels under `<section>_<field>` — today that is `intake_enabled`, `intake_max_questions`, `intake_max_iterations`, `intake_classify_enabled`, `intake_max_classify_questions`, `intake_clarify_enabled`, `intake_handoff_note_enabled`, `intake_similar_enabled`, `intake_similar_max_age_days`, `intake_similar_candidates`, `intake_similar_top`, `vector_enabled`, `vector_sweep_interval_seconds`, `vector_sweep_page_size`, `vector_reconcile_interval_days`, `vector_max_chunk_tokens`, `vector_log_entries_per_chunk`.

A module's OQL, its class list, its note templates cannot appear here at all: they are neither a `bool` nor an `int`.

One consequence worth knowing, because it is visible in the document: a module switched off in the environment before startup (`INTAKE_ENABLED=false`) is never registered, so **none** of its fields appear — not even `intake_enabled: false`. Switched off later through the admin UI, it stays registered and reports `intake_enabled: false` like any other value. So an absent group means "not running here", by either route; it does not distinguish a module this build does not have from one this installation never started.

### Activity

Fifteen counters, for the day the document covers. Every one of them is present in every document, including the zeros.

| Counter | What it counts |
|---|---|
| `runs_webhook` | Runs started by an iTop webhook |
| `runs_request` | Runs started by an explicit request |
| `runs_schedule` | Runs started on a timer |
| `runs_failed` | Runs that ended in an error |
| `runs_skipped` | Runs that stopped before doing anything — lock held, object gone, or the module's own guard said no |
| `itop_public_comment` | Public log entries written to iTop |
| `itop_private_note` | Private notes written to iTop |
| `itop_field_update` | Ticket field changes written to iTop |
| `llm_calls` | Calls to the model |
| `llm_failures` | Calls to the model that failed |
| `llm_tokens_in` | Prompt tokens |
| `llm_tokens_out` | Completion tokens |
| `vector_searches` | Similarity searches |
| `vector_searches_empty` | Similarity searches that found nothing |
| `vector_chunks_embedded` | Text chunks sent for embedding |

---

## What is never sent

What the document cannot contain, under our agreement with the receiver:

- **Ticket content of any kind** — subjects, descriptions, resolutions, public comments, private notes.
- **Names** — of people, of organizations, of teams.
- **Addresses** — URLs, hostnames, IP addresses, endpoint addresses, your iTop's location.
- **Credentials** — keys, tokens, passwords.
- **iTop object identifiers** — no ticket id, no person id, no organization id.

Two rules hold that line, neither of which depends on discipline at the call site.

**A value that is not recognized becomes `other`.** The provider is an enumeration: a value this build does not ship travels as `other`. The model name cannot be an enumeration, so it is checked by shape — a name shaped like a model id travels, `qwen3-32b-final-from-Pete` does not, and neither does anything with a comment appended to it, in any alphabet.

**Everything else is a number or a flag.** The configuration group admits only `bool` and `int` values, the activity group only integers. Prose has nowhere to go.

Error messages, stack traces and model traces are not sent either. An exception message carries ticket content freely — a model's answer inside a validation error, an iTop response body inside a parse error — so they would need a switch of their own; there is none today, and nothing of the kind leaves.

---

## Who receives it

**TelemetryDeck GmbH**, Augsburg, Germany. Signals are posted to `nom.telemetrydeck.com`.

- [Privacy policy](https://telemetrydeck.com/privacy/)
- [Terms](https://telemetrydeck.com/terms/)
- [Data processing agreement](https://telemetrydeck.com/dpa/)
- [How their anonymization works](https://telemetrydeck.com/docs/articles/anonymization-how-it-works/)
- [Privacy FAQ — what is collected, and what is not](https://telemetrydeck.com/docs/guides/privacy-faq/)

### What the receiver does

These are their commitments and not ours, so read them at the source rather than take this summary for them:

- **IP addresses are not stored** — not in their database, not in their logs, nowhere. They do read the address of a *web* request far enough to name a country, but the documents this service sends go to the plain ingest API, which does no such lookup: no country is derived from ours, and none is stored.
- **The identity field is hashed again on arrival**, with their own salt, so that neither they nor we can reverse it.

### The installation id

Your installation has an anonymous id — a random value it generated for itself on first start, out of nothing. Not derived from your iTop URL, not from your organization name, not from a key: if it were derived from anything, it would be a fingerprint of that thing.

It is shown on the **System** screen, and it travels in the document as an ordinary field — which is what makes the next section possible at all.

It lives in Redis, the only state this service owns. Reset Redis and the installation gets a new id, and counts as a new installation from then on.

### "Delete my data"

Two routes, and the second one does not depend on anyone's goodwill.

1. **Ask.** Send us your installation id — from the System screen — and we will file the deletion request with the receiver. Their dashboard can filter by that field, so the data of one installation can be found; deleting it selectively is a support request on their side rather than a button on ours.
2. **Switch it off and wait.** Data on our plan is retained for three months. Turn telemetry off and everything about your installation ages out within that window, with nobody's cooperation required.

### It is a one-way channel

The receiver sends nothing back to your installation. No update notifications, no configuration, no commands — there is no code path for it to answer on.

---

## Switching it off

**System → Anonymous usage telemetry.** Applies immediately, no restart. Or set `TELEMETRY_ENABLED=false` before starting the container — the switch in the UI outranks it either way, so a deployment default never locks an administrator out of turning it off.

With it off, nothing leaves: no connection is opened to the receiver, and its name is not even resolved.

What does continue is the counting. The daily counters keep being written into Redis, where they expire after three days, because they are cheap and the switch may be turned back on. They simply never go anywhere.

### Builds that never send anything

Telemetry reports from **released builds only** — the images we publish from a git tag. A checkout you cloned and ran with `uv sync`, or an image you built yourself, sends nothing regardless of the switch, and the System screen says so.

This is a stamp, not a guess: the workflow that publishes an image marks the artifact as a release build, and nothing else does. It is not read off the version number, so a checkout sitting exactly on a tag is still not a published image.

This keeps developers' machines out of the count of installations. But an installation deployed from source onto a real server is not a developer's machine, and nothing in the build can tell the two apart — so if that is you, say so:

```
TELEMETRY_ALLOW_UNPUBLISHED_BUILD=true
```

That installation then reports like any other, and counts like any other. Left unset, it never reports and is never counted.

`TELEMETRY_TEST_MODE=true` is a different switch and does not unlock anything: it marks whatever is sent as a test signal, so it stays out of our product numbers. Our own verification stand sets both — it runs an unpublished build, and its numbers are not real.

---

## When the first document is sent

A fresh installation sends its first document **when the setup wizard is finished** — which is why the wizard says so on its welcome screen, before a single setting is saved, with the switch right there. That first document covers a partial day and is the only one that ever does.

An installation that was upgraded rather than newly set up has no such moment, so it waits for the ordinary cycle: yesterday's day, whole, and never sooner than 24 hours after the installation was first seen.

After that, one document a day. If the receiver is unreachable, that day is lost — there is no queue, no retry tomorrow, and no error anywhere in your interface. Telemetry is not allowed to become an incident in your production, and one installation missing one day changes no conclusion we would draw.
