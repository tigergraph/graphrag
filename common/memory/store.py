# Copyright (c) 2024-2026 TigerGraph, Inc.
"""Canonical relational store for chat transcripts and mem0 payloads."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from time import sleep
from typing import Iterable, Iterator

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from common.memory.models import (
    Base,
    Conversation,
    ConversationSummary,
    MemoryFact,
    MemoryHistory,
    MemoryOutbox,
    Message,
    utcnow,
)


def memory_user_id(username: str, graphname: str) -> str:
    """mem0 user ID; graph is part of the tenant boundary."""
    return f"{graphname}\0{username}"


def scope_hash(username: str, graphname: str) -> str:
    return hashlib.sha256(memory_user_id(username, graphname).encode()).hexdigest()[:32]


def fact_index_id(username: str, graphname: str, fact_id: str) -> str:
    return f"{scope_hash(username, graphname)}:{fact_id}"


class PostgresMemoryStore:
    def __init__(self, database_url: str, *, create_schema: bool = True):
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            database_url, pool_pre_ping=True, future=True, connect_args=connect_args
        )
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)
        if create_schema:
            for attempt in range(6):
                try:
                    Base.metadata.create_all(self.engine)
                    break
                except OperationalError:
                    if attempt == 5:
                        raise
                    sleep(2 ** attempt)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def healthcheck(self) -> bool:
        with self.session() as session:
            session.execute(select(1))
        return True

    @staticmethod
    def conversation_json(row: Conversation) -> dict:
        return {
            "id": row.id,
            "user_id": row.username,
            "graphname": row.graphname,
            "conversation_id": row.conversation_id,
            "name": row.name,
            "create_ts": row.created_at,
            "update_ts": row.updated_at,
            "delete_ts": row.deleted_at,
        }

    @staticmethod
    def message_json(row: Message) -> dict:
        return {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "message_id": row.message_id,
            "parent_id": row.parent_id,
            "model": row.model,
            "content": row.content,
            "role": row.role,
            "response_time": row.response_time,
            "feedback": row.feedback,
            "comment": row.comment,
            "answered_question": row.answered_question,
            "response_type": row.response_type,
            "query_sources": row.query_sources,
            "create_ts": row.created_at,
            "update_ts": row.updated_at,
            "delete_ts": row.deleted_at,
        }

    def _owned_conversation(
        self, session: Session, username: str, graphname: str, conversation_id: str
    ) -> Conversation | None:
        return session.scalar(
            select(Conversation).where(
                Conversation.username == username,
                Conversation.graphname == graphname,
                Conversation.conversation_id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
        )

    def list_conversations(self, username: str, graphname: str) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(
                select(Conversation)
                .where(
                    Conversation.username == username,
                    Conversation.graphname == graphname,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.updated_at.desc())
            ).all()
            return [self.conversation_json(row) for row in rows]

    def get_messages(
        self, username: str, graphname: str, conversation_id: str
    ) -> list[dict]:
        with self.session() as session:
            if self._owned_conversation(session, username, graphname, conversation_id) is None:
                return []
            rows = session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.sequence)
            ).all()
            return [self.message_json(row) for row in rows]

    def save_message(
        self,
        username: str,
        graphname: str,
        message: dict,
        *,
        conversation_name: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> dict:
        conversation_id = str(message["conversation_id"])
        message_id = str(message["message_id"])
        with self.session() as session:
            conversation = self._owned_conversation(
                session, username, graphname, conversation_id
            )
            if conversation is None:
                if session.scalar(
                    select(Conversation).where(
                        Conversation.conversation_id == conversation_id
                    )
                ):
                    raise PermissionError("Conversation belongs to another scope")
                content = str(message.get("content") or "")
                conversation = Conversation(
                    username=username,
                    graphname=graphname,
                    conversation_id=conversation_id,
                    name=conversation_name
                    or (content[:80] if message.get("role") == "user" else "New conversation"),
                    created_at=created_at or utcnow(),
                    updated_at=updated_at or created_at or utcnow(),
                )
                session.add(conversation)
                session.flush()

            existing = session.scalar(select(Message).where(Message.message_id == message_id))
            if existing:
                if existing.conversation_id != conversation_id:
                    raise PermissionError("Message belongs to another conversation")
                existing.feedback = message.get("feedback")
                existing.comment = message.get("comment")
                existing.updated_at = updated_at or utcnow()
                return self.message_json(existing)

            sequence = int(
                session.scalar(
                    select(func.coalesce(func.max(Message.sequence), 0)).where(
                        Message.conversation_id == conversation_id
                    )
                )
            ) + 1
            row = Message(
                conversation_id=conversation_id,
                message_id=message_id,
                parent_id=message.get("parent_id"),
                sequence=sequence,
                model=message.get("model"),
                content=str(message.get("content") or ""),
                role=str(message.get("role") or "user"),
                response_time=message.get("response_time"),
                feedback=message.get("feedback"),
                comment=message.get("comment"),
                answered_question=message.get("answered_question"),
                response_type=message.get("response_type"),
                query_sources=message.get("query_sources"),
                created_at=created_at or utcnow(),
                updated_at=updated_at or created_at or utcnow(),
            )
            session.add(row)
            conversation.updated_at = row.updated_at
            session.flush()
            return self.message_json(row)

    def feedback_messages(
        self, username: str, graphname: str, *, include_all: bool = False
    ) -> list[dict]:
        with self.session() as session:
            stmt = (
                select(Message)
                .join(
                    Conversation,
                    Conversation.conversation_id == Message.conversation_id,
                )
                .where(
                    Conversation.graphname == graphname,
                    Conversation.deleted_at.is_(None),
                    Message.deleted_at.is_(None),
                )
            )
            if not include_all:
                stmt = stmt.where(Conversation.username == username)
            return [
                self.message_json(row)
                for row in session.scalars(stmt.order_by(Message.created_at.desc())).all()
            ]

    def delete_conversation(
        self, username: str, graphname: str, conversation_id: str
    ) -> list[str]:
        with self.session() as session:
            conversation = self._owned_conversation(
                session, username, graphname, conversation_id
            )
            if conversation is None:
                return []
            now = utcnow()
            conversation.deleted_at = now
            session.execute(
                update(Message)
                .where(Message.conversation_id == conversation_id)
                .values(deleted_at=now)
            )
            facts = list(
                session.scalars(
                    select(MemoryFact).where(
                        MemoryFact.username == username,
                        MemoryFact.graphname == graphname,
                        MemoryFact.conversation_id == conversation_id,
                        MemoryFact.deleted_at.is_(None),
                    )
                ).all()
            )
            for fact in facts:
                fact.deleted_at = now
                self._enqueue(session, "delete", fact.fact_id)
            return [fact.fact_id for fact in facts]

    def get_summary(
        self, username: str, graphname: str, conversation_id: str
    ) -> ConversationSummary | None:
        with self.session() as session:
            if self._owned_conversation(session, username, graphname, conversation_id) is None:
                return None
            return session.get(ConversationSummary, conversation_id)

    def unsummarized_messages(
        self, username: str, graphname: str, conversation_id: str
    ) -> tuple[ConversationSummary | None, list[Message]]:
        with self.session() as session:
            if self._owned_conversation(session, username, graphname, conversation_id) is None:
                return None, []
            summary = session.get(ConversationSummary, conversation_id)
            after = summary.last_summarized_sequence if summary else 0
            messages = list(
                session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.sequence > after,
                        Message.deleted_at.is_(None),
                    )
                    .order_by(Message.sequence)
                ).all()
            )
            return summary, messages

    def update_summary(
        self,
        username: str,
        graphname: str,
        conversation_id: str,
        *,
        summary_text: str,
        through_sequence: int,
        source_tokens: int,
        summary_tokens: int,
        model: str,
    ) -> None:
        with self.session() as session:
            conversation = self._owned_conversation(
                session, username, graphname, conversation_id
            )
            if conversation is None:
                raise PermissionError("Conversation does not belong to caller")
            # Row lock serializes compaction for this conversation on PostgreSQL.
            session.execute(
                select(Conversation)
                .where(Conversation.id == conversation.id)
                .with_for_update()
            )
            row = session.get(ConversationSummary, conversation_id)
            if row is None:
                row = ConversationSummary(
                    conversation_id=conversation_id,
                    summary="",
                    last_summarized_sequence=0,
                    source_tokens=0,
                    summary_tokens=0,
                    version=0,
                )
                session.add(row)
            if through_sequence <= row.last_summarized_sequence:
                return
            row.summary = summary_text
            row.last_summarized_sequence = through_sequence
            row.source_tokens = source_tokens
            row.summary_tokens = summary_tokens
            row.model = model
            row.version = (row.version or 0) + 1

    def put_fact(
        self,
        fact_id: str,
        username: str,
        graphname: str,
        conversation_id: str | None,
        text: str,
        payload: dict,
    ) -> MemoryFact:
        with self.session() as session:
            row = session.get(MemoryFact, fact_id)
            if row is None:
                row = MemoryFact(
                    fact_id=fact_id,
                    index_id=fact_index_id(username, graphname, fact_id),
                    username=username,
                    graphname=graphname,
                    conversation_id=conversation_id,
                    text=text,
                    payload=payload,
                )
                session.add(row)
            elif row.username != username or row.graphname != graphname:
                raise PermissionError("Memory fact scope cannot change")
            else:
                row.text = text
                row.payload = payload
                row.deleted_at = None
            self._enqueue(session, "upsert", fact_id)
            session.flush()
            return row

    def get_fact(self, fact_id: str, *, include_deleted: bool = False) -> MemoryFact | None:
        with self.session() as session:
            stmt = select(MemoryFact).where(MemoryFact.fact_id == fact_id)
            if not include_deleted:
                stmt = stmt.where(MemoryFact.deleted_at.is_(None))
            return session.scalar(stmt)

    def has_facts(self, username: str, graphname: str) -> bool:
        with self.session() as session:
            return session.scalar(
                select(MemoryFact.fact_id).where(
                    MemoryFact.username == username,
                    MemoryFact.graphname == graphname,
                    MemoryFact.deleted_at.is_(None),
                ).limit(1)
            ) is not None

    def list_facts(
        self, username: str, graphname: str, conversation_id: str | None = None
    ) -> list[MemoryFact]:
        with self.session() as session:
            stmt = select(MemoryFact).where(
                MemoryFact.username == username,
                MemoryFact.graphname == graphname,
                MemoryFact.deleted_at.is_(None),
            )
            if conversation_id is not None:
                stmt = stmt.where(MemoryFact.conversation_id == conversation_id)
            return list(session.scalars(stmt.order_by(MemoryFact.created_at.desc())).all())

    def facts_by_index_ids(
        self, username: str, graphname: str, index_ids: Iterable[str]
    ) -> list[MemoryFact]:
        ordered = list(index_ids)
        if not ordered:
            return []
        with self.session() as session:
            rows = session.scalars(
                select(MemoryFact).where(
                    MemoryFact.username == username,
                    MemoryFact.graphname == graphname,
                    MemoryFact.index_id.in_(ordered),
                    MemoryFact.deleted_at.is_(None),
                )
            ).all()
            by_id = {row.index_id: row for row in rows}
            return [by_id[index] for index in ordered if index in by_id]

    def delete_fact(self, fact_id: str) -> None:
        with self.session() as session:
            row = session.get(MemoryFact, fact_id)
            if row and row.deleted_at is None:
                row.deleted_at = utcnow()
                self._enqueue(session, "delete", fact_id)

    def enforce_fact_cap(self, username: str, graphname: str, maximum: int) -> int:
        with self.session() as session:
            rows = list(
                session.scalars(
                    select(MemoryFact)
                    .where(
                        MemoryFact.username == username,
                        MemoryFact.graphname == graphname,
                        MemoryFact.deleted_at.is_(None),
                    )
                    .order_by(MemoryFact.updated_at.desc())
                ).all()
            )
            evicted = rows[maximum:]
            for row in evicted:
                row.deleted_at = utcnow()
                self._enqueue(session, "delete", row.fact_id)
            return len(evicted)

    def apply_retention(
        self, username: str, graphname: str, retention_days: int
    ) -> None:
        expires_at = utcnow() + timedelta(days=retention_days)
        with self.session() as session:
            rows = session.scalars(
                select(MemoryFact).where(
                    MemoryFact.username == username,
                    MemoryFact.graphname == graphname,
                    MemoryFact.deleted_at.is_(None),
                    MemoryFact.expires_at.is_(None),
                )
            ).all()
            for row in rows:
                row.expires_at = expires_at

    def expire_facts(self, username: str, graphname: str) -> int:
        with self.session() as session:
            rows = session.scalars(
                select(MemoryFact).where(
                    MemoryFact.username == username,
                    MemoryFact.graphname == graphname,
                    MemoryFact.deleted_at.is_(None),
                    MemoryFact.expires_at.is_not(None),
                    MemoryFact.expires_at <= utcnow(),
                )
            ).all()
            for row in rows:
                row.deleted_at = utcnow()
                self._enqueue(session, "delete", row.fact_id)
            return len(rows)

    def add_history(self, record: dict) -> None:
        with self.session() as session:
            session.add(
                MemoryHistory(
                    memory_id=str(record.get("memory_id")),
                    old_memory=record.get("old_memory"),
                    new_memory=record.get("new_memory"),
                    event=str(record.get("event") or "ADD"),
                    created_at=_parse_datetime(record.get("created_at")) or utcnow(),
                    updated_at=_parse_datetime(record.get("updated_at")),
                    is_deleted=bool(record.get("is_deleted", False)),
                    actor_id=record.get("actor_id"),
                    role=record.get("role"),
                )
            )

    def get_history(self, memory_id: str) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(
                select(MemoryHistory)
                .where(MemoryHistory.memory_id == memory_id)
                .order_by(MemoryHistory.created_at)
            ).all()
            return [
                {
                    "id": row.id,
                    "memory_id": row.memory_id,
                    "old_memory": row.old_memory,
                    "new_memory": row.new_memory,
                    "event": row.event,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    "is_deleted": row.is_deleted,
                    "actor_id": row.actor_id,
                    "role": row.role,
                }
                for row in rows
            ]

    @staticmethod
    def _enqueue(session: Session, operation: str, fact_id: str) -> None:
        item = session.scalar(
            select(MemoryOutbox).where(
                MemoryOutbox.operation == operation, MemoryOutbox.fact_id == fact_id
            )
        )
        if item:
            item.completed_at = None
            item.next_attempt_at = utcnow()
            item.last_error = None
        else:
            session.add(MemoryOutbox(operation=operation, fact_id=fact_id))

    def pending_outbox(self, limit: int = 100) -> list[MemoryOutbox]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(MemoryOutbox)
                    .where(
                        MemoryOutbox.completed_at.is_(None),
                        MemoryOutbox.next_attempt_at <= utcnow(),
                    )
                    .order_by(MemoryOutbox.created_at)
                    .limit(limit)
                ).all()
            )

    def complete_outbox(self, outbox_id: str, *, indexed: bool) -> None:
        with self.session() as session:
            item = session.get(MemoryOutbox, outbox_id)
            if not item:
                return
            item.completed_at = utcnow()
            fact = session.get(MemoryFact, item.fact_id)
            if fact:
                fact.indexed = indexed

    def mark_fact_indexed(self, fact_id: str) -> None:
        with self.session() as session:
            fact = session.get(MemoryFact, fact_id)
            if fact:
                fact.indexed = True
            items = session.scalars(
                select(MemoryOutbox).where(
                    MemoryOutbox.fact_id == fact_id,
                    MemoryOutbox.operation == "upsert",
                    MemoryOutbox.completed_at.is_(None),
                )
            ).all()
            for item in items:
                item.completed_at = utcnow()

    def mark_fact_unindexed(self, fact_id: str) -> None:
        with self.session() as session:
            fact = session.get(MemoryFact, fact_id)
            if fact:
                fact.indexed = False
            items = session.scalars(
                select(MemoryOutbox).where(
                    MemoryOutbox.fact_id == fact_id,
                    MemoryOutbox.operation == "delete",
                    MemoryOutbox.completed_at.is_(None),
                )
            ).all()
            for item in items:
                item.completed_at = utcnow()

    def fail_outbox(self, outbox_id: str, error: str) -> None:
        with self.session() as session:
            item = session.get(MemoryOutbox, outbox_id)
            if item:
                item.attempts += 1
                item.last_error = error[:4000]
                delay = min(3600, 2 ** min(item.attempts, 10))
                item.next_attempt_at = utcnow() + timedelta(seconds=delay)

def _parse_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
