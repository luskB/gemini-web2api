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


def sse_frames(data: bytes):
    frames = []
    for block in data.decode().split("\n\n"):
        if not block:
            continue
        event = None
        payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if event and payload:
            frames.append({"event": event, "data": payload})
    return frames


def google_sse_chunks(data: bytes):
    return [
        json.loads(line[6:])
        for line in data.decode().splitlines()
        if line.startswith("data: {")
    ]


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


class ResponsesStreamingTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(GeminiHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        handler.headers = {"User-Agent": "responses streaming test"}
        return handler

    def test_tools_request_streams_output_text_deltas(self):
        handler = self._handler()
        request = {
            "model": "gemini-3.6-flash",
            "stream": True,
            "tools": [StreamingToolCallHandlerTests.TOOL],
            "input": "Say hello.",
        }

        with mock.patch.object(server, "generate_stream", return_value=iter(["one", " two"])), \
             mock.patch.object(server, "generate", side_effect=AssertionError("stream path must not buffer")):
            handler._handle_responses(json.dumps(request).encode())

        frames = sse_frames(handler.wfile.getvalue())
        self.assertEqual(
            [frame["event"] for frame in frames],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [frame["data"]["delta"] for frame in frames if frame["event"] == "response.output_text.delta"],
            ["one", " two"],
        )
        self.assertEqual(frames[-1]["data"]["response"]["status"], "completed")

    def test_tools_request_streams_function_call_events(self):
        handler = self._handler()
        request = {
            "model": "gemini-3.6-flash",
            "stream": True,
            "tools": [StreamingToolCallHandlerTests.TOOL],
            "input": "Check Tokyo weather.",
        }

        with mock.patch.object(server, "generate_stream", return_value=iter([
            "```tool_",
            'call\n{"name":"weather","arguments":{"city":"Tokyo"}}\n```',
        ])), mock.patch.object(server, "generate", side_effect=AssertionError("stream path must not buffer")):
            handler._handle_responses(json.dumps(request).encode())

        frames = sse_frames(handler.wfile.getvalue())
        self.assertEqual(
            [frame["event"] for frame in frames],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        argument_delta = next(frame["data"]["delta"] for frame in frames if frame["event"] == "response.function_call_arguments.delta")
        self.assertEqual(json.loads(argument_delta), {"city": "Tokyo"})
        output = frames[-1]["data"]["response"]["output"]
        self.assertEqual(output[0]["type"], "function_call")
        self.assertEqual(output[0]["name"], "weather")

    def test_standalone_tools_request_streams_output_text_deltas(self):
        handler = object.__new__(standalone.GeminiHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        handler.headers = {"User-Agent": "standalone responses streaming test"}
        request = {
            "model": "gemini-3.6-flash",
            "stream": True,
            "tools": [StreamingToolCallHandlerTests.TOOL],
            "input": "Say hello.",
        }

        with mock.patch.object(standalone, "gemini_stream_generate_iter", return_value=iter(["one", " two"])), \
             mock.patch.object(standalone.GeminiHandler, "_call_gemini", side_effect=AssertionError("stream path must not buffer")):
            handler.handle_responses(json.dumps(request).encode())

        frames = sse_frames(handler.wfile.getvalue())
        self.assertEqual(
            [frame["event"] for frame in frames],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [frame["data"]["delta"] for frame in frames if frame["event"] == "response.output_text.delta"],
            ["one", " two"],
        )


class GoogleNativeStreamingTests(unittest.TestCase):
    GOOGLE_TOOL = {
        "functionDeclarations": [{
            "name": "weather",
            "description": "Get the weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }],
    }

    def _module_handler(self):
        handler = object.__new__(GeminiHandler)
        handler.path = "/v1beta/models/gemini-3.6-flash:streamGenerateContent"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        handler.headers = {"User-Agent": "google streaming test"}
        return handler

    def _standalone_handler(self):
        handler = object.__new__(standalone.GeminiHandler)
        handler.path = "/v1beta/models/gemini-3.6-flash:streamGenerateContent"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        handler.headers = {"User-Agent": "google standalone streaming test"}
        return handler

    def _request(self):
        return {
            "contents": [{"role": "user", "parts": [{"text": "Check Tokyo weather."}]}],
            "tools": [self.GOOGLE_TOOL],
        }

    def _assert_function_call_stream(self, chunks):
        self.assertEqual(chunks[0]["candidates"][0]["content"]["parts"], [{"text": "Checking. "}])
        call = chunks[1]["candidates"][0]["content"]["parts"][0]["functionCall"]
        self.assertEqual(call, {"name": "weather", "args": {"city": "Tokyo"}})
        self.assertEqual(chunks[-1]["candidates"][0]["finishReason"], "STOP")

    def test_tools_request_streams_google_function_call_without_buffering(self):
        handler = self._module_handler()
        with mock.patch.object(server, "generate_stream", return_value=iter([
            "Checking. ```function_",
            'call\n{"name":"weather","args":{"city":"Tokyo"}}\n```',
        ])), mock.patch.object(server, "generate", side_effect=AssertionError("stream path must not buffer")):
            handler._handle_google_generate(json.dumps(self._request()).encode(), stream=True)

        self._assert_function_call_stream(google_sse_chunks(handler.wfile.getvalue()))

    def test_standalone_tools_request_streams_google_function_call_without_buffering(self):
        handler = self._standalone_handler()
        with mock.patch.object(standalone, "gemini_stream_generate_iter", return_value=iter([
            "Checking. ```function_",
            'call\n{"name":"weather","args":{"city":"Tokyo"}}\n```',
        ])), mock.patch.object(standalone.GeminiHandler, "_call_gemini", side_effect=AssertionError("stream path must not buffer")):
            handler._handle_google_generate(json.dumps(self._request()).encode(), stream=True)

        self._assert_function_call_stream(google_sse_chunks(handler.wfile.getvalue()))


if __name__ == "__main__":
    unittest.main()
