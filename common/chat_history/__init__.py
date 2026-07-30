"""TigerGraph-backed, principal-scoped chat history persistence."""

from .models import (
    ConversationPage,
    HistoryMessage,
    TraceEnvelope,
    TraceStep,
)
from .principal import HistoryPrincipal
from .repository import (
    AdminHistoryRepository,
    HistoryConflictError,
    HistoryConfigurationError,
    HistoryNotFoundError,
    HistoryPayloadTooLargeError,
    HistoryRepositoryError,
    HistoryUnavailableError,
    PrincipalHistoryRepository,
    check_history_health,
    create_admin_history_repository,
    create_history_repository,
)

__all__ = [
    "AdminHistoryRepository",
    "ConversationPage",
    "HistoryConflictError",
    "HistoryConfigurationError",
    "HistoryMessage",
    "HistoryNotFoundError",
    "HistoryPayloadTooLargeError",
    "HistoryPrincipal",
    "HistoryRepositoryError",
    "HistoryUnavailableError",
    "PrincipalHistoryRepository",
    "TraceEnvelope",
    "TraceStep",
    "check_history_health",
    "create_admin_history_repository",
    "create_history_repository",
]
