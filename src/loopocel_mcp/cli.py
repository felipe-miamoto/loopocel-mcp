"""Command-line interface for loopocel-mcp."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import time

from .adapters.claude_code import HOOK_AGENTS, hooks_snippet, ingest_transcript, main_hook_cli
from .adapters.llm_proxy import LLMProxyServer
from .adapters.tool_proxy import MCPStdioWrapper
from .convert import convert, summarize_log
from .figure import render_session_figure
from .process_graphs import render_session_process_graphs
from .server import LoopOCELServer, handle_jsonrpc, run_http, run_stdio
from .validate import validate_log


def fresh_session_dir(session_dir: Path | None, sessions_root: Path | str | None, label: str) -> Path:
    """Folder for one recording run; never appends to a capture from an earlier run.

    Wrappers and proxies are started once per agent session from a fixed config, so a
    given folder that already holds a capture gets a new timestamped sibling instead.
    """
    from datetime import datetime
    import re as _re

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if session_dir is None:
        slug = _re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()[:40] or "run"
        return Path(sessions_root or "sessions") / f"{stamp}-{slug}"
    session_dir = Path(session_dir)
    if (session_dir / "capture.jsonl").is_file():
        candidate = session_dir.parent / f"{session_dir.name}-{stamp}"
        n = 2
        while candidate.exists():
            candidate = session_dir.parent / f"{session_dir.name}-{stamp}-{n}"
            n += 1
        print(f"loopocel: {session_dir} already holds a recording; writing to {candidate}", file=sys.stderr)
        return candidate
    return session_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="loopocel-mcp",
        description="LoopOCEL MCP server, capture adapters, converter, validator, and figure renderer",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.cwd() / "sessions",
        help="Root directory for recorded sessions",
    )
    sub = parser.add_subparsers(dest="subcommand")

    serve_p = sub.add_parser("serve", help="Run the LoopOCEL MCP server (default)")
    serve_p.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)

    hook_p = sub.add_parser("hook", help="Record a hook event from stdin into a session directory")
    hook_p.add_argument("--session-dir", type=Path, required=True)
    hook_p.add_argument("--event", help="Hook event name, for agents whose payload does not carry it (Antigravity)")

    hcfg_p = sub.add_parser("hooks-config", help="Print the hook configuration for Claude Code, Codex or Antigravity")
    hcfg_p.add_argument("--agent", choices=HOOK_AGENTS, default="claude-code")
    hcfg_p.add_argument("--session-dir", type=Path, required=True)
    hcfg_p.add_argument("--command", default="loopocel-mcp", help="Command that runs loopocel-mcp")

    conv_p = sub.add_parser("convert", help="Convert capture.jsonl in a directory to loopocel.json")
    conv_p.add_argument("directory", type=Path)

    val_p = sub.add_parser("validate", help="Validate loopocel.json in a directory")
    val_p.add_argument("directory", type=Path)
    val_p.add_argument("--pm4py", action="store_true")

    fig_p = sub.add_parser("figure", help="Render LoopOCEL coverage figure for a directory")
    fig_p.add_argument("directory", type=Path)
    fig_p.add_argument("--compare", type=Path, nargs="*", default=[])

    graphs_p = sub.add_parser(
        "graphs", help="Render the agent-loop OC-DFG and semantic process graph for a directory"
    )
    graphs_p.add_argument("directory", type=Path)
    graphs_p.add_argument(
        "--include", type=Path, nargs="*", default=[], help="Further session directories to aggregate"
    )

    ing_p = sub.add_parser("ingest", help="Ingest an agent transcript into a session directory")
    ing_p.add_argument("session_dir", type=Path)
    ing_p.add_argument("transcript", type=Path)
    ing_p.add_argument("--task", default=None)

    proxy_p = sub.add_parser("proxy", help="Run local LLM recording proxy")
    proxy_p.add_argument(
        "--session-dir", type=Path, default=None,
        help="Session folder (default: a new timestamped folder under --sessions-root)",
    )
    proxy_p.add_argument("--upstream", required=True, help="Upstream API base URL (e.g. https://api.anthropic.com)")
    proxy_p.add_argument("--host", default="127.0.0.1")
    proxy_p.add_argument("--port", type=int, default=18080)
    proxy_p.add_argument("--task", default="proxy-task")

    wrap_p = sub.add_parser("wrap", help="Wrap a downstream stdio MCP server and record all tools/call interactions")
    wrap_p.add_argument(
        "--session-dir", type=Path, default=None,
        help="Session folder (default: a new timestamped folder under --sessions-root)",
    )
    wrap_p.add_argument("--task", default="mcp-wrapped-task", help="Task identifier")
    wrap_p.add_argument("command", nargs=argparse.REMAINDER, help="Downstream MCP server command (prefix with --)")

    call_p = sub.add_parser("call", help="Invoke an MCP tool on LoopOCELServer via JSON-RPC tools/call")
    call_p.add_argument("tool_name", help="MCP tool name (e.g. session_start, record_turn, session_end)")
    call_p.add_argument("--args-json", default="{}", help="JSON object of tool arguments")
    call_p.add_argument("--args-file", type=Path, default=None, help="Path to JSON file of tool arguments")

    args = parser.parse_args(argv)

    if args.subcommand in (None, "serve"):
        transport = getattr(args, "transport", "stdio")
        server = LoopOCELServer(sessions_root=args.sessions_root)
        if transport == "http":
            run_http(server, host=args.host, port=args.port)
        else:
            run_stdio(server)
        return 0

    if args.subcommand == "hook":
        return main_hook_cli(args.session_dir, event=args.event)

    if args.subcommand == "hooks-config":
        print(json.dumps(hooks_snippet(args.agent, args.session_dir, args.command), indent=2))
        return 0

    if args.subcommand == "convert":
        log = convert(args.directory)
        print(json.dumps({"objects": len(log["objects"]), "events": len(log["events"])}, indent=2))
        return 0

    if args.subcommand == "validate":
        ocel_path = args.directory / "loopocel.json"
        if not ocel_path.is_file():
            log = convert(args.directory)
        else:
            log = json.loads(ocel_path.read_text(encoding="utf-8"))
        report = validate_log(log, ocel_path=ocel_path, check_pm4py=args.pm4py)
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1

    if args.subcommand == "figure":
        info = render_session_figure(args.directory, compare_dirs=args.compare)
        print(json.dumps(info, indent=2))
        return 0

    if args.subcommand == "graphs":
        res = render_session_process_graphs(args.directory, include_dirs=args.include)
        print(json.dumps({"sessions": res["sessions"], "outputs": res["outputs"]}, indent=2))
        return 0

    if args.subcommand == "ingest":
        stats = ingest_transcript(args.session_dir, args.transcript, task_id=args.task)
        log = convert(args.session_dir)
        print(json.dumps({**stats, "objects": len(log["objects"]), "events": len(log["events"])}, indent=2))
        return 0

    if args.subcommand == "proxy":
        args.session_dir = fresh_session_dir(args.session_dir, args.sessions_root, args.task or "proxy")
        proxy = LLMProxyServer(
            args.session_dir,
            args.upstream,
            host=args.host,
            port=args.port,
            task_id=args.task,
        )
        url = proxy.start()
        print(json.dumps({"proxy_url": url, "session_dir": str(args.session_dir.resolve())}), flush=True)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            proxy.stop()
            print(json.dumps({"finalized": proxy.finalize()}, indent=2), flush=True)
        return 0

    if args.subcommand == "wrap":
        cmd = list(args.command)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        session_dir = fresh_session_dir(args.session_dir, args.sessions_root, args.task or "wrap")
        wrapper = MCPStdioWrapper(session_dir, cmd, task_id=args.task)
        return wrapper.run()

    if args.subcommand == "call":
        server = LoopOCELServer(sessions_root=args.sessions_root, transport="cli")
        if args.args_file is not None:
            tool_args = json.loads(args.args_file.read_text(encoding="utf-8"))
        else:
            tool_args = json.loads(args.args_json)
        rpc_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": args.tool_name, "arguments": tool_args},
        }
        rpc_resp = handle_jsonrpc(server, rpc_req)
        print(json.dumps(rpc_resp, indent=2))
        is_err = bool(
            rpc_resp
            and (
                "error" in rpc_resp
                or rpc_resp.get("result", {}).get("isError")
            )
        )
        return 1 if is_err else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

