from pathlib import Path
from types import SimpleNamespace

import elevenlabs
from elevenlabs import ConversationalConfig

from omarvis.provision import conversation_payload, provision


def test_conversation_payload_enables_end_call_as_a_system_tool():
    config = ConversationalConfig.model_validate(
        conversation_payload("prompt", "gpt-5.6-sol")
    )
    payload = config.model_dump(mode="json", exclude_none=True)
    tools = payload["agent"]["prompt"]["tools"]

    assert tools[0] == {
        "type": "system",
        "name": "end_call",
        "description": "",
        "params": {"system_tool_type": "end_call"},
    }
    assert tools[1]["type"] == "client"
    assert tools[1]["name"] == "run"
    assert payload["conversation"]["file_input"] == {
        "enabled": True,
        "max_files_in_memory": 3,
        "max_files_per_conversation": 10,
    }
    assert payload["tts"]["voice_id"] == "JSWO6cw2AyFE324d5kEr"
    placeholders = payload["agent"]["dynamic_variables"][
        "dynamic_variable_placeholders"
    ]
    assert "profile" in placeholders


def test_provision_creates_exactly_one_agent(monkeypatch):
    creates = []

    class Agents:
        def create(self, *, conversation_config, name):
            creates.append((name, conversation_config))
            return SimpleNamespace(agent_id=f"{name.lower().replace(' ', '-')}-id")

        def update(self, *_args, **_kwargs):
            raise AssertionError("new config should create the agent")

    client = SimpleNamespace(
        conversational_ai=SimpleNamespace(agents=Agents())
    )
    monkeypatch.setattr(elevenlabs, "ElevenLabs", lambda *, api_key: client)
    monkeypatch.setattr("omarvis.provision.save_config", lambda config: None)
    config = {"agent_id": "", "llm": "gpt-5.6-sol"}

    agent_id = provision(config, "api-key")

    # Ask mode is gone, so provisioning must never create a second agent.
    assert agent_id == "omarvis-id"
    assert config["agent_id"] == "omarvis-id"
    assert "ask_agent_id" not in config
    assert [name for name, _payload in creates] == ["Omarvis"]
    assert creates[0][1].tts.voice_id == "JSWO6cw2AyFE324d5kEr"


def test_no_ask_prompt_ships_with_the_plugin():
    from omarvis import provision as provision_module

    assert not (Path(provision_module.__file__).parents[1] / "agent" / "prompt-ask.md").exists()
    assert "ask" not in provision_module.manual_steps().lower()


def test_reasoning_effort_is_pinned_for_model_switches():
    payload = conversation_payload("prompt", "gpt-5.6-terra")

    # A stored effort from a previous model can be invalid for the new one
    # (gemini's "minimal" broke the switch to gpt-5.6-terra), so provision
    # always sends an explicit, widely supported value.
    assert payload["agent"]["prompt"]["reasoning_effort"] == "low"


def test_herdr_skill_is_a_declared_dynamic_variable():
    from omarvis.provision import DYNAMIC_VARIABLES

    assert "herdr_skill" in DYNAMIC_VARIABLES
    payload = conversation_payload("prompt", "gpt-5.6-terra")
    placeholders = payload["agent"]["dynamic_variables"][
        "dynamic_variable_placeholders"
    ]
    assert "herdr_skill" in placeholders
