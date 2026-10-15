import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from naman.agent import Naman
from naman.mcp_tools import MCPManager, load_config, needs_approval, tool_name
from naman.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
DEMO = {"command": "python", "args": ["demo_server.py"]}


@pytest.fixture(scope="module")
def manager():
    m = MCPManager(
        {"demo": DEMO, "broken": {"command": "definitely-not-a-real-command-xyz"}},
        base_dir=FIXTURES,
    )
    m.start()
    yield m
    m.close()


def test_servers_start_and_failures_are_isolated(manager):
    assert manager.status["demo"].ok
    assert set(manager.status["demo"].tools) == {"add", "save_note", "boom"}
    broken = manager.status["broken"]
    assert not broken.ok and broken.error


def test_tools_are_namespaced_with_approval_rules(manager):
    tools = {t.name: t for t in manager.tools()}
    assert set(tools) == {"demo__add", "demo__save_note", "demo__boom"}
    assert tools["demo__add"].needs_approval is False  # read-only
    assert tools["demo__save_note"].needs_approval is True  # unannotated = write
    assert tools["demo__add"].description.startswith("[demo] ")


def test_calls_return_text_and_errors(manager):
    tools = {t.name: t for t in manager.tools()}
    assert tools["demo__add"].run(a=2, b=40) == "42"
    # MCP 2.x servers don't leak exception details to clients, only that the tool failed.
    assert tools["demo__boom"].run() == "error: Error executing tool boom"
    assert manager.call("broken", "x", {}).startswith("error: MCP server 'broken' is not connected")


def test_agent_uses_mcp_tools_and_asks_before_writes(manager):
    class Script:
        def __init__(self, *msgs):
            self.msgs, self.seen = list(msgs), []

        def chat(self, messages, tools):
            self.seen.append((messages, [t["function"]["name"] for t in tools]))
            return self.msgs.pop(0)

    def use(name, args, cid):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

    model = Script(
        use("demo__add", {"a": 1, "b": 2}, "c1"),
        use("demo__save_note", {"text": "hi"}, "c2"),
        {"role": "assistant", "content": "3, and the note is saved."},
    )
    naman = Naman(Store(":memory:"), model, InMemorySaver(), extra_tools=manager.tools())
    reply = naman.send("t", "add 1 and 2, then save a note")
    assert reply.approval == {"tool": "demo__save_note", "args": {"text": "hi"}}  # add ran without asking
    assert naman.resume("t", True).text == "3, and the note is saved."
    assert "demo__add" in model.seen[0][1]
    tool_outputs = [m["content"] for m in model.seen[2][0] if m["role"] == "tool"]
    assert tool_outputs == ["3", "saved note #1"]


def test_config_loading(tmp_path):
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"mcpServers": {"a": DEMO, "b": {**DEMO, "enabled": False}}}))
    assert list(load_config(path)) == ["a"]
    assert load_config(tmp_path / "missing.json") == {}


def test_allow_list_and_policies(tmp_path):
    m = MCPManager({"demo": {**DEMO, "tools": ["add"], "approval": "always"}}, base_dir=FIXTURES)
    try:
        m.start()
        [tool] = m.tools()
        assert tool.name == "demo__add" and tool.needs_approval  # "always" overrides read-only
    finally:
        m.close()
    assert not m._thread.is_alive()
    assert needs_approval("never", {}) is False


def test_tool_names_fit_function_calling_rules():
    assert tool_name("my server", "get.thing/v2") == "my_server__get_thing_v2"
    assert len(tool_name("s" * 50, "t" * 50)) == 64
