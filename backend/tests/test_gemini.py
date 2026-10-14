from backend.voice.gemini import _contents, _declarations


def test_contents_pairs_tool_results_with_calls_and_keeps_signatures() -> None:
    system, contents = _contents(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "holidays?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "calendar.holidays", "arguments": {}}, "thought_signature": "sig1"},
                    {"function": {"name": "system.get_time", "arguments": {}}},
                ],
            },
            {"role": "tool", "content": '{"holidays": []}'},
            {"role": "tool", "content": '{"now": "noon"}'},
            {"role": "assistant", "content": "None this week."},
        ]
    )
    assert system == "be brief"
    assert [c["role"] for c in contents] == ["user", "model", "user", "model"]
    assert contents[1]["parts"][0]["thoughtSignature"] == "sig1"
    results = contents[2]["parts"]
    assert [p["functionResponse"]["name"] for p in results] == ["calendar.holidays", "system.get_time"]


def test_declarations_drop_empty_parameters() -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": "a", "description": "x", "parameters": {"type": "object", "properties": {}}},
        },
        {
            "type": "function",
            "function": {
                "name": "b",
                "description": "y",
                "parameters": {"type": "object", "properties": {"q": {"type": "string", "title": "Q"}}},
            },
        },
    ]
    a, b = _declarations(tools)
    assert "parameters" not in a
    assert b["parameters"]["properties"]["q"] == {"type": "string"}
