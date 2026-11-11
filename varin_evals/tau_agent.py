"""Transport official tau tools/messages to one persistent native Varin session."""
import asyncio
from concurrent.futures import Future
from contextlib import asynccontextmanager
import json
from pathlib import Path
from queue import Queue
import socket
import subprocess
import tempfile
import threading
from uuid import uuid4

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool as MCPTool
from starlette.applications import Starlette
from starlette.routing import Mount
import uvicorn

from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall, ToolMessage, UserMessage


class VarinAgent(LLMAgent):
    def __init__(self, *, runtime: Path, configuration: dict, environment: dict,
                 output: Path, task_id: str, tools, domain_policy, llm, llm_args=None,
                 additional_stop_tool_names=frozenset()):
        super().__init__(tools, domain_policy, llm, llm_args, additional_stop_tool_names)
        self.runtime, self.configuration, self.environment = runtime, configuration, environment
        self.output = output / f"{task_id}-{uuid4().hex}"
        self.events, self.pending = Queue(), {}
        self.stopping = threading.Event()
        self.process = None
        self.closed = False
        self.shutdown = None

    def _start(self, state):
        self.output.mkdir(parents=True)
        (self.output / "workspace").mkdir()
        private = Path(tempfile.mkdtemp(prefix="varin-tau-"))
        functions = [tool.openai_schema["function"] for tool in self.tools]

        async def list_tools(_context, _parameters):
            return ListToolsResult(tools=[MCPTool(name=tool["name"], description=tool.get("description"),
                                                input_schema=tool["parameters"]) for tool in functions])

        async def call_tool(_context, parameters):
            if self.stopping.is_set():
                raise RuntimeError("The official simulation has ended")
            key, future = uuid4().hex, Future()
            self.pending[key] = future
            self.events.put({"kind": "tool", "id": key, "name": parameters.name,
                             "arguments": parameters.arguments or {}})
            try:
                result = await asyncio.wrap_future(future)
                return CallToolResult(content=[TextContent(type="text", text=result.content or "")],
                                      is_error=result.error)
            finally:
                self.pending.pop(key, None)

        mcp = Server("tau", on_list_tools=list_tools, on_call_tool=call_tool)
        manager = StreamableHTTPSessionManager(app=mcp, stateless=True)

        @asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))

        def serve():
            try:
                self.server.run(sockets=[listener])
            except Exception as error:
                self.events.put({"kind": "failure", "message": str(error)})
            finally:
                listener.close()

        self.server_thread = threading.Thread(target=serve, daemon=True)
        self.server_thread.start()
        selected = json.loads(json.dumps(self.configuration))
        selected["nativeTools"] = []
        selected["agentInstructions"] = self.system_prompt
        selected["mcp"] = {"mcpServers": {"tau": {"url": f"http://127.0.0.1:{port}/mcp/", "exposure": "deferred"}}}
        config_path = private / "config.json"
        config_path.write_text(json.dumps(selected))
        self.stderr = (private / "native-stderr.log").open("w")
        self.process = subprocess.Popen([str(self.runtime / "bin/node"), str(Path(__file__).with_name("session.mjs")),
            "--runtime", str(self.runtime), "--config", str(config_path), "--workdir", str(self.output / "workspace"),
            "--output", str(self.output / "native")], env=self.environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True)
        (self.output / "transport.json").write_text(json.dumps({"nativePid": self.process.pid,
            "privateDiagnostics": str(private), "protocol": "official tau tools over MCP; native final replies per user turn"}, indent=2))

        def read_native():
            try:
                for line in self.process.stdout:
                    event = json.loads(line)
                    if event["kind"] != "accepted":
                        self.events.put(event)
            except Exception as error:
                self.events.put({"kind": "failure", "message": str(error)})
            finally:
                self.events.put({"kind": "eof"})

        self.reader = threading.Thread(target=read_native, daemon=True)
        self.reader.start()

    def _command(self, command):
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()

    def _deliver(self, message):
        messages = message.tool_messages if isinstance(message, MultiToolMessage) else [message]
        for result in messages:
            future = self.pending.get(result.id)
            if future is None:
                raise RuntimeError(f"No pending native tool request for tau result {result.id}")
            future.set_result(result)

    def _generate_next_message(self, message, state):
        history = list(state.messages)
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)
        first = self.process is None
        if first:
            self._start(state)
        if isinstance(message, UserMessage):
            if message.is_audio:
                raise ValueError("This adapter implements text conversations")
            text = message.content or ""
            if history and first:
                text = "Existing conversation:\n" + "\n".join(str(item) for item in history) + "\n\nCustomer:\n" + text
            self._command({"kind": "submit", "text": text})
        else:
            self._deliver(message)
        while True:
            event = self.events.get()
            if event["kind"] == "tool":
                return AssistantMessage(role="assistant", tool_calls=[ToolCall(id=event["id"], name=event["name"],
                                                                              arguments=event["arguments"])])
            if event["kind"] == "message":
                if event["outcome"] != "completed" or not event["content"]:
                    raise RuntimeError(f"Native user turn ended {event['outcome']} without a final reply")
                return AssistantMessage.text(event["content"])
            raise RuntimeError(f"Native transport ended: {event}")

    def stop(self, message=None, state=None):
        if self.closed or self.process is None:
            return
        self.closed = True
        try:
            if isinstance(message, (ToolMessage, MultiToolMessage)):
                self._deliver(message)
            self.stopping.set()
            self._command({"kind": "close"})
            for future in list(self.pending.values()):
                if not future.done():
                    future.cancel()
            receipt = None
            while self.process.poll() is None or not self.events.empty():
                event = self.events.get()
                if event["kind"] == "closed":
                    receipt = event
                    break
                if event["kind"] == "eof":
                    break
            self.process.stdin.close()
            exit_code = self.process.wait()
            self.shutdown = {"receipt": receipt, "transportExitCode": exit_code}
            if not receipt or not receipt["exportsComplete"] or exit_code != 0 or receipt["hostExit"]["code"] != 0:
                raise RuntimeError(f"Native shutdown/export incomplete: {receipt}, transport exit {exit_code}")
        except Exception as error:
            self.shutdown = {**(self.shutdown or {}), "error": str(error)}
            raise
        finally:
            (self.output / "shutdown.json").write_text(json.dumps(self.shutdown, indent=2) + "\n")
            self.stopping.set()
            for future in list(self.pending.values()):
                if not future.done():
                    future.cancel()
            self.server.should_exit = True
            self.server_thread.join()
            self.stderr.close()
