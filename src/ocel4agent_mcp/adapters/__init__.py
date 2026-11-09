"""Adapters for capturing LoopOCEL traces from agents."""

from .claude_code import claude_code_settings_snippet, ingest_transcript, record_hook_event
from .llm_proxy import LLMProxyServer, LLMRecordingRecorder, reassemble_sse_response
from .tool_proxy import MCPStdioWrapper, ToolCallProxy

__all__ = [
    "LLMProxyServer",
    "LLMRecordingRecorder",
    "MCPStdioWrapper",
    "ToolCallProxy",
    "claude_code_settings_snippet",
    "ingest_transcript",
    "reassemble_sse_response",
    "record_hook_event",
]

