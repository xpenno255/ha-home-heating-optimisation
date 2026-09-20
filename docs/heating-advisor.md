# Heating Advisor (0.5.2)

The optional advisor produces daily summaries, reviews of the configured historical
window, and investigations. It does not change heating settings. Every task selects
an existing Home Assistant **AI Task** profile. Conversation agents are not supported
in this first version because they can carry unrelated context and tools.

## Configure a provider profile

Supported providers are Extended OpenAI Conversation and Home Assistant's built-in
Anthropic integration. They own credentials, endpoints, models, output token limits
and supported thinking settings. The heating integration never stores an API key.
Create separate profiles for different models/efforts, then assign them per task.
The report records the selected profile's model and explicit effort settings. A
provider default is reported as such, not guessed; settings are read at call time.

For Extended OpenAI, add an **AI Task** subentry to your existing local connection:

- Name it Heating Advisor.
- Select the model served by your endpoint, with an output allowance suitable for
  reports (1,500 tokens is a tested starting point for the local Gemma deployment).
- Set Home Assistant APIs to none and **Functions to the literal `[]`**. An empty
  field inherits Extended OpenAI's default tools and is rejected.
- Extra body must be empty, `{}`, or a literal JSON object containing only
  `chat_template_kwargs.enable_thinking` with a boolean value. Templates and arbitrary
  request overrides are not accepted. Model support for thinking remains provider-owned.

For Claude, add Anthropic under Devices & services, enter the key there, and add an
AI Task profile with the chosen model and supported thinking settings. Disable
Home Assistant APIs, web search/fetch, code execution and tool search. Anthropic
profiles use task-specific heating instructions supplied by HHO on each request;
a separate conversation-agent system prompt is not required. Provider defaults and
model availability can change, so select explicit settings for comparisons.

Profiles are checked before calls. Unsupported providers, enabled tools and missing
profiles prevent a call. No profile is selected by default and there is no fallback
to another provider. A provider configuration change can affect subsequent reviews.

## Enable and run

Integration options now offer **Room, system and survey mappings** or **Heating
Advisor**, so AI settings can be changed without repeating room configuration.
Enable historical analytics first. Select profiles independently for daily summary,
weekly review and investigation. Enabling the advisor authorises sending its selected
evidence to those profiles; choosing a cloud profile sends that data to its provider.

```yaml
action: home_heating_optimisation.run_review
data:
  task: investigation
  question: "What should I check before changing any heating settings?"
response_variable: heating_review
```

Other task values are `daily_summary` and `weekly_review`. A weekly review uses the
configured analysis window, which may be 3–14 days; its actual dates are included.
Manual tasks require the advisor to be enabled and that task's profile to be selected.

```yaml
action: home_heating_optimisation.get_advisor_reports
response_variable: heating_reports
```

The list includes the latest 20 reports and available profile settings. Supply a
`report_id` to retrieve that report's exact evidence, hash and prompt version.
The **Advisor status** sensor exposes status/count/timestamp only. Full report text
stays out of entity attributes and downloadable diagnostics. Conversational
follow-ups remain later work; the reader and notifications below were added in
September 2026 (issue #18).

## Reading reports

`get_advisor_report_summary` returns one retained report in readable form. Omit
`report_id` for the latest report; an unknown or deleted ID returns a validation
error and never triggers a new AI call.

```yaml
action: home_heating_optimisation.get_advisor_report_summary
data:
  report_id: optional-id-from-get_advisor_reports
response_variable: heating_report
```

The response contains rendered Markdown in `text` plus structured fields: task and
`created_at`, profile name/provider/model/effort, `prompt_version`, `evidence_hash`,
the fact IDs cited (`evidence_references`), a `coverage` block (analysis window,
system and per-room coverage percentages, unavailable/suppressed metrics, omitted
evidence categories), the report's `conclusion` and `summary`, `findings` (title,
kind, detail, evidence IDs, next check), `limitations` and `follow_up_actions`
(one per finding). Coverage is described by reference: the summary reports
percentages and metric names, not the evidence values themselves. Retrieve the full
evidence with `get_advisor_reports` when you need to check a citation.

Show `{{ heating_report.text }}` in a Markdown card or script, or read the fields in
an automation. The **Advisor latest report** diagnostic sensor
(`sensor.home_heating_optimisation_advisor_latest_report`) has the latest report's
creation time as its state and only `report_id`, `task`, `profile_name`,
`finding_count`, `limitation_count` and `headline` (the first finding's title,
at most 120 characters) as attributes. No report text, evidence or private notes
appear in entity attributes or diagnostics.

## Notifications

Notifications are off by default. In the Heating Advisor options step:

- **Send notifications** enables them.
- **Notification services** lists the available `notify.*` services (multi-select).
  Leave it empty to receive a Home Assistant persistent notification instead.
- **Notify on** selects events: report ready (default), report failed (timeout or
  rejected response) and provider call failed.
- **Include finding titles in notifications** adds finding titles only. Details,
  next checks, limitations, evidence and questions are never sent by default or
  with this option; the message carries the report ID, task, profile name,
  conclusion and counts, plus the action name to retrieve the report.

Deduplication is persisted in the advisor store: a report is never announced twice,
including across restarts, and failure notifications are sent at most once per task
per local day. A failure notification never causes a retry; the next call happens
only when you or a schedule requests it. Delivery failures (missing or failing
notify service, timeout) are logged at warning level and shown as `notify_status`
on the Advisor status sensor; they never affect the review result or heating
control. When a journal is enabled, each retained report also records an
`advisor_report` event with the report ID, task, profile name and finding count.

## Schedules and limits

