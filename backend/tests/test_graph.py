import asyncio

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.graph import (
    GraphState,
    approve_node,
    bind_emitter,
    get_graph,
    release_emitter,
    route_after_agent,
    route_after_approve,
    route_after_tools,
)


def test_graph_routes_confirmation_to_an_operator_pause():
    assert route_after_agent({"stop": True}) == "end"
    assert route_after_agent({"stop": False}) == "tools"
    assert route_after_tools({"pending_confirmation": {"destination": "main.silver.new"}, "stop": False}) == "approve"
    assert route_after_tools({"pending_confirmation": None, "stop": False}) == "agent"
    assert route_after_approve({"approved": True}) == "agent"
    assert route_after_approve({"approved": False}) == "end"


def test_compiled_graph_has_the_approval_step():
    nodes = set(get_graph().get_graph().nodes)
    assert {"agent", "tools", "approve"} <= nodes


def test_approval_creates_the_destination_when_the_operator_says_yes(monkeypatch):
    created = {}

    def fake_create(source: str, target: str) -> dict:
        created["pair"] = (source, target)
        return {"created": True, "exists": True, "name": target, "kind": "table"}

    monkeypatch.setattr("app.databricks.destination.create_missing_destination", fake_create)
    asyncio.run(_approve_yes())
    assert created["pair"] == ("main.bronze.orders", "main.silver.orders_new")


def _approve_yes():
    graph = StateGraph(GraphState)
    graph.add_node("approve", approve_node)
    graph.add_edge(START, "approve")
    graph.add_conditional_edges("approve", route_after_approve, {"agent": END, "end": END})
    compiled = graph.compile(checkpointer=MemorySaver())

    async def run():
        queue = asyncio.Queue()
        thread_id = "approval-create"
        bind_emitter(thread_id, queue)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            await compiled.ainvoke(
                {
                    "messages": [],
                    "pending_confirmation": {
                        "destination": "main.silver.orders_new",
                        "source": "main.bronze.orders",
                        "message": "Create it?",
                    },
                },
                config,
            )
            await compiled.ainvoke(
                Command(resume={"approved": True, "destination": "main.silver.orders_new"}),
                config,
            )
            event = await queue.get()
            assert event["type"] == "token"
            assert "Created main.silver.orders_new" in event["content"]
            finished = await compiled.aget_state(config)
            assert finished.values.get("approved") is True
        finally:
            release_emitter(thread_id)

    return run()


def test_approval_pauses_and_resumes_without_creating():
    asyncio.run(_pause_and_resume())


def _pause_and_resume():
    graph = StateGraph(GraphState)
    graph.add_node("approve", approve_node)
    graph.add_edge(START, "approve")
    graph.add_conditional_edges("approve", route_after_approve, {"agent": END, "end": END})
    compiled = graph.compile(checkpointer=MemorySaver())

    async def run():
        queue = asyncio.Queue()
        thread_id = "approval-test"
        bind_emitter(thread_id, queue)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            await compiled.ainvoke(
                {
                    "messages": [],
                    "pending_confirmation": {
                        "destination": "main.silver.new_orders",
                        "message": "Create it?",
                    },
                },
                config,
            )
            snapshot = await compiled.aget_state(config)
            assert snapshot.next == ("approve",)
            assert queue.empty()

            await compiled.ainvoke(Command(resume={"approved": False, "destination": "main.silver.new_orders"}), config)
            event = await queue.get()
            assert event["type"] == "token"
            assert "unchanged" in event["content"]
            assert "main.silver.new_orders" in event["content"]
            finished = await compiled.aget_state(config)
            assert finished.next == ()
            assert finished.values.get("approved") is False
        finally:
            release_emitter(thread_id)

    return run()
