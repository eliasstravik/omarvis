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
    placeholders = payload["agent"]["dynamic_variables"][
        "dynamic_variable_placeholders"
    ]
    assert "profile" in placeholders


def test_provision_creates_agent_and_ask_agent(monkeypatch):
    creates = []

    class Agents:
        def create(self, *, conversation_config, name):
            creates.append((name, conversation_config))
            return SimpleNamespace(agent_id=f"{name.lower().replace(' ', '-')}-id")

        def update(self, *_args, **_kwargs):
            raise AssertionError("new config should create both agents")

    client = SimpleNamespace(
        conversational_ai=SimpleNamespace(agents=Agents())
    )
    monkeypatch.setattr(elevenlabs, "ElevenLabs", lambda *, api_key: client)
    monkeypatch.setattr("omarvis.provision.save_config", lambda config: None)
    config = {"agent_id": "", "ask_agent_id": "", "llm": "gpt-5.6-sol"}

    agent_id = provision(config, "api-key")

    assert agent_id == "omarvis-id"
    assert config["agent_id"] == "omarvis-id"
    assert config["ask_agent_id"] == "omarvis-ask-id"
    assert [name for name, _payload in creates] == ["Omarvis", "Omarvis Ask"]
    assert "read-only teacher" in creates[1][1].agent.prompt.prompt
