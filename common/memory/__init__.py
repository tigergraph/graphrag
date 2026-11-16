"""PostgreSQL-backed short- and long-term agent memory."""

__all__ = ["MemoryService"]


def __getattr__(name):
    if name == "MemoryService":
        from common.memory.service import MemoryService

        return MemoryService
    raise AttributeError(name)
