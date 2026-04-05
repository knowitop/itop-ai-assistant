import logging

from langgraph.graph import END, StateGraph

from .context import GraphContext
from .nodes import ask, enrich, evaluate, guard
from .state import Action, EnrichmentState

logger = logging.getLogger(__name__)


def build_graph():
    g = StateGraph(EnrichmentState, context_schema=GraphContext)

    g.add_node("guard", guard.run)
    g.add_node("evaluate", evaluate.run)
    g.add_node("ask", ask.run)
    g.add_node("enrich", enrich.run)

    g.set_entry_point("guard")

    g.add_conditional_edges(
        "guard",
        lambda s: s["action"],
        {
            Action.STOP: END,
            Action.ASK: "evaluate",  # guard не остановил — идём дальше
            None: "evaluate",
        },
    )

    # g.add_node("evaluate", evaluate.run)
    g.add_conditional_edges(
        "evaluate",
        lambda s: s["action"],
        {
            Action.ASK: "ask",
            Action.ENRICH: "enrich",
            Action.STOP: END,
        },
    )

    g.add_edge("ask", END)
    g.add_edge("enrich", END)

    return g.compile()


graph = build_graph()
