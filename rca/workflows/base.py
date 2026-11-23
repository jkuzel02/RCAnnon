"""One-call baseline LangGraph RCA workflow using prompts/base.md."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rca.contracts.provider import CompleteFn

BASE_PROMPT_PATH = "base.md"

BaseWorkflowState = TypedDict(
    "BaseWorkflowState",
    {
        "context": dict[str, Any],
        "root-cause": str,
    },
    total=False,
)


def build_base_workflow(*, complete: CompleteFn) -> Any:
    """Build a minimal single-node compiled LangGraph for the baseline comparator."""
    if not callable(complete):
        raise TypeError("complete must be a callable accepting (prompt_path, payload) -> str.")

    def diagnose(state: BaseWorkflowState) -> BaseWorkflowState:
        response = complete(BASE_PROMPT_PATH, {"context": state["context"]})
        return {"root-cause": response}

    graph = StateGraph(BaseWorkflowState)
    graph.add_node("diagnose", diagnose)
    graph.add_edge(START, "diagnose")
    graph.add_edge("diagnose", END)
    return graph.compile()
