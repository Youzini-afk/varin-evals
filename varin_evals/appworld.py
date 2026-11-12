"""Run Varin through the official AppWorld MCP, world lifecycle and state evaluator."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from .configuration import load_configuration, model_bindings
from .sources import source_revision
from .continuation import continue_results


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def control(base: str, route: str, body: dict | None = None):
    request = Request(base + route, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        return json.load(response)


def sdk_json(python: Path, root: Path, code: str, *arguments: str):
    completed = subprocess.run([str(python), "-c", code, *arguments],
                               env={**os.environ, "APPWORLD_ROOT": str(root)}, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout.splitlines()[-1])


def main(args) -> None:
    source = args.source.resolve(strict=True)
    data_root = args.data_root.resolve(strict=True)
    runtime = args.runtime.resolve(strict=True)
    python = args.appworld_python.absolute()  # Preserve a venv launcher symlink and its site-packages.
    config, environment = load_configuration(args.config, args.private_provider, args.private_aux_provider)
    output = args.output.resolve()
    output.mkdir()
    task_ids = sdk_json(python, data_root, "from appworld.task import load_task_ids; import sys,json; print(json.dumps(load_task_ids(sys.argv[1])))", args.split)
    if args.tasks != "all":
        selected = set(args.tasks.split(","))
        unknown = selected - set(task_ids)
        if unknown:
            raise ValueError(f"Tasks outside the selected split: {sorted(unknown)}")
        task_ids = [task_id for task_id in task_ids if task_id in selected]
    if not task_ids:
        raise ValueError("No tasks selected")
    experiment = args.experiment or output.name
    original_prompt = (source / "experiments/prompts/function_calling_agent/instructions.txt").read_text()
    task_protocol = original_prompt.split("# Key Instructions:", 1)[1].split("Next, I will show you", 1)[0]
    task_protocol = "\n".join(line for line in task_protocol.splitlines() if "{max_steps}" not in line).strip()
    manifest = {"benchmark": "AppWorld MCP", "sourceRevision": source_revision(source), "split": args.split,
                "modelBindings": model_bindings(config), "experiment": experiment, "tasks": task_ids,
                "taskProtocol": "Official function-calling prompt sections A/B/C; original scaffold turn limit omitted",
                "attemptsPerTask": 1, "mcpExposure": args.mcp_exposure, "results": []}
    manifest['results'], continuation = continue_results(args.continue_from, args.retry_tasks, manifest)
    if continuation:
        manifest['continuation'] = continuation
        old = json.loads((args.continue_from.resolve() / 'manifest.json').read_text())
        if old['experiment'] == experiment:
            raise ValueError('Continuation requires a new AppWorld experiment to preserve old state')
        # Keep the original owner's saved task databases intact; its evaluator
        # reads the same immutable valid state under the new experiment name.
        home = data_root / 'experiments/outputs'
        for row in manifest['results']:
            shutil.copytree(home / old['experiment'] / 'tasks' / row['task'],
                            home / experiment / 'tasks' / row['task'])
    retained = {row['task'] for row in manifest['results']}
    pending_ids = [task_id for task_id in task_ids if task_id not in retained]
    lock = threading.Lock()
    admission_paused = threading.Event()

    def save_manifest():
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    save_manifest()

    def worker(index: int, tasks: list[str]):
        if not tasks:
            return
        environment_port, apis_port, mcp_port = [free_port() for _ in range(3)]
        base, apis, mcp = [f"http://127.0.0.1:{port}" for port in (environment_port, apis_port, mcp_port)]
        private = Path(tempfile.mkdtemp(prefix="varin-appworld-"))
        server_log = (private / "servers.log").open("wb")
        command = [str(python), "-m", "appworld.cli", "serve", "multiple",
                   "--environment", f"--port {environment_port}", "--apis", f"--port {apis_port}",
                   "--mcp", f"http --port {mcp_port} --output-type content_only --remote-apis-url {apis}",
                   "--root", str(data_root)]
        server = subprocess.Popen(command, cwd=source, env={**environment, "APPWORLD_ROOT": str(data_root)},
                                  stdin=subprocess.DEVNULL, stdout=server_log, stderr=server_log, start_new_session=True)
        server_record = {"pid": server.pid, "ports": [environment_port, apis_port, mcp_port],
                         "state": "starting", "privateDiagnostics": str(private)}
        server_record_path = output / f"servers-{index}.json"
        server_record_path.write_text(json.dumps(server_record, indent=2) + "\n")
        try:
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"AppWorld servers exited; private diagnostics: {private}")
                try:
                    with urlopen(base + "/", timeout=1):
                        pass
                    # serve multiple launches these independently; environment HTTP
                    # readiness alone does not mean the API/MCP children are ready.
                    for port in (apis_port, mcp_port):
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            pass
                    break
                except OSError:
                    time.sleep(0.25)
            server_record["state"] = "ready"
            server_record_path.write_text(json.dumps(server_record, indent=2) + "\n")
            for task_id in tasks:
                if admission_paused.is_set():
                    break
                task_dir = output / task_id
                task_dir.mkdir()
                workdir = task_dir / "workspace"
                workdir.mkdir()
                phase, initialized = "prepare", False
                row = {"task": task_id, "agentCompleted": False, "verifierPassed": None}
                try:
                    task = control(base, "/initialize", {"task_id": task_id, "experiment_name": experiment,
                                                        "remote_apis_url": apis, "remote_mcp_url": mcp})["output"]
                    initialized = True
                    supervisor = task["supervisor"]
                    instruction = (task["instruction"] + "\n\n"
                                   + f"You are acting for {supervisor['first_name']} {supervisor['last_name']}. "
                                   + f"Their email is {supervisor['email']} and phone number is {supervisor['phone_number']}.\n"
                                   + "Use the connected appworld MCP server for application actions. "
                                   + "The world's phone time API is authoritative for current date and time.")
                    selected = json.loads(json.dumps(config))
                    selected["agentInstructions"] = task_protocol
                    selected["nativeTools"] = []
                    selected["mcp"] = {"mcpServers": {"appworld": {"url": mcp + "/mcp/", "exposure": args.mcp_exposure}}}
                    phase = "execute"
                    with tempfile.TemporaryDirectory(prefix="varin-appworld-input-") as temporary:
                        inputs = Path(temporary)
                        (inputs / "config.json").write_text(json.dumps(selected))
                        (inputs / "task.txt").write_text(instruction)
                        completed = subprocess.run([str(runtime / "bin/node"), str(Path(__file__).with_name("run.mjs")),
                                                    "--runtime", str(runtime), "--config", str(inputs / "config.json"),
                                                    "--task", str(inputs / "task.txt"), "--workdir", str(workdir),
                                                    "--output", str(task_dir / "agent")],
                                                   env=environment, capture_output=True, text=True)
                    row["agentCompleted"] = completed.returncode == 0
                    if not row['agentCompleted']:
                        admission_paused.set()
                    phase = "save"
                    control(base, "/save", {"task_id": task_id})
                    phase = "verify"
                    result = control(base, "/evaluate", {"task_id": task_id, "experiment_name": experiment})["output"]
                    (task_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                    row["verifierPassed"] = result["success"]
                    phase = "export"
                    from .trajectory import export_trajectory
                    if (task_dir / "agent/histories.json").exists():
                        trajectory = export_trajectory(task_dir / "agent")
                        (task_dir / "trajectory.json").write_text(trajectory.model_dump_json(indent=2))
                except Exception as error:
                    admission_paused.set()
                    failure = {"phase": phase, "error": str(error)}
                    (task_dir / "adapter-error.json").write_text(json.dumps(failure, indent=2) + "\n")
                    row["adapterError"] = failure
                finally:
                    if initialized:
                        try:
                            control(base, "/close", {"task_id": task_id})
                        except Exception as error:
                            admission_paused.set()
                            row["cleanupError"] = str(error)
                    with lock:
                        manifest["results"].append(row)
                        save_manifest()
        finally:
            if server.poll() is None:
                os.killpg(server.pid, signal.SIGTERM)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(server.pid, signal.SIGKILL)
                    server.wait()
            server_log.close()
            server_record.update(state="exited", exitCode=server.returncode)
            server_record_path.write_text(json.dumps(server_record, indent=2) + "\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(worker, index, pending_ids[index::args.workers]) for index in range(args.workers)]
        for future in futures:
            future.result()
    if admission_paused.is_set():
        raise RuntimeError('Batch paused after an execution or adapter failure; in-flight tasks finalized. Diagnose before continuing.')
    # The original evaluator owns TGC and SGC over the selected task list.
    scores = sdk_json(python, data_root,
                      "from appworld.evaluator import evaluate_tasks; import sys,json; print(json.dumps(evaluate_tasks(json.loads(sys.argv[1]), sys.argv[2], include_details=True)))",
                      json.dumps(task_ids), experiment)
    (output / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"tasks": len(task_ids), "aggregate": scores["aggregate"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--appworld-python", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--private-provider", type=Path)
    parser.add_argument("--private-aux-provider", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--split", default="test_normal")
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--retry-tasks", default='', help="Explicit comma-separated task IDs; preserves other terminal outcomes")
    parser.add_argument("--experiment")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--mcp-exposure", choices=["direct", "deferred"], default="deferred")
    main(parser.parse_args())
