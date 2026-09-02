"""所有 SessionStore 实现复用的行为契约；不访问 SQLite 或私有驱动。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import Budget, EventDraft, ThreadCreated, TurnStarted
from harnessix.agent.reducer import replay
from harnessix.session.ports import SessionStore

StoreFactory = Callable[[str], SessionStore]


class SessionStoreContract:
    async def test_identity_idempotency_and_cursor(self, store_factory: StoreFactory) -> None:
        store = store_factory("shared")
        await store.initialize()
        thread_id = new_id()
        draft = EventDraft(payload=ThreadCreated(workspace="/workspace"))
        thread = await store.append(thread_id, [draft], expected_sequence=0)
        assert await store.append(thread_id, [draft], expected_sequence=0) == thread
        assert await store.thread_ids() == [thread_id]
        assert await store.events(thread_id, after=thread.sequence) == []
        assert replay(await store.events(thread_id)) == thread
        assert await store.rebuild(thread_id) == thread
        with pytest.raises(KernelError, match="不同载荷"):
            await store.append(
                thread_id,
                [
                    draft.model_copy(
                        update={
                            "payload": ThreadCreated(workspace="/changed"),
                        }
                    )
                ],
                expected_sequence=0,
            )
        assert await store.get_thread(thread_id) == thread
        with pytest.raises(KernelError):
            await store.events(thread_id, after=-1)

    async def test_cas_across_connections(self, store_factory: StoreFactory) -> None:
        first, second = store_factory("shared"), store_factory("shared")
        await first.initialize()
        thread_id = new_id()
        await first.append(
            thread_id,
            [EventDraft(payload=ThreadCreated(workspace="/workspace"))],
            expected_sequence=0,
        )
        results = await asyncio.gather(
            *(
                store.append(
                    thread_id,
                    [
                        EventDraft(
                            turn_id=new_id(),
                            payload=TurnStarted(
                                request_id=f"r{index}",
                                request_fingerprint="0" * 64,
                                budget=Budget(),
                            ),
                        )
                    ],
                    expected_sequence=1,
                )
                for index, store in enumerate((first, second))
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, KernelError)]
        assert len(failures) == 1 and failures[0].code == "sequence_conflict"
        assert (await first.get_thread(thread_id)).sequence == 2
        assert await first.get_thread(thread_id) == await second.get_thread(thread_id)

    async def test_batch_atomicity_and_cross_thread_event_id(
        self, store_factory: StoreFactory
    ) -> None:
        store = store_factory("shared")
        await store.initialize()
        thread_id = new_id()
        first = EventDraft(payload=ThreadCreated(workspace="/workspace"))
        snapshot = await store.append(thread_id, [first], expected_sequence=0)
        second = EventDraft(
            turn_id=new_id(),
            payload=TurnStarted(
                request_id="r",
                request_fingerprint="0" * 64,
                budget=Budget(),
            ),
        )
        for drafts, expected in [([first, second], 0), ([second, second], 1), ([], 1)]:
            with pytest.raises(KernelError):
                await store.append(thread_id, drafts, expected_sequence=expected)
            assert await store.get_thread(thread_id) == snapshot
        with pytest.raises(KernelError):
            await store.append(new_id(), [first], expected_sequence=0)
        # 第一条合法，第二条违反重复请求约束，整个批次回滚。
        with pytest.raises(KernelError):
            await store.append(
                thread_id,
                [
                    second,
                    EventDraft(turn_id=new_id(), payload=second.payload),
                ],
                expected_sequence=1,
            )
        assert await store.get_thread(thread_id) == snapshot
        assert len(await store.events(thread_id)) == 1

    async def test_thread_isolation_and_snapshot_value_semantics(
        self, store_factory: StoreFactory
    ) -> None:
        store = store_factory("shared")
        await store.initialize()
        first, second = new_id(), new_id()
        a = await store.append(
            first, [EventDraft(payload=ThreadCreated(workspace="/a"))], expected_sequence=0
        )
        b = await store.append(
            second, [EventDraft(payload=ThreadCreated(workspace="/b"))], expected_sequence=0
        )
        assert set(await store.thread_ids()) == {first, second}
        await store.append(
            first,
            [
                EventDraft(
                    turn_id=new_id(),
                    payload=TurnStarted(
                        request_id="r",
                        request_fingerprint="0" * 64,
                        budget=Budget(),
                    ),
                )
            ],
            expected_sequence=1,
        )
        assert a.sequence == 1
        assert await store.get_thread(second) == b
        assert all(event.thread_id == first for event in await store.events(first))
        with pytest.raises(KernelError):
            await store.get_thread(new_id())
