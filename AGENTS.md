# Sci-Hub-MCP-Server

Fork of JackKuo666/Sci-Hub-MCP-Server (Python MCP server for Sci-Hub). The owner is 6 commits ahead: updated Sci-Hub scraping, `scihub_patched.py`, tenacity in place of retrying, the OpenAlex title tool `search_scihub_by_title_via_openalex`, and the `mcp<2` pin.

- The owner runs this as an MCP server on their machines. A broken change breaks those sessions, so do not change code without being asked.
- Install: `pip install -r requirements.txt`, in a virtualenv (Python 3.10, see `.python-version`). Run: `python sci_hub_server.py`. There are no tests.
- Keep `mcp>=1.9,<2` in `requirements.txt`. `sci_hub_server.py` imports `mcp.server.fastmcp`, which mcp 2.x removed, and an unpinned install builds a venv that cannot start.
- Optional environment variables: `CROSSREF_MAILTO`, `OPENALEX_MAILTO`.
- Pull requests go to `domtisdell/Sci-Hub-MCP-Server`, never the parent. Pass `-R domtisdell/Sci-Hub-MCP-Server` to every `gh pr` command, and do not add or push to an `upstream` remote.
