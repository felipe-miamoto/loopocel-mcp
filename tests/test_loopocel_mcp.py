"""Regression and unit tests for loopocel-mcp."""

from __future__ import annotations

import glob
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from loopocel_mcp.adapters.claude_code import ingest_transcript, record_hook_event
from loopocel_mcp.adapters.llm_proxy import LLMRecordingRecorder, reassemble_sse_response
from loopocel_mcp.adapters.tool_proxy import MCPStdioWrapper, ToolCallProxy
from loopocel_mcp.convert import convert, summarize_log
from loopocel_mcp.figure import cohort_from_log, render_figure, render_session_figure
from loopocel_mcp.process_graphs import discover_ocdfg, discover_semantic_process
from loopocel_mcp.server import LoopOCELServer, handle_jsonrpc, run_stdio
from loopocel_mcp.validate import VerificationError, reconstruct, validate_log, validate_schema


OCEL4AGENT_RESULTS = Path(__file__).resolve().parents[2] / "OCEL4Agent" / "results"


class ConverterRegressionTests(unittest.TestCase):
    def test_reproduces_all_46_ocel4agent_captures_byte_for_byte(self) -> None:
        if not OCEL4AGENT_RESULTS.is_dir():
            self.skipTest("OCEL4Agent results directory not present")
        pairs = sorted(glob.glob(str(OCEL4AGENT_RESULTS / "**" / "capture.jsonl"), recursive=True))
        self.assertEqual(len(pairs), 46)
        for cap_str in pairs:
            cap_path = Path(cap_str)
            expected_bytes = (cap_path.parent / "loopocel.json").read_bytes()
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                shutil.copy2(cap_path, tmp_dir / "capture.jsonl")
                log = convert(tmp_dir)
                actual_bytes = (tmp_dir / "loopocel.json").read_bytes()
                self.assertEqual(actual_bytes, expected_bytes, f"Byte mismatch for {cap_path}")
                schema_report = validate_schema(log)
                self.assertTrue(schema_report["passed"], schema_report)

    def test_primary_freight_trace_reconstructs_and_validates(self) -> None:
        gemma04 = OCEL4AGENT_RESULTS / "freight-round1" / "gemma-04" / "loopocel.json"
        if not gemma04.is_file():
            self.skipTest("gemma-04 fixture not found")
        ocel = json.loads(gemma04.read_text(encoding="utf-8"))
        report = validate_log(ocel, ocel_path=gemma04)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["ontology"]["counts"]["turns"], 6)
        self.assertEqual(report["ontology"]["counts"]["requests"], 5)

    def test_detects_cycle_and_domain_range_violations(self) -> None:
        gemma04 = OCEL4AGENT_RESULTS / "freight-round1" / "gemma-04" / "loopocel.json"
        if not gemma04.is_file():
            self.skipTest("gemma-04 fixture not found")
        ocel = json.loads(gemma04.read_text(encoding="utf-8"))
        ocel["objects"][6]["relationships"][0]["objectId"] = "order:O1"
        with self.assertRaises(VerificationError):
            reconstruct(ocel)


