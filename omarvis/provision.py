from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .daemon import CONFIG_PATH, DEFAULT_CONFIG, load_api_key, load_config

PROMPT_PATH = Path(__file__).parent.parent / "agent" / "prompt.md"
DYNAMIC_VARIABLES = (
    "command_catalog",
    "hyprland_dispatchers",
    "herdr_catalog",
    "browser_catalog",
    "current_state",
)


def conversation_payload(prompt: str, llm: str) -> dict[str, Any]:
    return {
        "turn": {
            "turn_timeout": 20.0,
            "silence_end_call_timeout": 30.0,
        },
        "conversation": {"max_duration_seconds": 300},
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
                "built_in_tools": {
                    "end_call": {
                        "type": "system",
                        "name": "end_call",
                        "params": {"system_tool_type": "end_call"},
                    }
                },
                "tools": [
                    {
                        "type": "client",
                        "name": "run",
                        "description": "Run one policy-approved Omarchy, Hyprland, Herdr, or browser command on the user's computer.",
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
                            },
                        },
                    }
                ],
            },
        },
    }


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def manual_steps() -> str:
    return """Create an ElevenLabs Agent in the dashboard with these settings:
- System prompt: paste agent/prompt.md.
- LLM: Gemini 2.5 Flash (`gemini-2.5-flash`).
- First message: empty. Language: English.
- Turn timeout: 20 seconds. Silence end-call timeout: 30 seconds. Maximum duration: 300 seconds.
- Enable the `end_call` system tool.
- Add a Client tool named `run` with Wait for response enabled and a 35-second response timeout.
- `run` parameters: required string `command`; optional boolean `confirmed`.
- Declare command_catalog, hyprland_dispatchers, herdr_catalog, browser_catalog, and current_state as dynamic variables.
Then rerun `python -m omarvis.provision --agent-id <pasted-agent-id>`."""


def provision(config: dict[str, Any], api_key: str) -> str:
    try:
        from elevenlabs import ConversationalConfig, ElevenLabs
    except ImportError as error:
        raise RuntimeError(
            "The ElevenLabs SDK is missing. Run bin/omarvis-setup.\n" + manual_steps()
        ) from error
    prompt = PROMPT_PATH.read_text()
    conversation_config = ConversationalConfig.model_validate(
        conversation_payload(prompt, str(config["llm"]))
    )
    client = ElevenLabs(api_key=api_key)
    if config.get("agent_id"):
        response = client.conversational_ai.agents.update(
            str(config["agent_id"]),
            conversation_config=conversation_config,
            name="Omarvis",
        )
        agent_id = str(getattr(response, "agent_id", config["agent_id"]))
    else:
        response = client.conversational_ai.agents.create(
            conversation_config=conversation_config,
            name="Omarvis",
        )
        agent_id = str(response.agent_id)
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
