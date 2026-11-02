"""Stream the LangGraph data agent as server-sent events."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.agent.graph import bind_emitter, get_graph, initial_state, release_emitter
from app.agent.llm import chat_configuration_error
from app.agent.session_memory import close_thread
from app.errors import DataflowError
from app.schemas import ChatRequest


async def stream_agent(request: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    configuration_error = chat_configuration_error(request.provider)
    if configuration_error is not None:
        raise configuration_error
    from langgraph.types import Command

    queue: asyncio.Queue = asyncio.Queue()
    if request.resume:
        if not request.thread_id:
            raise DataflowError(
                "Approving a destination needs the paused agent thread. Ask again from the approval.",
                code="missing_thread",
                status=400,
            )
        thread_id = request.thread_id
    else:
        thread_id = str(uuid.uuid4())
    bind_emitter(thread_id, queue)
    graph = get_graph()
    config = {
        "configurable": {"thread_id": thread_id, "llm_provider": request.provider},
        "recursion_limit": 40,
    }

    async def drive() -> None:
        holding = False
        try:
            if request.resume:
                snapshot = await graph.aget_state(config)
                if snapshot.next:
                    await graph.ainvoke(
                        Command(
                            resume={
                                "approved": request.resume.approved,
                                "destination": request.resume.destination,
                                "comment": request.resume.comment,
                                "action": request.resume.action,
                            }
                        ),
                        config,
                    )
                else:
                    await graph.ainvoke(initial_state(request), config)
            else:
                await graph.ainvoke(initial_state(request), config)
            snapshot = await graph.aget_state(config)
            if snapshot.next:
                holding = True
                await queue.put({"type": "hold", "thread_id": thread_id})
            await queue.put({"type": "done"})
        except DataflowError as exc:
            await queue.put({"type": "error", "message": exc.message, "code": exc.code})
            await queue.put({"type": "done"})
        except Exception as exc:  # pragma: no cover
            await queue.put({"type": "error", "message": str(exc), "code": "chat_failed"})
            await queue.put({"type": "done"})
        finally:
            if not holding:
                close_thread(thread_id)
            release_emitter(thread_id)

    task = asyncio.create_task(drive())
    try:
        while True:
            event = await queue.get()
            yield event
            if event.get("type") == "done":
                break
    finally:
        await task
