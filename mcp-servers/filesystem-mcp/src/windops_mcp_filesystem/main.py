"""Start the Filesystem MCP server."""

from windops_mcp.serve import run

from windops_mcp_filesystem.tools import INSTRUCTIONS, SERVER, tools


def main() -> None:
    run(SERVER, tools, instructions=INSTRUCTIONS)


if __name__ == "__main__":
    main()
