"""LangGraph control plane for the data agent.

The model still streams through the OpenAI SDK. The graph only decides what
happens around that call: run a tool, pause for an operator, or finish.
A LangChain agent executor is intentionally not used.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from langchain_core.runnables import RunnableConfig
from typing_extensions import TypedDict

from app.agent.prompts import system_prompt
from app.agent.tools import LABELS, TOOL_SPECS, bind_user_locations, execute_tool, tool_result_json
from app.errors import DataflowError
from app.schemas import ChatRequest

MAX_ROUNDS = 6


class GraphState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    rounds: int
    pending_confirmation: dict[str, Any] | None
    approved: bool
    stop: bool

# Live SSE queues are not checkpointed. They live only for the in-flight turn.
_EMITTERS: dict[str, asyncio.Queue] = {}
_GRAPH = None


def route_after_agent(state: GraphState) -> str:
    if state.get("stop"):
        return "end"
    return "tools"


def route_after_tools(state: GraphState) -> str:
    if state.get("pending_confirmation"):
        return "approve"
    if state.get("stop"):
        return "end"
    return "agent"


def route_after_approve(state: GraphState) -> str:
    if state.get("approved"):
        return "agent"
    return "end"


def _emitter(config: RunnableConfig) -> asyncio.Queue | None:
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        return None
    return _EMITTERS.get(thread_id)


async def _emit(config: RunnableConfig, event: dict[str, Any]) -> None:
    queue = _emitter(config)
    if queue is not None:
        await queue.put(event)


async def agent_node(state: GraphState, config: RunnableConfig) -> GraphState:
    rounds = int(state.get("rounds") or 0)
    if rounds >= MAX_ROUNDS:
        await _emit(
            config,
            {
                "type": "error",
                "message": "The agent stopped after several tool rounds. Ask it to continue from the last result.",
            },
        )
        return {"stop": True, "rounds": rounds, "pending_confirmation": None}

    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover
        raise DataflowError("The openai package is not installed.", code="missing_dependency", status=500) from exc

    client = AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1")
    messages = list(state.get("messages") or [])
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SPECS,
            temperature=0.2,
            stream=True,
        )
    except Exception as exc:
        raise DataflowError(f"OpenAI request failed: {exc}", code="openai_error", status=502) from exc

    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            text_parts.append(delta.content)
            await _emit(config, {"type": "token", "content": delta.content})
        for call in delta.tool_calls or []:
            bucket = tool_calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
            if call.id:
                bucket["id"] = call.id
            if call.function and call.function.name:
                bucket["name"] = call.function.name
            if call.function and call.function.arguments:
                bucket["arguments"] += call.function.arguments

    if not tool_calls:
        return {"stop": True, "rounds": rounds + 1, "pending_confirmation": None}

    assistant_calls = []
    for index in sorted(tool_calls):
        bucket = tool_calls[index]
        assistant_calls.append(
            {
                "id": bucket["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": bucket["name"], "arguments": bucket["arguments"] or "{}"},
            }
        )
    messages.append(
        {
            "role": "assistant",
            "content": "".join(text_parts) or None,
            "tool_calls": assistant_calls,
        }
    )
    return {
        "messages": messages,
        "rounds": rounds + 1,
        "stop": False,
        "pending_confirmation": None,
    }


async def tools_node(state: GraphState, config: RunnableConfig) -> GraphState:
    messages = list(state.get("messages") or [])
    assistant = messages[-1] if messages else {}
    pending: dict[str, Any] | None = None
    for call in assistant.get("tool_calls") or []:
        name = call["function"]["name"]
        await _emit(config, {"type": "tool_start", "name": name, "label": LABELS.get(name, name)})
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        arguments = bind_user_locations(name, arguments, messages)
        outcome = await asyncio.to_thread(execute_tool, name, arguments)
        for artifact in outcome.get("artifacts") or []:
            await _emit(config, {"type": "artifact", **artifact})
            if artifact.get("kind") == "confirmation" and pending is None:
                pending = {
                    "destination": artifact.get("destination") or "",
                    "source": artifact.get("source") or "",
                    "message": artifact.get("message") or "Create this destination?",
                }
        result = outcome.get("result") or {}
        if result.get("confirmation_required") and pending is None:
            pending = {
                "destination": result.get("destination") or "",
                "source": result.get("source") or "",
                "message": result.get("message") or "Create this destination?",
            }
        await _emit(config, {"type": "tool_end", "name": name, "ok": "error" not in result})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": tool_result_json(result)[:14000],
            }
        )
    return {"messages": messages, "pending_confirmation": pending, "stop": False}


async def approve_node(state: GraphState, config: RunnableConfig) -> GraphState:
    """Pause until an operator allows or refuses a new Unity Catalog object.

    LangGraph restarts this node on resume. The interrupt is the first call,
    so the confirmation event is emitted by the tools node, not here.
    """
    from langgraph.types import interrupt

    pending = state.get("pending_confirmation") or {}
    if not pending:
        return {"approved": False, "stop": True}
    decision = interrupt(pending)
    approved = bool(isinstance(decision, dict) and decision.get("approved"))
    destination = str(pending.get("destination") or decision.get("destination") or "")
    source = str(pending.get("source") or "")
    if not approved:
        await _emit(
            config,
            {
                "type": "token",
                "content": f"Left {destination} unchanged. Nothing was created.",
            },
        )
        return {"pending_confirmation": None, "approved": False, "stop": True}
    if not source:
        await _emit(
            config,
            {
                "type": "error",
                "message": f"Cannot create {destination} because the source path was not recorded.",
            },
        )
        return {"pending_confirmation": None, "approved": False, "stop": True}
    from app.databricks.destination import create_missing_destination

    try:
        created = await asyncio.to_thread(create_missing_destination, source, destination)
    except DataflowError as exc:
        await _emit(config, {"type": "error", "message": exc.message, "code": exc.code})
        return {"pending_confirmation": None, "approved": False, "stop": True}
    verb = "Created" if created.get("created") else "Found"
    await _emit(config, {"type": "token", "content": f"{verb} {created.get('name') or destination} in Unity Catalog. "})
    messages = list(state.get("messages") or [])
    messages.append(
        {
            "role": "user",
            "content": (
                f"The operator approved creating {destination}. The object now exists. "
                "Call generate_pipeline with confirm_create_destination true. "
                "That copies the source rows and runs validation. Do not ask for the path again."
            ),
        }
    )
    return {"messages": messages, "pending_confirmation": None, "approved": True, "stop": False}


def build_graph():
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(GraphState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("approve", approve_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    graph.add_conditional_edges("tools", route_after_tools, {"approve": "approve", "agent": "agent", "end": END})
    graph.add_conditional_edges("approve", route_after_approve, {"agent": "agent", "end": END})
    return graph.compile(checkpointer=MemorySaver())


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def initial_state(request: ChatRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(request.context)}]
    for message in request.messages:
        messages.append({"role": message.role, "content": message.content})
    return {
        "messages": messages,
        "rounds": 0,
        "pending_confirmation": None,
        "approved": False,
        "stop": False,
    }


def bind_emitter(thread_id: str, queue: asyncio.Queue) -> None:
    _EMITTERS[thread_id] = queue


def release_emitter(thread_id: str) -> None:
    _EMITTERS.pop(thread_id, None)
