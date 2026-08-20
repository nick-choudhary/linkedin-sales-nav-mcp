"""Non-tool MCP capabilities: one resource and one prompt.

The resource gives agents cheap, side-effect-free read access to the saved
query state (the same data the list_queries tool returns) as attachable
context. The prompt encodes the intended prospecting workflow so a client that
supports prompts can hand the agent the playbook instead of the user spelling
it out.
"""

import json

from fastmcp import FastMCP

from sales_nav_mcp.store import get_store


def register_capabilities(mcp: FastMCP) -> None:
    """Register the server's resources and prompts with the MCP server."""

    @mcp.resource(
        "sales-nav://queries",
        name="saved_queries",
        title="Saved Search Queries",
        description=(
            "Every saved Sales Navigator search and its progress (url_hash, "
            "url, scraper_type, status, last_page, total_available, "
            "records_count). Attach this to see what is already in the local "
            "store before deciding to search again."
        ),
        mime_type="application/json",
        tags={"data"},
    )
    def saved_queries() -> str:
        """JSON snapshot of all saved queries and their progress."""
        store = get_store()
        payload = {
            "queries": [
                {
                    "url_hash": q.url_hash,
                    "url": q.url,
                    "scraper_type": q.scraper_type,
                    "status": q.status,
                    "last_page": q.last_page,
                    "total_available": q.total_available,
                    "records_count": q.records_count,
                }
                for q in store.list_queries()
            ]
        }
        return json.dumps(payload, indent=2)

    @mcp.prompt(
        name="sales_nav_search_workflow",
        title="Sales Navigator Search Workflow",
        description=(
            "Playbook for a prospecting run with this server: verify the "
            "session, run a Sales Navigator search URL, then sample or export "
            "the saved records."
        ),
        tags={"workflow"},
    )
    def sales_nav_search_workflow(goal: str = "") -> str:
        """
        Walk the agent through a Sales Navigator prospecting run.

        Args:
            goal: What the user wants to find (e.g. 'heads of engineering at
                mid-size fintechs in Berlin'). Optional — the prompt asks for
                it when empty.

        Returns:
            The workflow instructions as a single user message.
        """
        goal_line = (
            f"Goal: {goal}"
            if goal.strip()
            else "Goal: not specified — ask the user what they want to find."
        )
        intro = (
            "You are running a LinkedIn Sales Navigator prospecting task "
            "with the linkedin-sales-nav-mcp server."
        )
        return f"""{intro}

{goal_line}

Follow this sequence:

1. Session check — call `check_session_status`. If `logged_in` is false, tell
   the user to run `linkedin-sales-nav-mcp --login` once in a terminal and
   sign in by hand. Never attempt to automate the LinkedIn login.
2. Get the search URL — this server does not build filters itself. Have the
   user build the search in the Sales Navigator UI (or open a saved
   lead/account list) and paste the full URL.
3. Run the search — call `search_contacts` (people) or `search_accounts`
   (companies) with that URL and a small `pages` value first (1-3).
   Pagination is deliberately slow (3-8s dwell per page, longer breaks every
   ~5 pages), so expect a 10-page run to take minutes and do not fire a second
   call while one is still running.
4. Resume instead of restart — re-calling the same URL continues from the
   next page. Only pass `refresh=true` when the user wants to discard saved
   progress and start over.
5. Read the data — search tools return a progress summary, not the records.
   Use `get_results` (up to 200 records) to analyze a sample in-chat, or
   `export_results` to write the full set as JSON/CSV and analyze the file.
6. Volume discipline — fetch what the task needs, not the whole result set.
   High daily volume is what gets LinkedIn accounts flagged; no pacing
   setting changes that.

Report back: what was searched, how many records are saved, and where the
export landed (if any)."""
