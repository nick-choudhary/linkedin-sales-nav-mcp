"""Registration tests for the server's prompt and resource capabilities."""

import json

import pytest
from fastmcp import Client

from sales_nav_mcp.config import reset_config
from sales_nav_mcp.server import create_mcp_server
from sales_nav_mcp.store import Store, close_store


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the store at a throwaway DB so tests never touch real state."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    reset_config()
    close_store()
    yield
    close_store()
    reset_config()


class TestSavedQueriesResource:
    async def test_registered_and_returns_json(self, isolated_state):
        mcp = create_mcp_server()
        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = [str(r.uri) for r in resources]
            assert "sales-nav://queries" in uris

            contents = await client.read_resource("sales-nav://queries")
            payload = json.loads(contents[0].text)
            assert payload == {"queries": []}

    async def test_reflects_saved_query(self, isolated_state, tmp_path):
        store = Store(tmp_path / "state" / "sales_nav.db")
        store.upsert_query(
            "https://www.linkedin.com/sales/search/people?query=(x)", "contacts"
        )
        store.close()

        mcp = create_mcp_server()
        async with Client(mcp) as client:
            contents = await client.read_resource("sales-nav://queries")
            payload = json.loads(contents[0].text)
        assert len(payload["queries"]) == 1
        q = payload["queries"][0]
        assert q["scraper_type"] == "contacts"
        assert q["url_hash"]


class TestSearchWorkflowPrompt:
    async def test_registered_and_renders_goal(self):
        mcp = create_mcp_server()
        async with Client(mcp) as client:
            prompts = await client.list_prompts()
            names = [p.name for p in prompts]
            assert "sales_nav_search_workflow" in names

            rendered = await client.get_prompt(
                "sales_nav_search_workflow", {"goal": "find fintech CTOs"}
            )
            text = rendered.messages[0].content.text
            assert "find fintech CTOs" in text
            assert "search_contacts" in text

    async def test_renders_without_goal(self):
        mcp = create_mcp_server()
        async with Client(mcp) as client:
            rendered = await client.get_prompt("sales_nav_search_workflow", {})
            text = rendered.messages[0].content.text
            assert "ask the user" in text
            assert "check_session_status" in text
