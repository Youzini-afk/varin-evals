"""Run the released tau-bench text frame using native Varin and official scoring."""
import argparse
import json
import os
from pathlib import Path

from .configuration import load_configuration, model_bindings
from .sources import source_revision


def main(args):
    from tau2.data_model.semantic_communication import CommunicateScoring, SemanticCommunicateSettings
    from tau2.data_model.simulation import TextRunConfig
    from tau2.evaluator import evaluator_nl_assertions
    from tau2.registry import registry
    from tau2.runner.batch import run_domain
    from tau2.runner.presets import BenchmarkRequest, resolve_benchmark
    from .tau_agent import VarinAgent

    config, environment = load_configuration(args.config, args.private_provider, args.private_aux_provider)
    runtime, source = args.runtime.resolve(strict=True), args.source.resolve(strict=True)
    output = args.output.resolve()
    os.chdir(source)  # Upstream records its source revision from the current directory.
    # LiteLLM's OpenAI provider reads these at request time. Neither credentials
    # nor the custom endpoint enter the released frame, plans or run arguments.
    os.environ.update(OPENAI_API_BASE=environment["VARIN_EVAL_BASE_URL"],
                      OPENAI_API_KEY=environment["VARIN_EVAL_API_KEY"])
    model = "openai/" + config["model"]["modelId"]
    effort = config["model"].get("thinkingLevel", "high")
    llm_args = {"extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": effort}}
    # Upstream exposes the semantic judge as run config, but NL assertions
    # still read module constants. This explicit per-process override changes
    # only the authorized custom judge model/arguments, not its prompt/rules.
    evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = model
    evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = llm_args

    cell_output = output / "native"

    def factory(tools, domain_policy, llm, llm_args, task, additional_stop_tool_names=frozenset(), **_unused):
        return VarinAgent(runtime=runtime, configuration=config, environment=environment,
                          output=cell_output / current_cell[0], task_id=task.id,
                          tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args,
                          additional_stop_tool_names=additional_stop_tool_names)

    registry.register_agent_factory(factory, name="varin")
    template = TextRunConfig(agent="varin", llm_agent=model, llm_args_agent={},
        llm_user=model, llm_args_user=llm_args, num_trials=1, workers=0,
        max_concurrency=args.concurrency, max_retries=0, verbose_logs=False,
        communicate_scoring=CommunicateScoring(judge=SemanticCommunicateSettings(model=model, model_args=llm_args)),
        **({"num_tasks": args.num_tasks} if args.num_tasks else {}))
    plan = resolve_benchmark(BenchmarkRequest(preset="text", template=template, output=output,
        languages=args.languages.split(",") if args.languages else None,
        domains=args.domains.split(",") if args.domains else None))
    if args.dry_run:
        print(json.dumps({"cells": len(plan.configs), "calls": sum(len(cell.task_ids) for cell in plan.configs),
                          "partial": plan.partial}))
        return
    output.mkdir(parents=True, exist_ok=True)
    (output / "benchmark-frame.json").write_text(plan.frame.model_dump_json(indent=2))
    (output / "benchmark-plan.json").write_text(plan.model_dump_json(indent=2))
    manifest = {"benchmark": "tau-bench text-v1", "sourceRevision": source_revision(source),
        "modelBindings": model_bindings(config), "customConfiguration": {
            "userSimulator": {"model": model, "arguments": llm_args},
            "semanticJudge": {"model": model, "arguments": llm_args},
            "nlAssertionJudge": {"model": model, "arguments": llm_args},
            "transport": "official domain tools over MCP; native session persists across customer turns",
            "nativeToolCalls": "delivered individually in execution order; official orchestrator executes effects",
            "usage": "native session ledger is authoritative; upstream unknown model costs may be zero"},
        "attemptsPerTask": 1, "partial": plan.partial, "cells": []}
    current_cell = [""]
    try:
        for cell in plan.configs:
            name = f"{cell.language}-{cell.domain}"
            current_cell[0] = name
            row = {"cell": name, "tasks": len(cell.task_ids), "state": "running"}
            manifest["cells"].append(row)
            (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            results = run_domain(cell)
            row.update(state="finished", completed=len(results.simulations))
    finally:
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--private-provider", type=Path)
    parser.add_argument("--private-aux-provider", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--languages")
    parser.add_argument("--domains")
    parser.add_argument("--num-tasks", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    main(parser.parse_args())
