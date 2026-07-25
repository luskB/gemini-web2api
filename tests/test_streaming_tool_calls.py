import io
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

from gemini_web2api import server
from gemini_web2api.server import GeminiHandler
from gemini_web2api.tools import iter_stream_events


_STANDALONE_SPEC = importlib.util.spec_from_file_location(
    "standalone_gemini_web2api", Path(__file__).resolve().parents[1] / "gemini_web2api.py"
)
standalone = importlib.util.module_from_spec(_STANDALONE_SPEC)
_STANDALONE_SPEC.loader.exec_module(standalone)


class StreamingToolCallDecoderTests(unittest.TestCase):
    def test_tool_call_split_across_deltas_becomes_one_structured_event(self):
        events = list(iter_stream_events([
            "Checking now. ```tool_",
            'call\n{"name":"weather",',
            '"arguments":{"city":"Tokyo"}}\n```',
        ]))

        self.assertEqual(events, [
            ("content", "Checking now. "),
            ("tool_calls", [{"name": "weather", "arguments": '{"city": "Tokyo"}'}]),
        ])


class StreamingToolCallHandlerTests(unittest.TestCase):
    TOOL = {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get the weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }

    def _handler(self):
        handler = object.__new__(GeminiHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        handler.headers = {"User-Agent": "streaming test"}
        return handler

    def test_request_with_tools_streams_text_without_buffering(self):
        handler = self._handler()
        request = {
            "model": "gemini-3.6-flash",
            "stream": True,
            "tools": [self.TOOL],
            "messages": [{"role": "user", "content": "Say hello."}],
        }

        with mock.patch.object(server, "generate_stream", return_value=iter(["one", " two"])), \
             mock.patch.object(server, "generate", side_effect=AssertionError("stream path must not buffer")):
            handler._handle_chat(json.dumps(request).encode())

        chunks = [
            json.loads(line[6:])
            for line in handler.wfile.getvalue().decode().splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(
            [chunk["choices"][0]["delta"].get("content") for chunk in chunks[:-1]],
            ["one", " two"],
        )
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(handler.wfile.getvalue().endswith(b"data: [DONE]\n\n"))


class StandaloneStreamingToolCallHandlerTests(unittest.TestCase):
    TOOL = StreamingToolCallHandlerTests.TOOL
    CALL = '```tool_call\n{"name":"weather","arguments":{"city":"Tokyo"}}\n```'

    def _handler(self):
        handler = object.__new__(standalone.GeminiHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        handler.headers = {"User-Agent": "streaming test"}
        return handler

    def test_request_with_tools_emits_a_structured_tool_call(self):
        handler = self._handler()
        request = {
            "model": "gemini-3.6-flash",
            "stream": True,
            "tools": [self.TOOL],
            "messages": [{"role": "user", "content": "Check Tokyo weather."}],
        }

        with mock.patch.object(standalone, "gemini_stream_generate_iter", return_value=iter([self.CALL])), \
             mock.patch.object(standalone, "gemini_stream_generate", side_effect=AssertionError("stream path must not buffer")):
            handler.handle_chat(json.dumps(request).encode())

        chunks = [
            json.loads(line[6:])
            for line in handler.wfile.getvalue().decode().splitlines()
            if line.startswith("data: {")
        ]
        tool_delta = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tool_delta["type"], "function")
        self.assertEqual(tool_delta["function"], {"name": "weather", "arguments": '{"city": "Tokyo"}'})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")


if __name__ == "__main__":
    unittest.main()
