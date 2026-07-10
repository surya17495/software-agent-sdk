"""Regression tests for ``Config.max_concurrent_runs`` admission control.

Bug class this catches (#4063):
    ``max_concurrent_runs`` only sized the dedicated ``ThreadPoolExecutor``
    used by the synchronous ``conversation.run()`` fallback. The native async
    ``conversation.arun()`` path bypassed it, so a burst of conversations could
    all execute agent steps concurrently regardless of the configured limit,
    exhausting memory.

The fix introduces a shared ``asyncio.BoundedSemaphore(max_concurrent_runs)``
on ``ConversationService`` that guards *both* paths inside
``EventService._run_and_publish``. These tests drive ``EventService.run()``
directly with blocking stand-in conversations instrumented with a shared
active-counter, and assert the maximum observed active count never exceeds the
configured limit for:

  * the native async ``arun()`` path (the gap the bug opened), and
  * the synchronous ``run()`` thread-pool path.

A third test verifies permits are released on cancellation (pause/interrupt),
so a cancelled run does not permanently shrink the admission budget.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from openhands.agent_server.event_service import EventService
from openhands.agent_server.models import StoredConversation
from openhands.sdk import Agent
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.llm import LLM
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.workspace import LocalWorkspace


def _stored() -> StoredConversation:
    return StoredConversation(
        id=uuid4(),
        agent=Agent(llm=LLM(model="gpt-4o", usage_id="test-llm"), tools=[]),
        workspace=LocalWorkspace(working_dir="workspace/project"),
        confirmation_policy=NeverConfirm(),
        initial_message=None,
        metrics=None,
        created_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


def _new_event_service(
    *, executor: ThreadPoolExecutor, semaphore: asyncio.BoundedSemaphore
) -> EventService:
    """Standalone EventService wired with the shared executor + semaphore,
    mirroring what ConversationService._start_event_service does."""
    es = EventService(stored=_stored(), conversations_dir=Path("unused"))
    es._run_executor = executor
    es._run_semaphore = semaphore
    return es


def _patch_run_essentials(event_service: EventService):
    """Patch the helpers that touch real conversation state so the stand-in
    conversation only exercises the dispatch + admission logic."""
    return [
        patch.object(
            type(event_service),
            "_get_execution_status",
            new_callable=AsyncMock,
            return_value=ConversationExecutionStatus.IDLE,
        ),
        patch.object(
            type(event_service),
            "_publish_state_update",
            new_callable=AsyncMock,
        ),
    ]


@pytest.mark.asyncio
async def test_max_concurrent_runs_limits_native_async_path():
    """The native async ``arun()`` path must be bounded by the semaphore.

    With max_concurrent_runs=2 and 5 conversations, at most 2 ``arun()``
    coroutines may be actively executing at once. Before the fix the semaphore
    did not guard this path, so all 5 could run concurrently.
    """
    limit = 2
    n = 5
    executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="test-run")
    semaphore = asyncio.BoundedSemaphore(limit)

    state = {"active": 0, "max_active": 0}
    counter = asyncio.Lock()
    block = asyncio.Event()

    class _NativeAsyncAgent:
        async def astep(self, conversation, on_event, on_token=None):  # noqa: ARG002
            pass

    class _NativeAsyncConv:
        def __init__(self, agent):
            self.agent = agent

        async def arun(self):
            async with counter:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            await block.wait()
            async with counter:
                state["active"] -= 1

    agent = _NativeAsyncAgent()
    services: list[EventService] = []
    patches: list = []
    try:
        for _ in range(n):
            es = _new_event_service(executor=executor, semaphore=semaphore)
            es._conversation = _NativeAsyncConv(agent)  # type: ignore[assignment]
            for p in _patch_run_essentials(es):
                p.start()
                patches.append(p)
            services.append(es)

        for es in services:
            await es.run()

        # Wait until the limit is reached; queued tasks block on the semaphore.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while state["max_active"] < limit and loop.time() < deadline:
            await asyncio.sleep(0.005)

        assert state["max_active"] == limit, (
            f"expected {limit} concurrent native-async runs, "
            f"observed {state['max_active']}"
        )

        # Release the blocked runs; queued ones then acquire permits and finish.
        block.set()
        await asyncio.gather(*[es._run_task for es in services if es._run_task])

        assert state["max_active"] <= limit, (
            f"native-async concurrency exceeded the limit: "
            f"{state['max_active']} > {limit}"
        )
    finally:
        for p in patches:
            p.stop()
        executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_max_concurrent_runs_limits_sync_path():
    """The synchronous ``run()`` thread-pool path must also be bounded by the
    semaphore, not just by the executor size.

    The executor is deliberately oversized (10 workers) while the semaphore
    limit is 2, proving the semaphore is the binding admission control for the
    sync path too.
    """
    limit = 2
    n = 5
    executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="test-run")
    semaphore = asyncio.BoundedSemaphore(limit)

    state = {"active": 0, "max_active": 0}
    counter = threading.Lock()
    block = threading.Event()

    # A plain agent (no ``astep`` override) keeps ``has_native_arun`` False;
    # combined with the conversation not overriding ``arun``, ``run()`` is
    # routed to the thread-pool executor.
    class _SyncAgent:
        pass

    class _SyncConv:
        def __init__(self, agent):
            self.agent = agent

        def run(self):
            with counter:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            block.wait(timeout=10.0)
            with counter:
                state["active"] -= 1

    agent = _SyncAgent()
    services: list[EventService] = []
    patches: list = []
    try:
        for _ in range(n):
            es = _new_event_service(executor=executor, semaphore=semaphore)
            es._conversation = _SyncConv(agent)  # type: ignore[assignment]
            for p in _patch_run_essentials(es):
                p.start()
                patches.append(p)
            services.append(es)

        for es in services:
            await es.run()

        # Wait until the limit is reached; queued tasks block on the semaphore.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while True:
            with counter:
                cur = state["max_active"]
            if cur >= limit or loop.time() >= deadline:
                break
            await asyncio.sleep(0.005)

        assert state["max_active"] == limit, (
            f"expected {limit} concurrent sync runs, observed {state['max_active']}"
        )

        block.set()
        await asyncio.gather(*[es._run_task for es in services if es._run_task])

        assert state["max_active"] <= limit, (
            f"sync concurrency exceeded the limit: {state['max_active']} > {limit}"
        )
    finally:
        for p in patches:
            p.stop()
        executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_permit_released_on_cancellation():
    """A run cancelled (pause/interrupt/close) mid-execution must release its
    permit so the admission budget is not permanently shrunk."""
    limit = 1
    executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="test-run")
    semaphore = asyncio.BoundedSemaphore(limit)

    block = asyncio.Event()
    # Tracks whether the second conversation's arun() body has started.
    es2_started = asyncio.Event()

    class _NativeAsyncAgent:
        async def astep(self, conversation, on_event, on_token=None):  # noqa: ARG002
            pass

    class _NativeAsyncConv:
        def __init__(self, agent, *, on_enter=None):
            self.agent = agent
            self._on_enter = on_enter

        async def arun(self):
            if self._on_enter is not None:
                self._on_enter()
            await block.wait()

    agent = _NativeAsyncAgent()
    es1 = _new_event_service(executor=executor, semaphore=semaphore)
    es2 = _new_event_service(executor=executor, semaphore=semaphore)
    es1._conversation = _NativeAsyncConv(agent)  # type: ignore[assignment]
    es2._conversation = _NativeAsyncConv(  # type: ignore[assignment]
        agent, on_enter=es2_started.set
    )

    patches: list = []
    for es in (es1, es2):
        for p in _patch_run_essentials(es):
            p.start()
            patches.append(p)

    try:
        # First conversation grabs the only permit and blocks.
        await es1.run()
        await asyncio.sleep(0.05)
        assert es1._run_task is not None and not es1._run_task.done()

        # Second conversation is queued behind the semaphore; its arun() body
        # must NOT have started yet because the only permit is held by es1.
        await es2.run()
        await asyncio.sleep(0.05)
        assert es2._run_task is not None and not es2._run_task.done()
        assert not es2_started.is_set(), (
            "es2 started executing before a permit was available — the "
            "semaphore is not queuing native-async runs"
        )

        # Cancel the first run (simulates interrupt()/close() cancelling the
        # _run_task). The CancelledError unwinds ``async with``, releasing the
        # permit.
        es1._run_task.cancel()
        with suppress(asyncio.CancelledError):
            await es1._run_task

        # es2 should now acquire the freed permit and proceed to block on the
        # event; release it so the run can finish.
        block.set()
        await asyncio.gather(es2._run_task)

        assert semaphore._value == limit, (
            f"permit was not released after cancellation; semaphore value "
            f"{semaphore._value} != {limit}"
        )
    finally:
        for p in patches:
            p.stop()
        executor.shutdown(wait=False)
