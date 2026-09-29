"""
Self-test: start mcp_server.py exactly the way Claude Desktop does (a child
process speaking MCP over stdio), then call a few tools.

    venv/bin/python testing/selftest_stdio.py          (macOS/Linux)
    venv\\Scripts\\python.exe testing\\selftest_stdio.py (Windows)

If this passes but Claude Desktop still doesn't show the server, the problem
is the Claude Desktop config/restart, not the server. No login is attempted.
"""

import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(REPO, "mcp_server.py")],
        cwd=os.path.expanduser("~"),  # like Claude Desktop: not the repo dir
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("    Server started. Tools: " + ", ".join(t.name for t in tools.tools))
            hello = await session.call_tool("hello", {})
            print("    hello -> " + hello.content[0].text)
            cal = await session.call_tool("get_calendar", {"days_ahead": 7, "days_behind": 0})
            lines = cal.content[0].text.splitlines()
            print("    get_calendar (7 days) -> " + (lines[0] if lines else "(empty)"))
            for line in lines[1:6]:
                print("      " + line)


def main() -> int:
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=120))
    except BaseException as e:  # noqa: BLE001 - report anything, incl. ExceptionGroup
        print(f"    SELF-TEST FAILED: {type(e).__name__}: {e}")
        return 1
    print("    SELF-TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
