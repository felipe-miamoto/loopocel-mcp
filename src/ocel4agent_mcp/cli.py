"""Command-line interface for ocel4agent-mcp."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .adapters.claude_code import ingest_transcript, main_hook_cli
from .adapters.llm_proxy import LLMProxyServer
from .convert import convert, summarize_log
from .figure import render_session_figure
from .server import LoopOCELServer, handle_jsonrpc, run_http, run_stdio
from .validate import validate_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocel4agent-mcp",
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

    conv_p = sub.add_parser("convert", help="Convert capture.jsonl in a directory to loopocel.json")
    conv_p.add_argument("directory", type=Path)

    val_p = sub.add_parser("validate", help="Validate loopocel.json in a directory")
    val_p.add_argument("directory", type=Path)
    val_p.add_argument("--pm4py", action="store_true")

    fig_p = sub.add_parser("figure", help="Render LoopOCEL coverage figure for a directory")
    fig_p.add_argument("directory", type=Path)
    fig_p.add_argument("--compare", type=Path, nargs="*", default=[])

    ing_p = sub.add_parser("ingest", help="Ingest an agent transcript into a session directory")
    ing_p.add_argument("session_dir", type=Path)
    ing_p.add_argument("transcript", type=Path)
    ing_p.add_argument("--task", default=None)

    proxy_p = sub.add_parser("proxy", help="Run local LLM recording proxy")
    proxy_p.add_argument("--session-dir", type=Path, required=True)
    proxy_p.add_argument("--upstream", required=True, help="Upstream API base URL (e.g. https://api.anthropic.com)")
    proxy_p.add_argument("--host", default="127.0.0.1")
    proxy_p.add_argument("--port", type=int, default=18080)
    proxy_p.add_argument("--task", default="proxy-task")

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
        return main_hook_cli(args.session_dir)

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

    if args.subcommand == "ingest":
        stats = ingest_transcript(args.session_dir, args.transcript, task_id=args.task)
        log = convert(args.session_dir)
        fig = render_session_figure(args.session_dir)
        print(json.dumps({**stats, "objects": len(log["objects"]), "events": len(log["events"]), "figure": fig["outputs"]}, indent=2))
        return 0

    if args.subcommand == "proxy":
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
        return 0

    if args.subcommand == "call":
        server = LoopOCELServer(sessions_root=args.sessions_root)
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

