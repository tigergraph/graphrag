# Copyright (c) 2024-2026 TigerGraph, Inc.
"""SQLAlchemy models for PostgreSQL-backed conversation memory."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "memory_conversations"
    __table_args__ = (
        UniqueConstraint(
            "username", "graphname", "conversation_id", name="uq_memory_conversation_scope"
        ),
        Index("ix_memory_conversation_owner", "username", "graphname", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    graphname: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "memory_messages"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_memory_message_id"),
        Index("ix_memory_message_thread", "conversation_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    response_time: Mapped[Optional[float]] = mapped_column(Float)
    feedback: Mapped[Optional[int]] = mapped_column(Integer)
    comment: Mapped[Optional[str]] = mapped_column(Text)
    answered_question: Mapped[Optional[bool]] = mapped_column(Boolean)
    response_type: Mapped[Optional[str]] = mapped_column(String(64))
    query_sources: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ConversationSummary(Base):
    __tablename__ = "memory_conversation_summaries"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_summarized_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    __table_args__ = (
        Index("ix_memory_fact_scope", "username", "graphname", "deleted_at"),
        Index("ix_memory_fact_conversation", "conversation_id"),
    )

    fact_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    index_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    graphname: Mapped[str] = mapped_column(String(256), nullable=False)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MemoryHistory(Base):
    __tablename__ = "memory_fact_history"
    __table_args__ = (Index("ix_memory_history_fact", "memory_id", "created_at"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    memory_id: Mapped[str] = mapped_column(String(36), nullable=False)
    old_memory: Mapped[Optional[str]] = mapped_column(Text)
    new_memory: Mapped[Optional[str]] = mapped_column(Text)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(256))
    role: Mapped[Optional[str]] = mapped_column(String(32))


class MemoryOutbox(Base):
    __tablename__ = "memory_outbox"
    __table_args__ = (
        UniqueConstraint("operation", "fact_id", name="uq_memory_outbox_operation"),
        Index("ix_memory_outbox_pending", "completed_at", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    fact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


