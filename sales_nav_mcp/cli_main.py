"""CLI entry point.

Two modes:
  --login   one-shot: open a headed browser, wait for you to sign into Sales
            Navigator by hand, persist the profile, exit.
  (default) run the MCP server (stdio or streamable-http).

Configuration errors are printed to stderr and exit 1 rather than escaping as
tracebacks — under a stdio host a traceback is all the user sees behind
"Server disconnected".
"""

import asyncio
import logging
import sys

from sales_nav_mcp import __version__
from sales_nav_mcp.browser import close_browser, get_browser
from sales_nav_mcp.config import ConfigurationError, get_config
from sales_nav_mcp.server import create_mcp_server

logger = logging.getLogger(__name__)


def _configure_logging(log_level: str) -> None:
    # Log to stderr: on stdio transport, stdout belongs to the protocol.
    logging.basicConfig(
        level=getattr(logging, log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _run_login() -> None:
    async def go() -> bool:
        try:
            return await get_browser().run_login()
        finally:
            await close_browser()

    ok = asyncio.run(go())
    sys.exit(0 if ok else 1)


def main() -> None:
    """Main application entry point."""
    argv = sys.argv[1:]
    login_mode = "--login" in argv

    try:
        config = get_config()
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    _configure_logging(config.server.log_level)
    logger.info("LinkedIn Sales Navigator MCP Server v%s", __version__)

    if login_mode:
        _run_login()
        return

    mcp = create_mcp_server(tool_timeout=config.server.tool_timeout_seconds)

    try:
        if config.server.transport == "streamable-http":
            # host_origin_protection guards against DNS-rebinding: a website
            # the user merely visits could otherwise drive these tools from
            # the user's own browser.
            mcp.run(
                transport="streamable-http",
                host=config.server.host,
                port=config.server.port,
                path=config.server.path,
                host_origin_protection=True,
            )
        else:
            mcp.run(transport="stdio")
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logger.exception("Server runtime error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
