# Heating Advisor (0.5.0)

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
  reports (2,000 tokens is a tested starting point for the local Gemma deployment).
- Set Home Assistant APIs to none and **Functions to the literal `[]`**. An empty
  field inherits Extended OpenAI's default tools and is rejected.
- Extra body must be empty, `{}`, or a literal JSON object containing only
  `chat_template_kwargs.enable_thinking` with a boolean value. Templates and arbitrary
  request overrides are not accepted. Model support for thinking remains provider-owned.

For Claude, add Anthropic under Devices & services, enter the key there, and add an
AI Task profile with the chosen model and supported thinking settings. Disable
Home Assistant APIs, web search/fetch, code execution and tool search. Anthropic
compatibility is source-checked and covered by profile tests; live Claude evaluation
requires a configured account and has not yet been performed.

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
stays out of entity attributes and downloadable diagnostics. A dashboard reader,
notifications and conversational follow-ups are later work; actions provide the
initial report access and can be used by your own automations.

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

Evidence is bounded to 48 KB of UTF-8 JSON. Repeated construction properties are
shared. Room adjacency is retained; detailed internal surfaces and repeated survey timestamps are explicitly omitted. If necessary, detailed room
surveys are omitted with an explicit marker. Larger remaining evidence is rejected.
Byte limits are not token counts: smaller-context models can still reject a request.
No missing measurement is filled in to make a report possible.

Responses must match a strict schema and bounded lengths, and every finding must
cite existing fact IDs. Extra control/action fields and invented references are
rejected. These checks verify structure and reference existence, **not whether the
AI's interpretation is correct**. Reports require human review. A cited number alone
does not validate a causal explanation or recommendation.

In particular, target overshoot includes setbacks/off-floor targets and cannot alone
establish heating-induced overheating. Demand coverage is different from demand
active share. Matched-response eligibility does not determine recovery eligibility.
No energy savings claims are supported without metered energy and suitable context.

Private storage is `.storage/home_heating_optimisation.<entry_id>.advisor`, separate
from measurement history. It retains 20 reports with exact evidence plus bounded
attempt/schedule metadata. Corrupt/unsupported storage is preserved and blocks new
calls. Failed saves also block calls until reload. Removing the integration preserves
this private store. Home Assistant/provider traces or logs can contain AI task inputs
and outputs; integration diagnostics omit them.
