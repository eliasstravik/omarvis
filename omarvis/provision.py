from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .daemon import CONFIG_PATH, DEFAULT_CONFIG, load_api_key, load_config
from .privatefiles import write_private_path

PROMPT_PATH = Path(__file__).parent.parent / "agent" / "prompt.md"
DYNAMIC_VARIABLES = (
    "command_catalog",
    "hyprland_dispatchers",
    "herdr_catalog",
    "herdr_skill",
    "browser_catalog",
    "current_state",
    "profile",
)
DEFAULT_VOICE_ID = str(DEFAULT_CONFIG["voice_id"])


def conversation_payload(
    prompt: str, llm: str, voice_id: str = DEFAULT_VOICE_ID
) -> dict[str, Any]:
    return {
        "turn": {
            "turn_timeout": 20.0,
            "silence_end_call_timeout": 30.0,
        },
        "conversation": {
            "max_duration_seconds": 300,
            "file_input": {
                "enabled": True,
                "max_files_in_memory": 3,
                "max_files_per_conversation": 10,
            },
        },
        "tts": {"voice_id": voice_id},
        "agent": {
            "first_message": "",
            "language": "en",
            "dynamic_variables": {
                "dynamic_variable_placeholders": {
                    name: "" for name in DYNAMIC_VARIABLES
                }
            },
            "prompt": {
                "prompt": prompt,
                "llm": llm,
                # Pinned explicitly: a stored value from a previous model can
                # be invalid for the new one (gemini's "minimal" broke the
                # switch to gpt-5.6-terra). "low" is the latency-right choice
                # for a realtime voice agent and widely supported.
                "reasoning_effort": "low",
                "tools": [
                    {
                        "type": "system",
                        "name": "end_call",
                        "description": "",
                        "params": {"system_tool_type": "end_call"},
                    },
                    {
                        "type": "client",
                        "name": "run",
                        "description": "Run one policy-approved Omarchy, Hyprland, Herdr, browser, or Omarvis screenshot command on the user's computer.",
                        "expects_response": True,
                        "response_timeout_secs": 35,
                        "parameters": {
                            "type": "object",
                            "required": ["command"],
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "description": "One exact command from the supplied catalogs.",
                                },
                                "confirmed": {
                                    "type": "boolean",
                                    "description": "True only after the user explicitly confirms the exact pending command.",
                                },
                                "approve_category": {
                                    "type": "boolean",
                                    "description": "True only after a confirmed command offered category approval and the user explicitly agreed in a later turn.",
                                },
                            },
                        },
                    }
                ],
            },
        },
    }


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """Publish the config as a fresh 0600 file, renamed into place atomically."""
    write_private_path(
        path, (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def manual_steps() -> str:
    return """Create one ElevenLabs Agent in the dashboard with these settings:
- System prompt: paste agent/prompt.md.
- LLM: GPT-5.6 Sol (`gpt-5.6-sol`).
- First message: empty. Language: English.
- Voice ID: `JSWO6cw2AyFE324d5kEr`.
- Turn timeout: 20 seconds. Silence end-call timeout: 30 seconds. Maximum duration: 300 seconds.
- Enable file input with at most 3 files in memory and 10 files per conversation.
- Enable the `end_call` system tool.
- Add a Client tool named `run` with Wait for response enabled and a 35-second response timeout.
- `run` parameters: required string `command`; optional booleans `confirmed` and `approve_category`.
- Declare command_catalog, hyprland_dispatchers, herdr_catalog, herdr_skill, browser_catalog, current_state, and profile as dynamic variables.
Store the ID as agent_id in ~/.config/omarchy/omarvis/config.json."""


def _upsert_agent(
    client: Any,
    *,
    agent_id: str,
    name: str,
    conversation_config: Any,
) -> str:
    if agent_id:
        response = client.conversational_ai.agents.update(
            agent_id,
            conversation_config=conversation_config,
            name=name,
        )
        return str(getattr(response, "agent_id", agent_id))
    response = client.conversational_ai.agents.create(
        conversation_config=conversation_config,
        name=name,
    )
    return str(response.agent_id)


def provision(config: dict[str, Any], api_key: str) -> str:
    try:
        from elevenlabs import ConversationalConfig, ElevenLabs
    except ImportError as error:
        raise RuntimeError(
            "The ElevenLabs SDK is missing. Run bin/omarvis-setup.\n" + manual_steps()
        ) from error
    client = ElevenLabs(api_key=api_key)
    voice_id = str(config.get("voice_id") or DEFAULT_VOICE_ID)
    agent_config = ConversationalConfig.model_validate(
        conversation_payload(
            PROMPT_PATH.read_text(), str(config["llm"]), voice_id
        )
    )
    agent_id = _upsert_agent(
        client,
        agent_id=str(config.get("agent_id") or ""),
        name="Omarvis",
        conversation_config=agent_config,
    )
    config["agent_id"] = agent_id
    save_config(config)
    return agent_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or update the Omarvis ElevenLabs agent"
    )
    parser.add_argument(
        "--agent-id", help="Store an agent ID created manually in the dashboard"
    )
    arguments = parser.parse_args(argv)
    config = dict(DEFAULT_CONFIG)
    config.update(load_config())
    if arguments.agent_id:
        config["agent_id"] = arguments.agent_id
        save_config(config)
        print(f"Stored agent id {arguments.agent_id}")
        return 0
    api_key = load_api_key()
    if not api_key:
        print("ELEVENLABS_API_KEY is missing. Run bin/omarvis-setup.", file=sys.stderr)
        return 2
    try:
        agent_id = provision(config, api_key)
    except Exception as error:  # noqa: BLE001 - SDK failures need the manual fallback
        print(f"Provisioning failed: {error}\n\n{manual_steps()}", file=sys.stderr)
        return 1
    print(f"Omarvis agent ready: {agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