Daily and weekly schedules are off by default. Enable each separately and choose
a local hour (09:00 by default). Weekly reviews run on Monday. The advisor attempts
each task once during that hour, with a one-minute minimum gap between calls. It
can catch up after a restart within the same hour; missed hours are not replayed.
Persisted scheduled dates prevent duplicate calls after a restart or daylight-saving
clock repetition. Unavailable prerequisites can be checked again during the hour,
but a started provider call is never automatically retried.

The default cap is four attempted calls in a rolling 24 hours, including failed calls;
options allow 1–12. The attempt is saved before contacting the provider. This is a
call limit, not a currency budget; token limits and billing belong to the provider.
Calls time out after 120 seconds by default (configurable 30–300). Only one review
runs at once. Unload/restart cancels in-flight work. Provider cancellation may not
cancel server-side computation or billing.

## Evidence and interpretation

Evidence includes coverage-gated metrics, explicit metric definitions, historical
and current input quality, selected controller output context, thermal survey
properties/confidence, missing-data advisories and adjustment time/kind/scope.
Raw history, journal note text, credentials, addresses and household routines are
excluded. An investigation question is sent as entered (up to 1,000 characters).

Evidence is bounded to 48 KB of UTF-8 JSON. Unavailable non-gated metrics are grouped by name in an explicit `unavailable_metrics` list; missing values are never replaced by zero. Repeated construction properties are
shared. Room adjacency is retained; detailed internal surfaces and repeated survey timestamps are explicitly omitted. If necessary, detailed room
surveys are omitted with an explicit marker. Larger remaining evidence is rejected.
Byte limits are not token counts: smaller-context models can still reject a request.
No missing measurement is filled in to make a report possible.

Responses must match a strict schema and bounded lengths, and every finding must
cite existing fact IDs. Extra control/action fields and invented references are
rejected. These checks verify structure and reference existence, **not whether the
AI's interpretation is correct**. Reports require human review. A cited number alone
does not validate a causal explanation or recommendation.

Prompt version 2 states every acceptance limit, including 1–12 references per
finding, a nonblank 30–500-character practical follow-up, and all report/string
limits. Schema field descriptions and local validation use the same constants.
The provider schema constrains fields, types and reference choices. Length/count
limits remain locally enforced because provider grammars do not support the same
JSON Schema keywords (see [Anthropic's structured-output limitations](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)).
There is no silent trimming, invented follow-up text or automatic paid retry.
Rejections expose a category such as `invalid_finding` or
`invalid_evidence_reference`, without exposing private report content. Previously
accepted reports remain readable; storage format and acceptance limits are unchanged.

In particular, target overshoot includes setbacks/off-floor targets and cannot alone
establish heating-induced overheating. Demand coverage is different from demand
active share. Matched-response eligibility does not determine recovery eligibility.
No energy savings claims are supported without metered energy and suitable context.
Detected target-recovery episodes are not a count of all heating or burner cycles.
Recent-change coverage cannot establish physical sensor freshness, since unchanged
values may still be freshly reported. Current shadow/observation-only state cannot
establish what any controller did throughout a historical window.

Private storage is `.storage/home_heating_optimisation.<entry_id>.advisor`, separate
from measurement history. It retains 20 reports with exact evidence plus bounded
attempt/schedule metadata. Corrupt/unsupported storage is preserved and blocks new
calls. Failed saves also block calls until reload. Removing the integration preserves
this private store. Home Assistant/provider traces or logs can contain AI task inputs
and outputs; integration diagnostics omit them.

## Initial local-model evaluation

The installed `gemma-4-26b-a4b` endpoint returned structured reports in approximately
13 seconds in the initial live trial. Its 16,384-token context limit required compact
survey evidence and a 1,500-token output allowance through Home Assistant. The direct generation schema
uses arrays of allowed references without `uniqueItems`, which this server's grammar
backend rejects when using a multiple-select selector through the generic service.

The trial also produced unsupported causal explanations, invented reference paths,
and some irrelevant follow-up checks. Explicit definitions improved the summaries;
reference constraints and validation reject unknown IDs. Existing IDs can still be
cited incorrectly, and a plausible suggestion can still be irrelevant. Treat Gemma
reports as drafts for review, not validated optimisation recommendations. Scheduled
reviews remain opt-in. Model comparisons must use identical saved evidence and
instructions, record profile settings and failed attempts, and assess interpretation
separately from format validation. A passing response is not proof of a reliable model.

Provider failures expose an error category without provider response text. Context-limit and unsupported-grammar errors are identified separately.

## Frozen-evidence comparison (15 September 2026)

Two standalone provider requests per model used the same saved evidence,
instructions, system prompt and serialized generation schema, followed by HHO's
local validator. These requests isolate model output; a deployment check through
Home Assistant is a separate step. No retries or response editing were used.

| Model/profile | Output cap | Format passes | Mean latency |
| --- | ---: | ---: | ---: |
| Sonnet 5, high effort | 10,000 tokens | 2/2 | 42.8 s |
| Haiku 4.5, 1,024 thinking tokens | 6,000 tokens | 2/2 | 29.4 s |
| Gemma 4 26B A4B, provider default effort | 1,500 tokens | 2/2 | 10.5 s |

All six responses passed format/reference validation, but all contained unsupported
or over-broad interpretations. Sonnet handled several metric distinctions better
in this small trial but still miscounted available duty values. Haiku invented
an observation count and a causal role for shadow mode. Gemma again linked matched
comparison eligibility to recovery/rate validity. This is a profile comparison on
one evidence snapshot, not a general model ranking or a reliable optimisation
benchmark. Haiku was not tested at its default response allowance. Sonnet is a
candidate for human-reviewed investigations; automatic recommendations remain
unvalidated. Private fixture hashes, settings, usage, responses and review notes
are retained locally and excluded from the repository.
