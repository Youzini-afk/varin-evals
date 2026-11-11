"""ATIF projection of original native history, with original call/result identities."""
import json
from pathlib import Path
import re

from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import (
    Agent, FinalMetrics, Observation, ObservationResult, Step, ToolCall, Trajectory,
)


def token_sum(rows: list[dict], field: str) -> int | None:
    total = 0
    if not rows:
        return None
    for row in rows:
        counter = row.get(field)
        if not counter or counter.get("known") is None or counter.get("missing", 0):
            return None
        total += int(counter["known"])
    return total


def usage_totals(usage: dict) -> dict:
    rows = usage["rows"]
    return {
        # Native completeInputTokens already includes provider-specific cache buckets.
        "n_input_tokens": token_sum(rows, "completeInputTokens"),
        "n_output_tokens": token_sum(rows, "outputTokens"),
        "n_cache_tokens": token_sum(rows, "cachedInputTokens"),
        "cost_usd": (sum(row["costUsd"] for row in rows)
                     if rows and all(row["costUnknownRequests"] == 0 for row in rows) else None),
    }


def apply_usage(context: AgentContext, usage: dict) -> None:
    for key, value in usage_totals(usage).items():
        setattr(context, key, value)


def request_identity(item: dict) -> str:
    identity = item["content"]["id"]
    match = re.match(r"model-item:(\d+):", identity)
    if match is None:
        raise ValueError(f"Tool call has no original model request identity: {item['id']}")
    return identity[match.end():].encode()[:int(match[1])].decode()


def text_parts(output: dict) -> str:
    return "\n".join(part["text"] for part in output.get("parts", []) if part.get("kind") == "text")


def history_steps(items: list[dict]) -> list[Step]:
    steps: list[Step] = []
    calls: dict[tuple[str, str], Step] = {}
    for item in items:
        if item["source"] == "user":
            content = item["content"]
            text = content if isinstance(content, str) else content.get("userText", content.get("text", ""))
            steps.append(Step(step_id=len(steps) + 1, source="user", message=text,
                              extra={"varin_history_id": item["id"]}))
            continue
        content = item.get("content", {}).get("content", {})
        kind = content.get("kind")
        if kind == "tool_result":
            result = content["result"]
            key = (result["request_id"], result["call_id"])
            if key not in calls:
                raise ValueError(f"Tool result has no matching original call: {key}")
            step = calls[key]
            if step.observation is not None:
                raise ValueError(f"Duplicate tool result: {key}")
            step.observation = Observation(results=[ObservationResult(
                source_call_id=result["call_id"],
                content=json.dumps(result["completion"], ensure_ascii=False),
            )])
        elif kind == "tool_call":
            call = content["call"]
            key = (request_identity(item), call["call_id"])
            if key in calls:
                raise ValueError(f"Duplicate tool call: {key}")
            step = Step(step_id=len(steps) + 1, source="agent", message="",
                        tool_calls=[ToolCall(tool_call_id=call["call_id"], function_name=call["name"], arguments=call["arguments"])],
                        extra={"varin_history_id": item["id"]})
            calls[key] = step
            steps.append(step)
        elif kind in {"text", "reasoning_summary", "output", "media"}:
            message = (text_parts(content["output"]) if kind == "output"
                       else content.get("text", json.dumps(content, ensure_ascii=False)))
            steps.append(Step(step_id=len(steps) + 1, source="agent", message=message,
                              extra={"varin_history_id": item["id"], "varin_kind": kind}))
    return steps


def export_trajectory(directory: Path) -> Trajectory:
    record = json.loads((directory / "run.json").read_text())
    histories = json.loads((directory / "histories.json").read_text())
    usage = json.loads((directory / "usage.json").read_text())
    totals = usage_totals(usage)
    identity = histories["root"]
    root = next(thread for thread in histories["threads"] if thread["identity"] == identity)
    revision = record["runtime"]["varin"]["sourceRevision"]
    trajectory = Trajectory(
        session_id=identity["threadId"], trajectory_id=identity["branchId"],
        agent=Agent(name="Varin", version=revision, model_name=record["model"]["modelId"]),
        steps=history_steps(root["items"]),
        final_metrics=FinalMetrics(total_prompt_tokens=totals["n_input_tokens"],
                                  total_completion_tokens=totals["n_output_tokens"],
                                  total_cached_tokens=totals["n_cache_tokens"],
                                  total_cost_usd=totals["cost_usd"],
                                  extra={"scope": "isolated trial Catalog, including child and auxiliary work"}),
        notes="Native history projection. Original histories and usage receipts accompany this file.",
    )
    # Retain child histories as separate ATIF files; raw family.json carries original relationships.
    for index, thread in enumerate(histories["threads"]):
        if thread is root or not thread["items"]:
            continue
        child = Trajectory(
            session_id=thread["identity"]["threadId"], trajectory_id=thread["identity"]["branchId"],
            agent=Agent(name=thread["name"], version=revision), steps=history_steps(thread["items"]),
        )
        (directory / f"child-trajectory-{index}.json").write_text(child.model_dump_json(indent=2), encoding="utf-8")
    return trajectory
