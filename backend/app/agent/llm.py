"""Chat models for the agent.

OpenAI stays the hosted tool-calling API. Qwen2.5-1.5B-Instruct is the local
model. Both receive the same OpenAI function specs and return the same
tool-call messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.errors import DataflowError

_DEFAULT_HUB = (
    Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct"
)
_MODEL = None
_MODEL_LOCK = threading.Lock()
_GENERATE_LOCK = threading.Lock()


def provider() -> str:
    """Server default from LLM_PROVIDER. A chat request can override it."""
    return resolve_provider(None)


def resolve_provider(choice: str | None) -> str:
    """Pick openai or qwen. An explicit choice wins over LLM_PROVIDER."""
    raw = (choice if choice is not None else os.getenv("LLM_PROVIDER","qwen") or "openai").strip().lower()
    if raw in {"qwen", "local", "huggingface"}:
        return "qwen"
    if raw in {"openai", "gpt"}:
        return "openai"
    raise DataflowError(
        "The model must be openai or qwen.",
        code="invalid_provider",
        status=400,
    )


def model_dir() -> Path:
    raw = (os.getenv("LOCAL_MODEL_PATH") or "").strip()
    path = Path(raw).expanduser() if raw else _DEFAULT_HUB
    if (path / "config.json").is_file():
        return path
    ref = path / "refs" / "main"
    if ref.is_file():
        snapshot = path / "snapshots" / ref.read_text(encoding="utf-8").strip()
        if snapshot.is_dir():
            return snapshot
    return path


def model_ready() -> bool:
    folder = model_dir()
    return (folder / "config.json").is_file() and (folder / "model.safetensors").is_file()


def chat_configuration_error(choice: str | None = None) -> DataflowError | None:
    try:
        selected = resolve_provider(choice)
    except DataflowError as exc:
        return exc
    if selected == "qwen":
        if model_ready():
            return None
        return DataflowError(
            "The local Qwen model was not found. Set LOCAL_MODEL_PATH to the snapshot that contains config.json and model.safetensors.",
            code="local_model_missing",
            status=503,
        )
    if os.getenv("OPENAI_API_KEY"):
        return None
    return DataflowError(
        "Set OPENAI_API_KEY to use the chat agent. Validation, upload, and notebook APIs still work without it.",
        code="openai_not_configured",
        status=503,
    )


def llm_status() -> dict[str, Any]:
    try:
        selected = provider()
    except DataflowError:
        selected = "invalid"
    return {"llm_provider": selected, "local_model_configured": model_ready()}


def parse_qwen_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split Qwen's <tool_call> blocks into OpenAI function-call messages."""
    visible: list[str] = []
    calls: list[dict[str, Any]] = []
    rest = text
    while True:
        start = rest.find("<tool_call>")
        if start < 0:
            visible.append(rest)
            break
        visible.append(rest[:start])
        end = rest.find("</tool_call>", start)
        if end < 0:
            break
        raw = rest[start + len("<tool_call>") : end].strip()
        call = _one_call(raw, len(calls))
        if call is not None:
            calls.append(call)
        rest = rest[end + len("</tool_call>") :]
    return "".join(visible).strip(), calls


def _one_call(raw: str, index: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("name") or "").strip()
    if not name:
        return None
    arguments = payload.get("arguments") if "arguments" in payload else {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if isinstance(arguments, dict) else {})
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }


