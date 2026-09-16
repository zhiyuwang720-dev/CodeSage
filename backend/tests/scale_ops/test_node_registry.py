from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.control_plane.persistence.execution_models import AgentNodeInstance
from app.control_plane.scale_ops.node_registry import AgentNodeRegistry


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(AgentNodeInstance.__table__.create)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def test_same_logical_node_registers_distinct_process_instances():
    engine, factory = await _session_factory()
    registry = AgentNodeRegistry()
    try:
        async with factory() as db:
            first = await registry.register(
                db,
                node_id="pr-node-a",
                agent_type="pr_review",
                entrypoint="quick_review",
                agent_version="1",
                protocol_version=1,
                capabilities=("review.finalize", "pr.diff.read"),
                max_concurrency=1,
            )
        async with factory() as db:
            second = await registry.register(
                db,
                node_id="pr-node-a",
                agent_type="pr_review",
                entrypoint="quick_review",
                agent_version="1",
                protocol_version=1,
                capabilities=("pr.diff.read",),
                max_concurrency=1,
            )
        assert first.node_id == second.node_id
        assert first.instance_id != second.instance_id
        assert first.worker_id != second.worker_id
        assert first.capabilities == ("pr.diff.read", "review.finalize")
    finally:
        await engine.dispose()


async def test_heartbeat_and_unregister_only_mutate_own_instance():
    engine, factory = await _session_factory()
    registry = AgentNodeRegistry()
    try:
        async with factory() as db:
            identity = await registry.register(
                db,
                node_id="pr-node-a",
                agent_type="pr_review",
                entrypoint="quick_review",
                agent_version="1",
                protocol_version=1,
                capabilities=(),
                max_concurrency=1,
            )
        async with factory() as db:
            assert await registry.heartbeat(db, identity) is True
            assert len(await registry.healthy_instances(db)) == 1
            assert await registry.unregister(db, identity) is True
            assert await registry.heartbeat(db, identity) is False
            assert await registry.healthy_instances(db) == []
    finally:
        await engine.dispose()
