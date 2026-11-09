"""Adapters for capturing LoopOCEL traces from agents."""

from .claude_code import claude_code_settings_snippet, ingest_transcript, record_hook_event
from .llm_proxy import LLMProxyServer, LLMRecordingRecorder
from .tool_proxy import ToolCallProxy

__all__ = [
    "LLMProxyServer",
    "LLMRecordingRecorder",
    "ToolCallProxy",
    "claude_code_settings_snippet",
    "ingest_transcript",
    "record_hook_event",
]

