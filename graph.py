"""LangGraph wiring for the Document Intake Agent.

    START -> ingest -> prepare -> classify -> extract -> validate -> cross_check --ok/exhausted--> emit -> END
                                                ^                                |
                                                +------------ retry ------------+  (INTAKE_MAX_RETRIES, error messages fed back)
"""
from __future__ import annotations

from extractors import Extractor
from nodes import IntakeState, make_nodes, route_after_validate

try:
    from langgraph.graph import END, START, StateGraph

    HAVE_LANGGRAPH = True
except ImportError:  # dev fallback so nodes/tests can run where langgraph isn't installed
    HAVE_LANGGRAPH = False


def build_graph(extractor: Extractor):
    n = make_nodes(extractor)
    g = StateGraph(IntakeState)
    for name, fn in n.items():
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "prepare")
    g.add_edge("prepare", "classify")
    g.add_edge("classify", "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", "cross_check")
    g.add_conditional_edges("cross_check", route_after_validate, {"retry": "extract", "emit": "emit"})
    g.add_edge("emit", END)
    return g.compile()  # add checkpointer=... here for resume / human-in-the-loop


def _run_sequential(extractor: Extractor, state: IntakeState) -> IntakeState:
    """Same nodes, same router, no langgraph. Dev/test fallback only."""
    n = make_nodes(extractor)
    for step in ("ingest", "prepare", "classify"):
        state = {**state, **n[step](state)}
    while True:
        state = {**state, **n["extract"](state)}
        state = {**state, **n["validate"](state)}
        state = {**state, **n["cross_check"](state)}
        if route_after_validate(state) == "emit":
            break
    return {**state, **n["emit"](state)}


def run_intake(extractor: Extractor, file_paths: list[str], user_text: str = "") -> dict:
    """Entry point used by the CLI demo now and by the A2A server later."""
    state: IntakeState = {"file_paths": file_paths, "user_text": user_text}
    final = build_graph(extractor).invoke(state) if HAVE_LANGGRAPH else _run_sequential(extractor, state)
    return final["result"]