class MCPServerAndLiveCaptureTests(unittest.TestCase):
    def test_full_mcp_session_lifecycle_and_jsonrpc_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = LoopOCELServer(sessions_root=Path(tmp))

            start_res = server.session_start(
                task="general-agent-demo",
                agent="antigravity",
                initial_objects={
                    "entity:E1": {"kind": "Entity", "status": "open"},
                },
                instruction="Inspect entity:E1 and update its status to processed.",
            )
            sid = start_res["session_id"]

            t1 = server.record_turn(
                session_id=sid,
                response_message="I will inspect entity:E1 first.",
                actions=[
                    {
                        "name": "inspect_entity",
                        "arguments": {"entity_id": "entity:E1"},
                        "result": {"id": "entity:E1", "kind": "Entity", "status": "open"},
                        "targets": ["entity:E1"],
                        "acted_on": ["entity:E1"],
                        "describes": ["entity:E1"],
                    }
                ],
            )
            self.assertEqual(t1["turn_id"], "turn:1")
            self.assertEqual(t1["actions_recorded"], 1)

            t2 = server.record_turn(
                session_id=sid,
                response_message="Now updating entity:E1 to processed.",
                actions=[
                    {
                        "name": "update_entity",
                        "arguments": {"entity_id": "entity:E1", "status": "processed"},
                        "result": {"ok": True, "id": "entity:E1", "kind": "Entity", "status": "processed"},
                        "targets": ["entity:E1"],
                        "acted_on": ["entity:E1"],
                        "describes": ["entity:E1"],
                        "changes": [
                            {
                                "id": "entity:E1",
                                "state": {"kind": "Entity", "status": "processed"},
                                "created": False,
                            }
                        ],
                    }
                ],
            )
            self.assertEqual(t2["turn_id"], "turn:2")

            t3 = server.record_turn(
                session_id=sid,
                response_message="Task complete: entity:E1 is processed.",
            )
            self.assertEqual(t3["turn_id"], "turn:3")

            end_res = server.session_end(sid)
            self.assertTrue(end_res["validation"]["passed"], end_res["validation"])
            self.assertEqual(end_res["summary"]["coverage"]["classes_covered"], 9)
            self.assertTrue(Path(end_res["ocel_path"]).is_file())
            self.assertTrue(Path(end_res["figure_outputs"]["svg"]).is_file())
            self.assertTrue(Path(end_res["figure_outputs"]["png"]).is_file())

            # Test JSON-RPC stdio round-trip
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_figure", "arguments": {"session_id": sid, "format": "png"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": f"loopocel://sessions/{sid}/summary"}},
            ]
            stdin_buf = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
            stdout_buf = io.StringIO()
            run_stdio(server, stdin=stdin_buf, stdout=stdout_buf)
            responses = [json.loads(line) for line in stdout_buf.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(responses), 4)
            self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "loopocel-mcp")
            tool_names = {t["name"] for t in responses[1]["result"]["tools"]}
            self.assertIn("session_start", tool_names)
            self.assertIn("record_turn", tool_names)
            self.assertIn("get_figure", tool_names)
            fig_content = responses[2]["result"]["content"]
            self.assertEqual(fig_content[1]["type"], "image")
            self.assertEqual(fig_content[1]["mimeType"], "image/png")

    def test_process_graphs_tool_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = LoopOCELServer(sessions_root=Path(tmp))
            sid = server.session_start(
                task="fix failing test",
                initial_objects={"file:app.py": {"status": "broken"}},
                instruction="Fix app.py",
            )["session_id"]
            server.record_turn(
                session_id=sid,
                response_message="Run tests, then edit.",
                actions=[
                    {"name": "run_tests", "arguments": {}, "outcome": "failure", "targets": ["file:app.py"]},
                    {
                        "name": "edit_file",
                        "arguments": {"path": "app.py"},
                        "targets": ["file:app.py"],
                        "changes": [{"id": "file:app.py", "state": {"status": "fixed"}, "created": False}],
                    },
                    {"name": "run_tests", "arguments": {}, "targets": ["file:app.py"]},
                    {
                        "name": "decide_done",
                        "internal": True,
                        "arguments": {},
                        "result": {"done": True},
                        "acted_on": ["file:app.py"],
                        "changes": {"file:app.py": {"state": {"status": "fixed", "reviewed": True}, "created": True}},
                    },
                ],
            )
            end_res = server.session_end(sid)
            for view in ("ocdfg", "semantic"):
                self.assertTrue(Path(end_res["process_graph_outputs"][view]["svg"]).is_file())
                self.assertTrue(Path(end_res["process_graph_outputs"][view]["png"]).is_file())

            log = json.loads(Path(end_res["ocel_path"]).read_text(encoding="utf-8"))
            sem = discover_semantic_process([log])
            self.assertEqual(sem["variants"], {"run_tests [failed] -> edit_file -> run_tests -> decide_done": 1})
            self.assertEqual(sem["activities"]["edit_file"]["kind"], "mutating")
            self.assertTrue(sem["activities"]["decide_done"]["internal"])
            self.assertFalse(sem["activities"]["edit_file"]["internal"])
            self.assertEqual(sem["objects"]["file"]["final_states"], {"fixed": 1})
            ocdfg = discover_ocdfg([log])
            self.assertIn("Turn.Started", ocdfg["activities"])
            self.assertTrue(any(e["object_type"] == "Turn" for e in ocdfg["edges"]))

            # Default session is the most recent one; both graphs come back as images.
            resp = handle_jsonrpc(
                server,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_process_graphs", "arguments": {}}},
            )
            content = resp["result"]["content"]
            self.assertEqual([c["type"] for c in content], ["text", "image", "image"])
            self.assertNotIn("base64", content[0]["text"])

            # A script that imports the package and replays turns in one burst is flagged.
            prov = end_res["provenance"]
            self.assertEqual(prov["recorded_via"], "python-api")
            self.assertFalse(prov["live"])
            self.assertEqual(len(prov["warnings"]), 1)

            prompt = server.get_prompt("process-graphs", {"task": "Book a table"})
            text = prompt["messages"][0]["content"]["text"]
            self.assertIn("session_start", text)
            self.assertIn("get_process_graphs", text)
            short = server.get_prompt("process-graphs", {})["messages"][0]["content"]["text"]
            self.assertIn("most recent", short)

    def test_provenance_flags_burst_replay_over_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = LoopOCELServer(sessions_root=Path(tmp), transport="mcp-stdio")
            sid = server.session_start(task="t", instruction="do it")["session_id"]
            for i in range(3):
                server.record_turn(
                    session_id=sid,
                    response_message=f"step {i}",
                    actions=[{"name": "search_web", "arguments": {"q": i}, "changes": {"item:1": {"n": i}}}],
                )
            prov = server.session_end(sid)["provenance"]
            self.assertEqual(prov["recorded_via"], "mcp-stdio")
            self.assertFalse(prov["live"])
            self.assertIn("replayed", prov["warnings"][0])


