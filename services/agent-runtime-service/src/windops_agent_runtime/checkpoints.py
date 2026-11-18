"""LangGraph's checkpoints, kept in the runtime's PostgreSQL under the run's lease (M8.4).

One run is one LangGraph thread. Each checkpoint, and each task's writes towards the next, is a
row that never changes, in a tenant-owned table. Every write names the claim's lease token, in the
same transaction, so a worker whose run another replica has taken cannot write a checkpoint: its
write raises LeaseLostError, and it stops. A run claimed again starts from its newest checkpoint,
and LangGraph skips the nodes that checkpoint has already passed.

Only the async methods are implemented: the runtime never runs a graph synchronously.
"""

import random
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)
from sqlalchemy import insert, select

from windops_agent_runtime.queue import Claim, Queue
from windops_agent_runtime.store import RUN_CHECKPOINT_WRITES, RUN_CHECKPOINTS


class RunCheckpointer(BaseCheckpointSaver[str]):
    def __init__(self, queue: Queue, claim: Claim) -> None:
        super().__init__()
        self._queue, self._claim = queue, claim

    def _thread(self, config: RunnableConfig) -> tuple[str, str]:
        configurable = config["configurable"]
        if configurable.get("thread_id") != self._claim.run_id:
            # A run's checkpointer serves only that run's thread.
            raise ValueError("This checkpointer belongs to another run")
        return self._claim.run_id, str(configurable.get("checkpoint_ns", ""))

    def _config(self, namespace: str, checkpoint_id: str) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": self._claim.run_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def _tuple(self, connection: Any, row: Any) -> CheckpointTuple:
        w = RUN_CHECKPOINT_WRITES.c
        writes = (
            await connection.execute(
                select(RUN_CHECKPOINT_WRITES).where(
                    w.tenant_id == row.tenant_id, w.run_id == row.run_id,
                    w.namespace == row.namespace, w.checkpoint_id == row.checkpoint_id,
                )
            )
        ).all()  # fmt: skip
        ordered = sorted(writes, key=lambda x: writes_sort_key(x.task_path, x.task_id, x.idx))
        return CheckpointTuple(
            config=self._config(row.namespace, row.checkpoint_id),
            checkpoint=self.serde.loads_typed((row.checkpoint_type, row.checkpoint)),
            metadata=self.serde.loads_typed((row.metadata_type, row.metadata)),
            parent_config=self._config(row.namespace, row.parent_id) if row.parent_id else None,
            pending_writes=[
                (x.task_id, x.channel, self.serde.loads_typed((x.value_type, x.value)))
                for x in ordered
            ],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        run_id, namespace = self._thread(config)
        c = RUN_CHECKPOINTS.c
        query = select(RUN_CHECKPOINTS).where(
            c.tenant_id == self._claim.tenant_id, c.run_id == run_id, c.namespace == namespace
        )
        wanted = get_checkpoint_id(config)
        query = (
            query.where(c.checkpoint_id == wanted)
            if wanted
            else query.order_by(c.checkpoint_id.desc()).limit(1)
        )
        async with self._queue.reading(self._claim) as connection:
            row = (await connection.execute(query)).one_or_none()
            return None if row is None else await self._tuple(connection, row)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        c = RUN_CHECKPOINTS.c
        query = select(RUN_CHECKPOINTS).where(
            c.tenant_id == self._claim.tenant_id, c.run_id == self._claim.run_id
        )
        if config is not None:
            _, namespace = self._thread(config)
            query = query.where(c.namespace == namespace)
        if before is not None and (until := get_checkpoint_id(before)):
            query = query.where(c.checkpoint_id < until)
        query = query.order_by(c.checkpoint_id.desc())
        async with self._queue.reading(self._claim) as connection:
            rows = (await connection.execute(query)).all()
            found = 0
            for row in rows:
                item = await self._tuple(connection, row)
                if filter and any(item.metadata.get(k) != v for k, v in filter.items()):
                    continue
                yield item
                found += 1
                if limit is not None and found >= limit:
                    return

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        run_id, namespace = self._thread(config)
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        async with self._queue.holding(self._claim) as connection:
            await connection.execute(
                insert(RUN_CHECKPOINTS).values(
                    tenant_id=self._claim.tenant_id, run_id=run_id, namespace=namespace,
                    checkpoint_id=checkpoint["id"],
                    parent_id=config["configurable"].get("checkpoint_id") or "",
                    attempt=self._claim.attempt, worker=self._claim.owner,
                    checkpoint_type=checkpoint_type, checkpoint=checkpoint_bytes,
                    metadata_type=metadata_type, metadata=metadata_bytes,
                    created_at=datetime.now(UTC),
                )
            )  # fmt: skip
        return self._config(namespace, checkpoint["id"])

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        run_id, namespace = self._thread(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        w = RUN_CHECKPOINT_WRITES.c
        async with self._queue.holding(self._claim) as connection:
            kept = set(
                (
                    await connection.execute(
                        select(w.idx).where(
                            w.tenant_id == self._claim.tenant_id, w.run_id == run_id,
                            w.namespace == namespace, w.checkpoint_id == checkpoint_id,
                            w.task_id == task_id,
                        )
                    )
                ).scalars()
            )  # fmt: skip
            for position, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, position)
                if idx in kept:
                    # Kept once, and never changed: a task replayed writes the same thing.
                    continue
                value_type, value_bytes = self.serde.dumps_typed(value)
                await connection.execute(
                    insert(RUN_CHECKPOINT_WRITES).values(
                        tenant_id=self._claim.tenant_id, run_id=run_id, namespace=namespace,
                        checkpoint_id=checkpoint_id, task_id=task_id, idx=idx, channel=channel,
                        value_type=value_type, value=value_bytes, task_path=task_path,
                    )
                )  # fmt: skip
                kept.add(idx)

    def get_next_version(self, current: str | None, channel: None) -> str:
        # As LangGraph's own savers: increasing, with a random part so concurrent writers differ.
        if current is None:
            number = 0
        elif isinstance(current, int):
            number = current
        else:
            number = int(current.split(".")[0])
        return f"{number + 1:032}.{random.random():016}"  # noqa: S311  # Not a secret.

    # The runtime runs graphs only asynchronously.
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("Use aget_tuple")

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError("Use alist")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError("Use aput")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("Use aput_writes")
