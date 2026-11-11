"""Real Varin + deterministic local provider. Wiring check, not a benchmark score."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time


class Provider(BaseHTTPRequestHandler):
    tool_calls = 0
    initial_failures = 0
    stream_failures = 0

    def log_message(self, *_):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "smoke-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Background title requests receive text, never an effectful tool call.
        tools = {tool["function"]["name"] for tool in request.get("tools", [])}
        has_tool_result = any(message.get("role") == "tool" for message in request["messages"])
        if tools and not has_tool_result and type(self).initial_failures == 0:
            type(self).initial_failures += 1
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"injected transient failure"}}')
            return
        if tools and has_tool_result and type(self).stream_failures == 0:
            type(self).stream_failures += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"id":"interrupted","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        if tools and not has_tool_result:
            arguments = {"cwd": "", "command": "/bin/sh", "args": ["-c", "echo varin-evals-smoke >> result.txt"],
                         "mode": "pipe", "env": [], "waitMs": 1000}
            name = "process_spawn"
            if name not in tools:
                if "tool_invoke" not in tools:
                    raise ValueError(f"No executable process tool in native directory: {sorted(tools)}")
                name, arguments = "tool_invoke", {"name": name, "arguments": arguments}
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "smoke-call", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            finish = "tool_calls"
            type(self).tool_calls += 1
        else:
            delta, finish = {"role": "assistant", "content": "Varin eval smoke complete."}, "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        common = {"id": "smoke-response", "object": "chat.completion.chunk", "created": int(time.time()), "model": "smoke-model"}
        chunks = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                  {**common, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                   "total_tokens": 120, "prompt_tokens_details": {"cached_tokens": 0}}}]
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main(runtime: Path, output: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        configuration = {
            "model": {"providerId": "smoke", "modelId": "smoke-model"},
            "models": {"channels": [{"id": "smoke", "name": "Deterministic smoke fixture", "type": "openai-compatible",
                       "enabled": True, "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                       "authentication": {"kind": "none"}, "parameters": {},
                       "models": [{"id": "smoke-model", "name": "smoke-model", "enabled": True,
                                   "operations": ["chat"], "contextWindow": 32768, "maxOutputTokens": 2048}]}]},
            "agentSettings": {"harness": {"permissions": {"mode": "bypass", "rules": []}}},
            "workFocus": "code",
        }
        (root / "config.json").write_text(json.dumps(configuration))
        (root / "task.txt").write_text("Write result.txt containing exactly varin-evals-smoke followed by a newline.")
        runner = Path(__file__).resolve().parents[1] / "varin_evals/run.mjs"
        try:
            subprocess.run([str(runtime / "bin/node"), str(runner), "--runtime", str(runtime),
                            "--config", str(root / "config.json"), "--task", str(root / "task.txt"),
                            "--workdir", str(workspace), "--output", str(output)], check=True, timeout=180)
            assert (workspace / "result.txt").read_text() == "varin-evals-smoke\n", "The original resource owner did not write the expected file"
            assert Provider.tool_calls == 1, "The effectful tool call was not executed exactly once"
            assert Provider.initial_failures == 1 and Provider.stream_failures == 1
            from varin_evals.trajectory import export_trajectory
            trajectory = export_trajectory(output)
            (output / "trajectory.json").write_text(trajectory.model_dump_json(indent=2))
            assert any(step.tool_calls and step.observation for step in trajectory.steps)
            print(json.dumps({"kind": "integration-smoke", "outcome": "passed", "toolCalls": Provider.tool_calls,
                              "varinRevision": json.loads((output / "run.json").read_text())["runtime"]["varin"]["sourceRevision"],
                              "provider": "deterministic local fixture; token counts are simulated",
                              "injectedFailures": ["http_503", "stream_interrupted_after_tool"], "benchmarkScore": None}))
        finally:
            server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    main(arguments.runtime.resolve(), arguments.output.resolve())