def _messages_for_template(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                calls.append(
                    {
                        "type": "function",
                        "function": {"name": function.get("name") or "", "arguments": arguments},
                    }
                )
            converted.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            continue
        content = message.get("content")
        converted.append({"role": role, "content": "" if content is None else str(content)})
    return converted


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise DataflowError(
                "Install torch and transformers in the backend environment to run the local Qwen model.",
                code="missing_dependency",
                status=500,
            ) from exc
        folder = str(model_dir())
        if not model_ready():
            raise DataflowError(
                f"The local Qwen model was not found at {folder}.",
                code="local_model_missing",
                status=503,
            )
        device = "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if device == "mps" else torch.float32
        tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
        try:
            model = AutoModelForCausalLM.from_pretrained(folder, local_files_only=True, dtype=dtype)
        except TypeError:
            model = AutoModelForCausalLM.from_pretrained(folder, local_files_only=True, torch_dtype=dtype)
        model.to(device)
        model.eval()
        _MODEL = (tokenizer, model, device)
        return _MODEL


async def stream_model(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_token: Callable[[str], Awaitable[None]],
    choice: str | None = None,
) -> list[dict[str, Any]]:
    selected = resolve_provider(choice)
    if selected == "qwen":
        return await _stream_qwen(messages, tools, on_token)
    return await _stream_openai(messages, tools, on_token)


async def _stream_openai(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_token: Callable[[str], Awaitable[None]],
) -> list[dict[str, Any]]:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover
        raise DataflowError("The openai package is not installed.", code="missing_dependency", status=500) from exc

    client = AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1")
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            temperature=0.2,
            stream=True,
        )
    except Exception as exc:
        raise DataflowError(f"OpenAI request failed: {exc}", code="openai_error", status=502) from exc

    tool_calls: dict[int, dict[str, str]] = {}
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            await on_token(delta.content)
        for call in delta.tool_calls or []:
            bucket = tool_calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
            if call.id:
                bucket["id"] = call.id
            if call.function and call.function.name:
                bucket["name"] = call.function.name
            if call.function and call.function.arguments:
                bucket["arguments"] += call.function.arguments
    return [
        {
            "id": tool_calls[index]["id"] or f"call_{index}",
            "type": "function",
            "function": {
                "name": tool_calls[index]["name"],
                "arguments": tool_calls[index]["arguments"] or "{}",
            },
        }
        for index in sorted(tool_calls)
    ]


async def _stream_qwen(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_token: Callable[[str], Awaitable[None]],
) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def produce() -> None:
        try:
            for piece in _generate_qwen(messages, tools):
                loop.call_soon_threadsafe(queue.put_nowait, {"token": piece})
        except DataflowError as exc:
            loop.call_soon_threadsafe(queue.put_nowait, {"error": exc})
        except Exception as exc:  # pragma: no cover
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"error": DataflowError(str(exc), code="local_model_error", status=502)},
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=produce, name="qwen-generate", daemon=True).start()
    raw_parts: list[str] = []
    visible_buffer = ""
    hiding_tool = False
    while True:
        item = await queue.get()
        if item is None:
            break
        if "error" in item:
            raise item["error"]
        piece = str(item.get("token") or "")
        raw_parts.append(piece)
        if hiding_tool:
            continue
        visible_buffer += piece
        marker = visible_buffer.find("<tool_call>")
        if marker >= 0:
            if marker:
                await on_token(visible_buffer[:marker])
            hiding_tool = True
            visible_buffer = ""
            continue
        keep = len("<tool_call>") - 1
        if len(visible_buffer) > keep:
            await on_token(visible_buffer[:-keep])
            visible_buffer = visible_buffer[-keep:]
    if visible_buffer and not hiding_tool:
        await on_token(visible_buffer)
    _, calls = parse_qwen_tool_calls("".join(raw_parts))
    return calls


def _generate_qwen(messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    import torch

    tokenizer, model, _device = _load_model()
    prompt = tokenizer.apply_chat_template(
        _messages_for_template(messages),
        tools=tools,
        add_generation_prompt=True,
        tokenize=False,
    )
    inputs = tokenizer(prompt, return_tensors="pt")
    inputs = {key: value.to(model.device) for key, value in inputs.items()}
    from transformers import TextIteratorStreamer

    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    max_tokens = int(os.getenv("LOCAL_MODEL_MAX_TOKENS", "768") or "768")

    def run() -> None:
        try:
            with _GENERATE_LOCK, torch.inference_mode():
                model.generate(
                    **inputs,
                    max_new_tokens=max(32, min(max_tokens, 2048)),
                    do_sample=False,
                    pad_token_id=tokenizer.pad_token_id,
                    streamer=streamer,
                )
        finally:
            streamer.end()

    worker = threading.Thread(target=run, name="qwen-model", daemon=True)
    worker.start()
    yield from streamer
    worker.join()
