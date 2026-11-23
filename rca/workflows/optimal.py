"""One-call optimized LangGraph RCA workflow using prompts/optimal.md."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rca.contracts.provider import CompleteFn
from rca.contracts.schemas import RCA, parse_and_validate_json

OPTIMAL_PROMPT_PATH = "optimal.md"

OptimalWorkflowState = TypedDict(
    "OptimalWorkflowState",
    {
        "context": dict[str, Any],
        "root-cause": dict[str, Any],
    },
    total=False,
)


def build_optimal_workflow(*, complete: CompleteFn) -> Any:
    """Build a single-call compiled LangGraph with strict RCA JSON Schema validation."""
    if not callable(complete):
        raise TypeError("complete must be a callable accepting (prompt_path, payload) -> str.")

    def diagnose(state: OptimalWorkflowState) -> OptimalWorkflowState:
        raw = complete(OPTIMAL_PROMPT_PATH, {"context": state["context"]})
        validated = parse_and_validate_json(raw, RCA, label="optimal RCA")
        return {"root-cause": validated}

    graph = StateGraph(OptimalWorkflowState)
    graph.add_node("diagnose", diagnose)
    graph.add_edge(START, "diagnose")
    graph.add_edge("diagnose", END)
    return graph.compile()
