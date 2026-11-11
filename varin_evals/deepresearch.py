"""Native research reports, scored with the original DeepResearch Bench II rubric evaluator."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading

from .configuration import load_configuration, model_bindings
from .sources import source_revision


def main(args):
    source, runtime = args.source.resolve(strict=True), args.runtime.resolve(strict=True)
    output = args.output.resolve()
    config, environment = load_configuration(args.config, args.private_provider, args.private_aux_provider)
    tasks = [json.loads(line) for line in (source / "tasks_and_rubrics.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    # Current official aggregate excludes these two tasks, keeping the released
    # prompt/rubric file intact. Selection is frozen before any agent runs.
    tasks = [task for task in tasks if task["idx"] not in {112, 114}]
    if args.tasks != "all":
        selected = {int(value) for value in args.tasks.split(",")}
        unknown = selected - {task["idx"] for task in tasks}
        if unknown:
            raise ValueError(f"Tasks outside the selected release scope: {sorted(unknown)}")
        tasks = [task for task in tasks if task["idx"] in selected]
    if not tasks:
        raise ValueError("No tasks selected")
    manifest = {"benchmark": "DeepResearch Bench II", "sourceRevision": source_revision(source),
        "modelBindings": model_bindings(config), "tasks": [task["idx"] for task in tasks],
        "excludedByOfficialScope": [112, 114], "attemptsPerTask": 1,
        "promptField": "prompt (including original blocked-reference instructions); rubrics are grader-only",
        "judge": {"model": config["model"]["modelId"], "thinkingLevel": config["model"].get("thinkingLevel"),
                  "configuration": "custom; official judge is GPT-5.5; original rubric, completeness validation and metrics retained"},
        "evaluatorAdjustments": [{"function": "_try_clean_and_load", "behavior": "strict json.loads",
            "reason": "Original regex quote repair corrupts valid JSON string values followed by another key; rubric validation and scores are unchanged"}],
        "results": []}
    if args.dry_run:
        print(json.dumps({"tasks": len(tasks), "sourceRevision": manifest["sourceRevision"], "customJudge": manifest["judge"]}))
        return
    output.mkdir()
    lock = threading.Lock()
    private = Path(tempfile.mkdtemp(prefix="varin-deepresearch-"))
    (private / "judge-receipts.jsonl").touch(mode=0o600)
    sys.path.insert(0, str(source))
    evaluator = importlib.import_module("run_evaluation")
    evaluator._try_clean_and_load = json.loads
    metrics = importlib.import_module("aggregate_scores")
    from gpt_client import GPTClient, GPTOutput
    import requests

    class CustomJudge(GPTClient):
        def query(self, input_data):
            payload = {"model": self.model, "messages": [{"role": "user", "content": input_data.text}],
                "thinking": {"type": "enabled"}, "reasoning_effort": self.reasoning_effort,
                "max_tokens": self.max_output_tokens, "stream": False}
            response = requests.post(self.api_url, headers={"Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json"}, json=payload, timeout=self.timeout)
            with lock:
                with (private / "judge-receipts.jsonl").open("a") as receipt:
                    receipt.write(json.dumps({"status": response.status_code, "response": response.text}) + "\n")
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            return GPTOutput(text=content or "", raw_response=data, usage_metadata={
                "promptTokenCount": usage.get("prompt_tokens"), "candidatesTokenCount": usage.get("completion_tokens"),
                "totalTokenCount": usage.get("total_tokens"), "thoughtsTokenCount": details.get("reasoning_tokens", 0)})

    evaluator.client = CustomJudge(api_url=environment["VARIN_EVAL_BASE_URL"].rstrip("/") + "/chat/completions",
        api_key=environment["VARIN_EVAL_API_KEY"], model=config["model"]["modelId"],
        reasoning_effort=config["model"].get("thinkingLevel", "high"), verbose=False)
    manifest["privateJudgeReceipts"] = str(private)

    def save():
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    save()

    def worker(task):
        directory = output / f"idx-{task['idx']}"
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
        report = workspace / "report.md"
        selected = json.loads(json.dumps(config))
        selected["workFocus"] = "research"
        instruction = task["prompt"] + "\n\nSave the final research report as report.md in the workspace. Include source citations and URLs in that file."
        row = {"task": task["idx"], "agentCompleted": False, "artifactPresent": False, "graded": False}
        phase = "execute"
        try:
            with tempfile.TemporaryDirectory(prefix="varin-research-input-") as temporary:
                inputs = Path(temporary)
                (inputs / "config.json").write_text(json.dumps(selected))
                (inputs / "task.txt").write_text(instruction, encoding="utf-8")
                completed = subprocess.run([str(runtime / "bin/node"), str(Path(__file__).with_name("run.mjs")),
                    "--runtime", str(runtime), "--config", str(inputs / "config.json"), "--task", str(inputs / "task.txt"),
                    "--workdir", str(workspace), "--output", str(directory / "agent")], env=environment, capture_output=True, text=True)
            row["agentCompleted"] = completed.returncode == 0
            row["artifactPresent"] = report.is_file() and report.stat().st_size > 0
            if row["artifactPresent"]:
                phase = "grade"
                _, result, _ = evaluator.process_one_with_chunking(task["idx"], str(report), task["content"],
                    chunk_size=50, max_retries=5)
                (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                row["graded"] = "error" not in result
                if row["graded"]:
                    row["metrics"] = metrics.compute_dimension_averages(result)
            phase = "export"
            if (directory / "agent/histories.json").exists():
                from .trajectory import export_trajectory
                (directory / "trajectory.json").write_text(export_trajectory(directory / "agent").model_dump_json(indent=2))
        except Exception as error:
            # Full diagnostics stay private; public-facing manifests identify
            # the phase and exception class without copying provider URLs.
            with lock:
                with (private / "errors.jsonl").open("a") as diagnostic:
                    diagnostic.write(json.dumps({"task": task["idx"], "phase": phase, "error": str(error)}) + "\n")
            row["adapterError"] = {"phase": phase, "type": type(error).__name__}
        finally:
            with lock:
                manifest["results"].append(row)
                save()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(worker, tasks))
    graded = [row["metrics"] for row in manifest["results"] if row["graded"]]
    summary = {"selectedTasks": len(tasks), "gradedTasks": len(graded), "customJudge": manifest["judge"],
        "meanValidTaskScores": {key: statistics.mean(values) if (values := [row[key] for row in graded if row.get(key) is not None]) else None
                                for key in ["inforecall", "analysis", "presentation", "total", "blocked_rate"]}}
    (output / "scores.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--private-provider", type=Path)
    parser.add_argument("--private-aux-provider", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    main(parser.parse_args())
