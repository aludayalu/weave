let interaction = {
    "side": "user", // or assistant
    "id": Math.random(),
    "thinking": [
        {"type": "text", "content": ""},
        {"type": "tool_call", "data": [], "raw_message": {}},
        {"type": "tool_response", "data": {}, "raw_message": {}},
    ],
    text, files,
    "output": [
        {"type": "text", "content": "", "raw_message": {}}
    ],
}