class AdaptersAndFigureTests(unittest.TestCase):
    def test_claude_code_hooks_and_transcript_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "cc-session"
            sdir.mkdir()
            record_hook_event(
                sdir,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_use_id": "toolu_01",
                    "tool_name": "search_items",
                },
            )
            transcript = Path(tmp) / "transcript.jsonl"
            transcript_lines = [
                {
                    "timestamp": "2026-11-09T12:00:00Z",
                    "message": {"role": "user", "content": "Find order_id order:101 and check item_id item:55."},
                },
                {
                    "timestamp": "2026-11-09T12:00:01Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_01",
                                "name": "lookup_order",
                                "input": {"order_id": "order:101"},
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-11-09T12:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": json.dumps({"order_id": "order:101", "item_id": "item:55", "status": "shipped"}),
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-11-09T12:00:03Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet",
                        "content": [{"type": "text", "text": "Order 101 with item 55 is shipped."}],
                    },
                },
            ]
            transcript.write_text("\n".join(json.dumps(r) for r in transcript_lines) + "\n", encoding="utf-8")

            ingest_transcript(sdir, transcript, task_id="check-order")
            log = convert(sdir)
            report = validate_log(log, ocel_path=sdir / "loopocel.json")
            self.assertTrue(report["passed"], report)
            summary = summarize_log(log)
            self.assertEqual(summary["classes"]["Turn"], 2)
            self.assertEqual(summary["classes"]["ActionRequest"], 1)
            self.assertEqual(summary["classes"]["ProcessObject"], 2)
            self.assertTrue(summary["partial_prompts"])

            fig_res = render_session_figure(sdir)
            svg_text = Path(fig_res["outputs"]["svg"]).read_text(encoding="utf-8")
            self.assertIn("Prompt*", svg_text)
            self.assertIn("ProcessObject.Registered [ext]", svg_text)

    def test_llm_proxy_recorder_and_tool_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "proxy-session"
            rec = LLMRecordingRecorder(pdir, task_id="proxy-test")
            rec.record_exchange(
                "/v1/messages",
                {
                    "model": "claude-sonnet",
                    "system": "You are a helpful assistant.",
                    "messages": [{"role": "user", "content": "Check user_id user:U9"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_99",
                            "name": "get_user",
                            "input": {"user_id": "user:U9"},
                        }
                    ],
                },
            )
            rec.record_exchange(
                "/v1/messages",
                {
                    "model": "claude-sonnet",
                    "system": "You are a helpful assistant.",
                    "messages": [
                        {"role": "user", "content": "Check user_id user:U9"},
                        {
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": "toolu_99", "name": "get_user", "input": {"user_id": "user:U9"}}],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": "toolu_99", "content": '{"user_id":"user:U9","tier":"gold"}'}],
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "User U9 is in the gold tier."}],
                },
            )
            log = convert(pdir)
            self.assertTrue(validate_log(log)["passed"])

            tdir = Path(tmp) / "tool-proxy-session"
            tproxy = ToolCallProxy(tdir, task_id="tool-proxy-test")
            out = tproxy.invoke("fetch_item", {"item_id": "item:I1"}, lambda args: {"item_id": args["item_id"], "stock": 12})
            self.assertEqual(out["stock"], 12)
            tlog = convert(tdir)
            self.assertTrue(validate_log(tlog)["passed"])

            # Multi-cohort comparison figure
            c1 = cohort_from_log(log, label="LLM Proxy")
            c2 = cohort_from_log(tlog, label="Tool Proxy", color="#2E6B4E")
            multi = render_figure([c1, c2], Path(tmp) / "comparison")
            self.assertTrue(Path(multi["outputs"]["svg"]).is_file())
            self.assertTrue(Path(multi["outputs"]["png"]).is_file())

    def test_prompts_sse_reassembly_and_mcp_stdio_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = LoopOCELServer(sessions_root=tmp)
            p_list = handle_jsonrpc(server, {"jsonrpc": "2.0", "id": 10, "method": "prompts/list"})
            assert p_list is not None
            prompt_names = {p["name"] for p in p_list["result"]["prompts"]}
            self.assertIn("record-task", prompt_names)
            self.assertIn("ingest-and-analyze", prompt_names)
            self.assertIn("process-graphs", prompt_names)

            p_get = handle_jsonrpc(
                server,
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "prompts/get",
                    "params": {"name": "record-task", "arguments": {"task": "Book flight to Vienna", "session_id": "s1"}},
                },
            )
            assert p_get is not None
            self.assertIn("session_start", p_get["result"]["messages"][0]["content"]["text"])

            # Test Anthropic SSE reassembly
            anth_sse = "\n".join(
                [
                    "event: message_start",
                    'data: {"type":"message_start","message":{"id":"msg_01","role":"assistant","content":[]}}',
                    "event: content_block_start",
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Checking flight..."}}',
                    "event: content_block_start",
                    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_sse1","name":"lookup_flight","input":{}}}',
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"flight_id\\": "}}',
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"flight:OS112\\"}"}}',
                    "event: message_stop",
                    'data: {"type":"message_stop"}',
                ]
            )
            anth_resp = reassemble_sse_response(anth_sse, is_anthropic=True)
            assert anth_resp is not None
            self.assertEqual(anth_resp["content"][0]["text"], "Checking flight...")
            self.assertEqual(anth_resp["content"][1]["input"]["flight_id"], "flight:OS112")

            # Test OpenAI SSE reassembly
            oai_sse = "\n".join(
                [
                    'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello "}}]}',
                    'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"content":"world","tool_calls":[{"index":0,"id":"call_sse1","function":{"name":"get_hotel","arguments":"{\\"hotel_id\\":"}}]}}]}',
                    'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hotel:H1\\"}"}}]}}]}',
                    "data: [DONE]",
                ]
            )
            oai_resp = reassemble_sse_response(oai_sse, is_anthropic=False)
            assert oai_resp is not None
            msg = oai_resp["choices"][0]["message"]
            self.assertEqual(msg["content"], "Hello world")
            self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"])["hotel_id"], "hotel:H1")

            # Test MCPStdioWrapper interception
            wdir = Path(tmp) / "wrapped-session"
            wrapper = MCPStdioWrapper(wdir, ["echo"], task_id="wrap-test")
            wrapper.observe_client_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 42,
                        "method": "tools/call",
                        "params": {"name": "reserve_room", "arguments": {"hotel_id": "hotel:H1"}},
                    }
                )
            )
            wrapper.observe_server_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 42,
                        "result": {
                            "content": [{"type": "text", "text": '{"hotel_id":"hotel:H1","status":"booked"}'}]
                        },
                    }
                )
            )
            fin = wrapper.finalize()
            assert fin is not None
            self.assertGreater(fin["objects"], 0)
            wlog = json.loads((wdir / "loopocel.json").read_text(encoding="utf-8"))
            self.assertTrue(validate_log(wlog)["passed"])

            # Verify automatic credential/secret redaction in Capture
            server.session_start(session_id="sec_test", task="Secret redaction check")
            server.record_turn(
                session_id="sec_test",
                response_message="Used token sk-ant-api03-1234567890abcdefghij and Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                parameters={"api_key": "super-secret-key", "model": "test-model"},
            )
            server.session_end(session_id="sec_test")
            sec_raw = (Path(tmp) / "sec_test" / "capture.jsonl").read_text(encoding="utf-8")
            sec_ocel = (Path(tmp) / "sec_test" / "loopocel.json").read_text(encoding="utf-8")
            for text_blob in (sec_raw, sec_ocel):
                self.assertNotIn("super-secret-key", text_blob)
                self.assertNotIn("sk-ant-api03-1234567890abcdefghij", text_blob)
                self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", text_blob)
                self.assertIn("[REDACTED]", text_blob)


if __name__ == "__main__":
    unittest.main()


