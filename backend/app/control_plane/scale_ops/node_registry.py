"""Persistent process identity and liveness for allowlisted agent nodes."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update

from app.control_plane.persistence.execution_models import AgentNodeInstance


@dataclass(frozen=True)
class NodeProcessIdentity:
    node_id: str
    instance_id: str
    agent_type: str
    entrypoint: str
    agent_version: str
    protocol_version: int
    capabilities: tuple[str, ...]
    max_concurrency: int
    pid: int
    hostname: str
    started_at: datetime

    @property
    def worker_id(self) -> str:
        return f"{self.node_id}:{self.instance_id}"


async def _database_now(db) -> datetime:
    bind = db.get_bind()
    clock = func.clock_timestamp() if bind.dialect.name == "postgresql" else func.now()
    value = await db.scalar(select(clock))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


class AgentNodeRegistry:
    async def register(
        self,
        db,
        *,
        node_id: str,
        agent_type: str,
        entrypoint: str,
        agent_version: str,
        protocol_version: int,
        capabilities: tuple[str, ...],
        max_concurrency: int,
    ) -> NodeProcessIdentity:
        now = await _database_now(db)
        identity = NodeProcessIdentity(
            node_id=node_id,
            instance_id=str(uuid4()),
            agent_type=agent_type,
            entrypoint=entrypoint,
            agent_version=agent_version,
            protocol_version=protocol_version,
            capabilities=tuple(sorted(set(capabilities))),
            max_concurrency=max_concurrency,
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=now,
        )
        db.add(AgentNodeInstance(
            instance_id=identity.instance_id,
            node_id=identity.node_id,
            agent_type=identity.agent_type,
            entrypoint=identity.entrypoint,
            agent_version=identity.agent_version,
            protocol_version=identity.protocol_version,
            capabilities=list(identity.capabilities),
            max_concurrency=identity.max_concurrency,
            pid=identity.pid,
            hostname=identity.hostname,
            started_at=now,
            last_heartbeat_at=now,
        ))
        await db.commit()
        return identity

    async def heartbeat(self, db, identity: NodeProcessIdentity) -> bool:
        now = await _database_now(db)
        result = await db.execute(
            update(AgentNodeInstance)
            .where(
                AgentNodeInstance.instance_id == identity.instance_id,
                AgentNodeInstance.node_id == identity.node_id,
                AgentNodeInstance.stopped_at.is_(None),
            )
            .values(last_heartbeat_at=now)
        )
        await db.commit()
        return result.rowcount == 1

    async def unregister(self, db, identity: NodeProcessIdentity) -> bool:
        now = await _database_now(db)
        result = await db.execute(
            update(AgentNodeInstance)
            .where(
                AgentNodeInstance.instance_id == identity.instance_id,
                AgentNodeInstance.node_id == identity.node_id,
                AgentNodeInstance.stopped_at.is_(None),
            )
            .values(stopped_at=now, last_heartbeat_at=now)
        )
        await db.commit()
        return result.rowcount == 1

    async def healthy_instances(self, db, *, expiry_seconds: int = 20) -> list[AgentNodeInstance]:
        now = await _database_now(db)
        rows = await db.execute(
            select(AgentNodeInstance).where(
                AgentNodeInstance.stopped_at.is_(None),
                AgentNodeInstance.last_heartbeat_at >= now - timedelta(seconds=expiry_seconds),
            )
        )
        return list(rows.scalars())


agent_node_registry = AgentNodeRegistry()
