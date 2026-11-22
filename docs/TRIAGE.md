# Conversation triage: intent taxonomy and escalation policy

GHL-022. This document and `src/ghl_triage/` are the same artifact in two forms:
the code is the source of truth, and `validate_taxonomy()` fails at import time
if the shape below is broken (an intent without signals, a human-only intent
quietly downgraded, the `unclear` intent losing its human queue).

## 1. Why a taxonomy at all

A triage bot that misroutes confidently is worse than one that escalates: the
customer is told the wrong thing by an automation that sounded certain, and the
error is only discovered when the deal is already lost. So the taxonomy is small
(eight intents), every intent states what it is *not*, and any doubt lands a
message in `human_review` with a recorded reason.

## 2. The eight intents

| Intent | Owner queue | Required entity | Human-only | What it is |
| --- | --- | --- | --- | --- |
| `lead_inquiry` | `leads` | contact name | no | A non-customer asks whether the business can help, before scope or price exists. |
| `quote_request` | `leads` | service | no | A formal price, proposal or scope for defined work is requested. |
| `appointment` | `bookings` | date or time | no | Create, move, cancel or confirm a scheduled slot. |
| `client_support` | `service` | order or account | no | An existing customer reports a problem with work already delivered. |
| `billing` | `billing` | invoice or amount | **yes** | Invoices, charges, refunds, payment methods, disputed amounts. |
| `partnership` | `partnerships` | company | no | A supplier, agency, recruiter or platform pitches to the business. |
| `spam_optout` | `optouts` | — | **yes** | Unsubscribe or stop requests, bulk marketing, messages with no answerable question. |
| `unclear` | `human_review` | — | **yes** | Not confidently separable, too little text, or the classifier could not run. |

Each intent also declares **exclusions** — the specific confusion it must not
absorb. The pairs that matter most:

- `lead_inquiry` ≠ `quote_request`: naming a service, a volume and a deadline is a quote.
- `quote_request` ≠ `billing`: asking what something will cost is not disputing a charge.
- `client_support` ≠ `billing`: "the campaign is broken" is support; "you charged me for it" is billing.
- `appointment` ≠ `quote_request`: a time request is a booking even if the price is unknown.
- `spam_optout` ≠ `client_support`: an angry customer is still a customer.
- `partnership` ≠ `lead_inquiry`: the direction of the sale decides it.

## 3. When a human takes over

`decide()` escalates on any of the following, and reports every applicable
reason in a fixed precedence order so the same message always produces the same
plan:

| Reason | Trigger | Default threshold |
| --- | --- | --- |
| `classifier_error` | The classifier raised, timed out or returned an intent outside the taxonomy. | always |
| `empty_message` | No usable text after whitespace stripping. | always |
| `human_only_intent` | Intent is `billing`, `spam_optout` or `unclear`. | always |
| `human_requested` | The contact asked for a person ("speak to a human", "real person", "stop the bot"). | on |
| `missing_entity` | An entity the intent requires is absent (e.g. a booking with no date). | always |
| `low_confidence` | Classifier score below the floor. | `< 0.55` |
| `repeated_contact` | Message count at or above the threshold in the thread. | `>= 3` |
| `already_escalated` | The thread was already handed over; it stays handed over. | always |

Escalation sends the message to `human_review`; the *classified* intent is still
recorded on the plan so the human sees what the automation thought.

## 4. Failure behaviour

- **Fail closed.** `fail_closed()` produces `unclear` / confidence `0.0` /
  `human_review`, and only accepts `classifier_error` or `empty_message` as a
  reason — a failure may never be dressed up as a confident routing decision.
- **Never auto-send.** `TriagePlan.auto_reply_allowed` is `False` for every plan
  the module can produce, including the clean happy path. `draft_allowed` is
  `True` only when the message is *not* escalated, and a draft still needs human
  approval before anything leaves the system — the same approval model as the
  governed MCP tool surface (`docs/MCP.md`).
- **Bad input is rejected, not coerced.** A confidence outside `[0, 1]` raises;
  a policy with a nonsensical floor or repeat threshold raises at construction.

## 5. Not in this task

- The classifier itself (GHL-023) — this task defines the labels and the routing
  contract, not the scorer.
- The labelled evaluation set and accuracy/cost/latency reporting (GHL-024).
  Until that harness exists, no accuracy figure may be quoted anywhere,
  including on `marvis.web.app`.

## 6. Running it

```bash
python -m pytest tests/test_triage_taxonomy.py tests/test_triage_escalation.py -q
```

39 tests: taxonomy shape and invariants, all eight escalation reasons, reason
precedence, fail-closed behaviour, and a parametrised check that no intent can
ever auto-reply.
