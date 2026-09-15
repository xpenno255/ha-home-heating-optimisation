# Design notes

Recorded 15 September 2026 from the initial design discussion and read-only
inspection of the existing installation. These are requirements and proposed
architecture. The observation-only foundation is now implemented; see the
[implementation plan](implementation-plan.md) for the boundary of version 0.1.0.

## Product requirements

- One Home Assistant integration with independently enabled comfort, boiler,
  analytics and advisor modules.
- Shared room/sensor mappings, normalised temperatures, timestamps and explicit
  data-quality information. Freshness requirements remain appropriate to each input.
- Distinguish scheduled operative-comfort target, corrected air command, measured
  air temperature and estimated operative temperature.
- Distinguish requested, effective, sent and confirmed boiler setpoints from
  actual water temperatures.
- Reuse one resolved heating/DHW state, with its source and uncertainty.
- Record why controllers change targets, alongside manual and physical changes.
- Preserve control limits, overrides, fallback behaviour and shadow modes.
- Keep one writer responsible for each controlled setting.
- An unavailable AI provider or failed history analysis must not stop live control.

## Advisor tasks and evidence

Tasks should include daily summaries, weekly reviews, investigations, optimisation
proposals, experiment follow-ups and conversational explanations. Each task needs
a selectable AI profile and appropriate scheduling, notification and output limits.

The integration computes metrics and comparison eligibility in tested code. The
model receives structured evidence including intent, outcomes, operating conditions,
coverage, uncertainty and intervention history. Numerical claims must reference
that evidence. Missing data must not become invented measurements or diagnoses.

Keep commanded-air tracking and modelled operative comfort separate. Radiator
capacity estimates are not measurements of delivered heat. Demand and cycling
alone cannot establish energy savings; evaluate metered energy with suitable
weather and DHW context where available.

Start with advisory reports. Later trials should specify an exact permitted
change, rationale, evaluation conditions, success criteria and rollback policy.
Any execution belongs to validated integration code. Autonomous tuning is a later,
optional feature; the initial advisor does not adjust DHW protection settings.

## Provider integration decision

The initial discussion considered accepting an Anthropic key directly inside the
heating integration. The preferred design after inspecting Home Assistant is to
delegate provider configuration to existing AI integrations instead:

- **Local Gemma:** Extended OpenAI Conversation.
- **Claude:** Home Assistant's built-in Anthropic integration.
- **Heating task:** selects an `ai_task` entity, or a conversation agent when needed.

This keeps credentials, model selection and provider-specific reasoning options in
the provider's configuration. Separate profiles provide different models/efforts
for different heating tasks. Standard HA task/conversation actions do not offer
generic per-call model/effort overrides. Show inherited or unsupported settings
honestly; do not silently mutate a shared agent or equate every provider's thinking
toggle with Claude effort levels.

## Live configuration verified on 15 September 2026

- Home Assistant Core **2026.9.2**.
- Installed Extended OpenAI Conversation manifest reports **3.0.0**.
- Both Extended OpenAI connections support `conversation` and `ai_task_data`
  subentries; their currently registered entities are conversation agents.
- One connection, titled `gemma4-8001`, has a conversation profile configured for
  `google/gemma-4-E4B-it`, temperature 0.5 and maximum output 150 tokens.
- The connection titled `gemma4:e4b` has a main profile configured for
  **`gemma-4-26b-a4b`**, temperature 0.2, top-p 1.0 and maximum output 150 tokens.
  Its connection title does not reflect the configured model.
- That connection also has a separate music profile using `gemma-4-26b-a4b`, its
  own prompt and maximum output 150 tokens. It demonstrates the reusable connection
  with separate specialist profiles we want for heating.
- The `spark-vllm-8001` Assist pipeline selects
  `conversation.extended_openai_conversation_2`.
- Installed Extended OpenAI `ai_task.py` accepts `task.structure`, forwards it to
  generation, and parses structured responses. This is source inspection, not a
  successful Gemma structured-output test.
- No Anthropic configuration entry exists yet.
- No credentials or household prompts are copied into this workspace.

## Dedicated local heating profile

Use the existing Gemma server with a separate heating configuration and prompt;
another agent profile does not inherently require another model instance. Give
reports an appropriate output allowance instead of inheriting the 150-token voice
limit. Validate supported thinking controls and server configuration before exposing
them to users.

Separate stable domain instructions, task-specific instructions and evidence.
Start periodic reviews with fresh conversation context and supply relevant history
explicitly. Retain bounded context for user follow-up conversations. Tool access
must match the profile's purpose and be enforced independently of prompt wording.

## Implementation sequence

1. Inventory reusable control/analytics code, licences, tests and migration needs.
2. Establish shared observations and configuration while preserving behaviour.
3. Connect decision intent and outcome analytics; preserve historical definitions.
4. Add AI Task execution, schema validation, report storage and profile selection.
5. Evaluate Gemma and Claude on the same evidence packages, including missing-data
   and no-change cases. Assess accuracy, uncertainty and recommendation quality.
6. Validate migrations, entity/history continuity, failure handling and exclusive
   write ownership before any live deployment.

## References

- [Anthropic integration: credentials, conversation/AI Task profiles, models and thinking settings](https://www.home-assistant.io/integrations/anthropic/)
- [AI Task generate-data action and structured responses](https://www.home-assistant.io/actions/ai_task.generate_data/)
- [Conversation integration](https://www.home-assistant.io/integrations/conversation/)
- [Extended OpenAI Conversation](https://github.com/jekalmin/extended_openai_conversation)

Provider compatibility and reasoning settings still need end-to-end verification
on the selected models. No model evaluation or configuration change was performed
during this inspection.
