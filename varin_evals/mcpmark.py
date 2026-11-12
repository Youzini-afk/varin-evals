"""Run native Varin against MCPMark's original Filesystem state and verifier owners."""
import argparse
from dataclasses import asdict
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

from .configuration import load_configuration, model_bindings
from .continuation import continue_results


def main(args) -> None:
    source = args.source.resolve(strict=True)
    sys.path.insert(0, str(source))
    from src.mcp_services.filesystem import FilesystemStateManager, FilesystemTaskManager

    runtime = args.runtime.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir()  # Every invocation is a distinct experiment, never an implicit re-attempt.
    config, environment = load_configuration(args.config, args.private_provider, args.private_aux_provider)
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        raise ValueError("MCPMark source has tracked edits; preserve and record the official task and verifier revision")
    manager = FilesystemTaskManager(task_suite=args.suite)
    tasks = manager.filter_tasks(args.tasks)
    if not tasks:
        raise ValueError(f"No tasks matched {args.tasks!r}")
    manifest = {"benchmark": "MCPMark Verified Filesystem", "suite": args.suite, "sourceRevision": revision,
                "model": config["model"], "modelBindings": model_bindings(config),
                "tasks": [f"{task.category_id}/{task.task_id}" for task in tasks],
                "mcpExposure": args.mcp_exposure, "attemptsPerTask": 1, "results": []}
    manifest['results'], continuation = continue_results(args.continue_from, args.retry_tasks, manifest)
    if continuation:
        manifest['continuation'] = continuation
    retained = {row['task'] for row in manifest['results']}
    tasks = [task for task in tasks if f'{task.category_id}/{task.task_id}' not in retained]
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for task in tasks:
        task_dir = output / task.name
        task_dir.mkdir()
        state = FilesystemStateManager()
        phase = "prepare"
        recorded = False
        try:
            if not state.set_up(task):
                raise RuntimeError("Official filesystem state preparation failed")
            workspace = state.get_test_directory()
            selected = json.loads(json.dumps(config))
            selected["nativeTools"] = []  # The MCPMark track exposes the pinned filesystem MCP tools.
            selected["mcp"] = {"mcpServers": {"filesystem": {
                "command": str(runtime / "bin/bun"),
                "args": ["x", "@modelcontextprotocol/server-filesystem@2025.12.18", str(workspace)],
                "exposure": args.mcp_exposure,
            }}}
            instruction = manager.get_task_instruction(task) + f"\n\nTest directory: {workspace}\nUse the connected filesystem MCP tools for file operations."
            phase = "execute"
            with tempfile.TemporaryDirectory(prefix="varin-mcpmark-") as temporary:
                private_run = Path(temporary)
                (private_run / "config.json").write_text(json.dumps(selected))
                (private_run / "task.txt").write_text(instruction)
                command = [str(runtime / "bin/node"), str(Path(__file__).with_name("run.mjs")),
                           "--runtime", str(runtime), "--config", str(private_run / "config.json"),
                           "--task", str(private_run / "task.txt"), "--workdir", str(workspace),
                           "--output", str(task_dir / "agent")]
                completed = subprocess.run(command, env=environment, capture_output=True, text=True)
            # The official owner also verifies a failed agent execution and keeps the two outcomes separate.
            phase = "verify"
            result = manager.execute_task(task, {"success": completed.returncode == 0,
                                                "error": "Varin execution failed; see agent/run.json" if completed.returncode else None})
            result_data = asdict(result)
            (task_dir / "result.json").write_text(json.dumps(result_data, ensure_ascii=False, indent=2) + "\n")
            shutil.copytree(workspace, task_dir / "files")
            manifest["results"].append({"task": f"{task.category_id}/{task.task_id}", "agentCompleted": completed.returncode == 0,
                                        "verifierPassed": result.success})
            recorded = True
            phase = "export"
            from .trajectory import export_trajectory
            if (task_dir / "agent/histories.json").exists():
                trajectory = export_trajectory(task_dir / "agent")
                (task_dir / "trajectory.json").write_text(trajectory.model_dump_json(indent=2))
        except Exception as error:
            failure = {"task": f"{task.category_id}/{task.task_id}", "phase": phase, "error": str(error)}
            (task_dir / "adapter-error.json").write_text(json.dumps(failure, indent=2) + "\n")
            if not recorded:
                manifest["results"].append({**failure, "agentCompleted": False, "verifierPassed": False})
            else:
                manifest["results"][-1]["exportError"] = str(error)
        finally:
            state.clean_up(task)
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if not manifest['results'][-1]['agentCompleted'] or manifest['results'][-1].get('exportError'):
            raise RuntimeError('Batch paused after an execution or export failure; task state and verifier result preserved. Diagnose before continuing.')
    print(json.dumps({"tasks": len(manifest['tasks']), "executed": len(tasks), "passed": sum(row["verifierPassed"] for row in manifest["results"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--private-provider", type=Path)
    parser.add_argument("--private-aux-provider", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--suite", choices=["standard", "easy"], default="easy")
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--retry-tasks", default='', help="Explicit comma-separated task IDs; preserves other terminal outcomes")
    parser.add_argument("--mcp-exposure", choices=["direct", "deferred"], default="direct",
                        help="Use Varin's original direct catalog or on-demand MCP discovery/call mode")
    main(parser.parse_args())
