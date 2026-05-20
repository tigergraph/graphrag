# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");

from common.memory.memory_router import MemoryRoute, decide_memory_route, routing_enabled


def test_no_messages_returns_none():
    assert decide_memory_route("hello", has_messages=False, has_summary=False) == MemoryRoute.NONE


def test_follow_up_stm():
    assert (
        decide_memory_route(
            "Make that shorter",
            has_messages=True,
            has_summary=True,
        )
        == MemoryRoute.STM
    )


def test_early_session_ltm_requires_summary():
    assert (
        decide_memory_route(
            "What was my budget at the start?",
            has_messages=True,
            has_summary=False,
        )
        == MemoryRoute.STM
    )
    assert (
        decide_memory_route(
            "What was my budget at the start?",
            has_messages=True,
            has_summary=True,
        )
        == MemoryRoute.LTM
    )


def test_graph_question_none():
    assert (
        decide_memory_route(
            "How many vertices are in the graph?",
            has_messages=True,
            has_summary=True,
        )
        == MemoryRoute.NONE
    )


def test_mixed_cues_hybrid():
    assert (
        decide_memory_route(
            "Using my budget from earlier, fix the table you just showed",
            has_messages=True,
            has_summary=True,
        )
        == MemoryRoute.HYBRID
    )


def test_routing_enabled_flag():
    assert routing_enabled({"routing_enabled": True}) is True
    assert routing_enabled({"routing_enabled": False}) is False
