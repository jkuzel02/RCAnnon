"""Unit tests for the RCA workflow variant registry."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rca.workflows import (
    build_workflow,
    list_variants,
    register_variant,
    unregister_variant,
)


def test_default_variants_are_registered() -> None:
    variants = list_variants()
    for expected in ("base", "optimal", "mas"):
        assert expected in variants

    base_graph = build_workflow("base", complete=Mock(return_value="ok"))
    assert base_graph.invoke({"context": {"incident": "test"}})["root-cause"] == "ok"

    valid_rca = (
        '{"cause":"c","causal_chain":"cc","supporting_evidence":["e"],'
        '"remediation":"r","recovery_check":"rc"}'
    )
    optimal_graph = build_workflow("optimal", complete=Mock(return_value=valid_rca))
    assert optimal_graph.invoke({"context": {"incident": "test"}})["root-cause"]["cause"] == "c"

    with pytest.raises(NotImplementedError):
        build_workflow("mas", complete=Mock(return_value="ok"))


def test_unknown_variant_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown workflow variant"):
        build_workflow("nonexistent-variant", complete=Mock(return_value="ok"))


def test_non_callable_complete_is_rejected() -> None:
    with pytest.raises(TypeError, match="callable"):
        build_workflow("base", complete="not-a-callable")  # type: ignore[arg-type]


def test_register_and_build_custom_variant() -> None:
    name = "test-custom-variant"
    try:

        def builder(*, complete):
            class _Graph:
                def invoke(self, state):
                    return {"root-cause": complete("base.md", state)}

            return _Graph()

        register_variant(name, builder)
        assert name in list_variants()

        with pytest.raises(ValueError, match="already registered"):
            register_variant(name, builder)

        register_variant(name, builder, replace=True)

        complete = Mock(return_value="custom diagnosis")
        graph = build_workflow(name, complete=complete)
        result = graph.invoke({"context": {"incident": "crash"}})
        assert result == {"root-cause": "custom diagnosis"}
        complete.assert_called_once_with("base.md", {"context": {"incident": "crash"}})
    finally:
        unregister_variant(name)
