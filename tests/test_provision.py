from elevenlabs import ConversationalConfig

from omarvis.provision import conversation_payload


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
