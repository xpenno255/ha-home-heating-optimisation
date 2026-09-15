"""Inspect only allowlisted profile settings; credentials remain with providers."""

import json

from homeassistant.helpers import entity_registry as er

PROVIDERS = {"extended_openai_conversation", "anthropic"}


def profile_info(hass, entity_id):
    info = {"entity_id": entity_id, "status": "unsupported_profile"}
    registered = er.async_get(hass).async_get(entity_id)
    if not registered or registered.platform not in PROVIDERS:
        return info
    entry = hass.config_entries.async_get_entry(registered.config_entry_id)
    subentry = entry.subentries.get(registered.config_subentry_id) if entry else None
    if not subentry or subentry.subentry_type != "ai_task_data":
        return info
    data = subentry.data
    info.update(
        provider=registered.platform,
        name=subentry.title,
        model=data.get("chat_model", "provider_default"),
        effort=data.get("reasoning_effort", data.get("thinking_effort", "provider_default")),
        thinking_budget=data.get("thinking_budget"),
        max_tokens=data.get("max_tokens", "provider_default"),
        settings_source="provider_profile",
    )
    # Extended OpenAI defaults to custom functions when the setting is absent or
    # empty. Require an explicit literal empty list, never evaluate a template.
    if data.get("llm_hass_api") or any(
        data.get(key, False) for key in ("web_search", "web_fetch", "code_execution", "tool_search")
    ):
        info["status"] = "tools_enabled"
    elif registered.platform == "extended_openai_conversation" and data.get("functions") not in (
        "[]",
        "[]\n",
    ):
        info["status"] = "tools_enabled"
    elif registered.platform == "extended_openai_conversation" and not safe_extra_body(
        data.get("extra_body", "")
    ):
        info["status"] = "unsupported_extra_body"
    else:
        state = hass.states.get(entity_id)
        info["status"] = (
            "ready"
            if state and state.state not in ("unavailable", "unknown")
            else "profile_unavailable"
        )
        # An AI Task never used before may have an unknown last-activity timestamp.
        if state and state.state == "unknown":
            info["status"] = "ready"
    return info


def profiles(hass):
    return [
        profile_info(hass, e.entity_id)
        for e in er.async_get(hass).entities.values()
        if e.domain == "ai_task" and e.platform in PROVIDERS
    ]


def safe_extra_body(value):
    if not value:
        return True
    try:
        data = json.loads(value)
        return (
            isinstance(data, dict)
            and set(data) <= {"chat_template_kwargs"}
            and (
                "chat_template_kwargs" not in data
                or (
                    isinstance(data["chat_template_kwargs"], dict)
                    and set(data["chat_template_kwargs"]) <= {"enable_thinking"}
                    and all(type(v) is bool for v in data["chat_template_kwargs"].values())
                )
            )
        )
    except ValueError, TypeError:
        return False
