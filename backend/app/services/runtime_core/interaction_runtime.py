from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .session_state import SessionRuntimeState


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InteractionRuntime:
    def create_todo(
        self,
        state: SessionRuntimeState,
        *,
        agent_type: str,
        title: str,
        details: str | None = None,
        todo_id: str | None = None,
    ) -> dict[str, Any]:
        agent_state = state.ensure_agent_state(agent_type)
        todo = {
            "id": str(todo_id or f"todo-{uuid4().hex[:12]}"),
            "title": str(title or "").strip(),
            "details": str(details or "").strip() or None,
            "status": "pending",
            "agent_type": agent_state.agent_type,
            "created_at": _utc_now(),
            "completed_at": None,
        }
        agent_state.pending_todos.append(todo)
        state.metadata.setdefault("todos", {})[todo["id"]] = dict(todo)
        return dict(todo)

    def complete_todo(
        self,
        state: SessionRuntimeState,
        *,
        agent_type: str,
        todo_id: str,
    ) -> dict[str, Any]:
        agent_state = state.ensure_agent_state(agent_type)
        for todo in agent_state.pending_todos:
            if str(todo.get("id")) != str(todo_id):
                continue
            todo["status"] = "completed"
            todo["completed_at"] = _utc_now()
            state.metadata.setdefault("todos", {})[str(todo_id)] = dict(todo)
            return dict(todo)
        raise KeyError(f"Unknown todo_id: {todo_id}")

    def ask_user(
        self,
        state: SessionRuntimeState,
        *,
        agent_type: str,
        question: str,
        context: dict[str, Any] | None = None,
        question_id: str | None = None,
    ) -> dict[str, Any]:
        agent_state = state.ensure_agent_state(agent_type)
        record = {
            "id": str(question_id or f"ask-{uuid4().hex[:12]}"),
            "question": str(question or "").strip(),
            "context": dict(context or {}),
            "status": "pending",
            "agent_type": agent_state.agent_type,
            "created_at": _utc_now(),
            "answered_at": None,
            "answer": None,
        }
        agent_state.pending_questions.append(record)
        state.pending_questions.append(record)
        state.metadata.setdefault("questions", {})[record["id"]] = dict(record)
        return dict(record)

    def resolve_question(
        self,
        state: SessionRuntimeState,
        *,
        agent_type: str,
        question_id: str,
        answer: str,
    ) -> dict[str, Any]:
        agent_state = state.ensure_agent_state(agent_type)
        answered: dict[str, Any] | None = None
        remaining_agent_questions: list[dict[str, Any]] = []
        for item in agent_state.pending_questions:
            if str(item.get("id")) == str(question_id):
                item["status"] = "answered"
                item["answer"] = str(answer or "")
                item["answered_at"] = _utc_now()
                answered = dict(item)
                continue
            remaining_agent_questions.append(item)
        agent_state.pending_questions = remaining_agent_questions

        remaining_session_questions: list[dict[str, Any]] = []
        for item in state.pending_questions:
            if str(item.get("id")) == str(question_id):
                item["status"] = "answered"
                item["answer"] = str(answer or "")
                item["answered_at"] = answered.get("answered_at") if answered else _utc_now()
                answered = dict(item)
                continue
            remaining_session_questions.append(item)
        state.pending_questions = remaining_session_questions
        if answered is None:
            raise KeyError(f"Unknown question_id: {question_id}")
        state.metadata.setdefault("questions", {})[str(question_id)] = dict(answered)
        return dict(answered)

    def enter_plan_mode(
        self,
        state: SessionRuntimeState,
        *,
        agent_type: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        state.permission_mode = "plan"
        plan_state = state.metadata.setdefault("plan_mode", {})
        transition_count = int(plan_state.get("transition_count") or 0) + 1
        plan_state.update(
            {
                "active": True,
                "owner_agent": str(agent_type or "").strip() or None,
                "reason": str(reason or "").strip() or None,
                "entered_at": _utc_now(),
                "last_exit_reason": plan_state.get("last_exit_reason"),
                "transition_count": transition_count,
            }
        )
        return dict(plan_state)

    def exit_plan_mode(
        self,
        state: SessionRuntimeState,
        *,
        agent_type: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        state.permission_mode = "default"
        plan_state = state.metadata.setdefault("plan_mode", {})
        transition_count = int(plan_state.get("transition_count") or 0) + 1
        plan_state.update(
            {
                "active": False,
                "owner_agent": str(agent_type or "").strip() or plan_state.get("owner_agent"),
                "exited_at": _utc_now(),
                "last_exit_reason": str(reason or "").strip() or None,
                "transition_count": transition_count,
            }
        )
        return dict(plan_state)
