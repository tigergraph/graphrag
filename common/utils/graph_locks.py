"""
Graph-level locking mechanism to prevent concurrent operations on the same graph.
Uses threading.Lock for sync operations and asyncio.Lock for async rebuild operations.
"""
import asyncio
import threading
import logging
from typing import Dict, Optional
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Module-level lock management
_graph_locks: Dict[str, threading.Lock] = {}
_locks_dict_lock = threading.Lock()

# Records the operation currently holding each graph's lock so the UI can
# reflect long-running work when a dialog remounts.
_current_operations: Dict[str, str] = {}

# Global rebuild lock (only one rebuild at a time across all graphs)
# Use asyncio.Lock for async operations
_rebuild_lock: Optional[asyncio.Lock] = None
_currently_rebuilding_graph: str = None
_rebuild_graph_lock = threading.Lock()  # Protects _currently_rebuilding_graph


def _get_rebuild_lock() -> asyncio.Lock:
    """Get or create the async rebuild lock."""
    global _rebuild_lock
    if _rebuild_lock is None:
        _rebuild_lock = asyncio.Lock()
    return _rebuild_lock


def get_graph_lock(graphname: str) -> threading.Lock:
    """Get or create a lock for a specific graph."""
    with _locks_dict_lock:
        if graphname not in _graph_locks:
            _graph_locks[graphname] = threading.Lock()
            logger.debug(f"Created new lock for graph: {graphname}")
        return _graph_locks[graphname]


def acquire_graph_lock(graphname: str, operation: str = "operation") -> bool:
    """
    Try to acquire lock for a graph. Returns True if acquired, False if already locked.

    Args:
        graphname: Name of the graph to lock
        operation: Description of the operation (for logging and status reporting)
    """
    lock = get_graph_lock(graphname)
    acquired = lock.acquire(blocking=False)

    if acquired:
        _current_operations[graphname] = operation
        logger.info(f"Lock acquired for graph '{graphname}' - {operation}")
    else:
        logger.warning(f"Lock already held for graph '{graphname}' - {operation} blocked")

    return acquired


def release_graph_lock(graphname: str, operation: str = "operation"):
    """
    Release the lock for a graph.

    Args:
        graphname: Name of the graph to unlock
        operation: Description of the operation (for logging)
    """
    lock = get_graph_lock(graphname)
    if lock.locked():
        _current_operations.pop(graphname, None)
        lock.release()
        logger.info(f"Lock released for graph '{graphname}' - {operation} completed")


def get_current_operation(graphname: str) -> Optional[str]:
    """Return the operation name currently holding ``graphname``'s lock,
    or ``None`` if the lock is free. Used by status endpoints so the UI
    can reflect long-running work that's still in flight on the server.
    """
    lock = _graph_locks.get(graphname)
    if lock is None or not lock.locked():
        return None
    return _current_operations.get(graphname)


def raise_if_locked(graphname: str, operation: str = "operation"):
    """
    Try to acquire lock or raise HTTPException with 409 Conflict status.
    Used for FastAPI endpoints.
    
    Args:
        graphname: Name of the graph to lock
        operation: Description of the operation
        
    Raises:
        HTTPException: 409 Conflict if lock is already held
    """
    if not acquire_graph_lock(graphname, operation):
        raise HTTPException(
            status_code=409,
            detail=f"Another operation is already in progress for graph '{graphname}'. Please wait and try again."
        )

# =====================================================
# Global Rebuild Lock Functions
# =====================================================

async def acquire_rebuild_lock(graphname: str) -> bool:
    """
    Try to acquire the global rebuild lock (only one rebuild at a time across all graphs).
    Returns True if acquired immediately, False if another rebuild is in progress.
    Non-blocking operation - returns instantly.
    
    Args:
        graphname: Name of the graph requesting rebuild
    """
    global _currently_rebuilding_graph
    
    lock = _get_rebuild_lock()
    
    # Non-blocking check: return immediately if lock is busy
    if lock.locked():
        logger.warning(f"Rebuild lock busy - another graph is rebuilding. Request from: {graphname}")
        return False
    
    # Try to acquire the lock (should be instant since we checked it's not locked)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.01)  # 10ms safety timeout
        with _rebuild_graph_lock:
            _currently_rebuilding_graph = graphname
        logger.info(f"Global rebuild lock acquired for graph: {graphname}")
        return True
    except asyncio.TimeoutError:
        # Race condition: lock was acquired between check and acquire
        logger.warning(f"Rebuild lock busy - another graph is rebuilding. Request from: {graphname}")
        return False


def release_rebuild_lock(graphname: str):
    """
    Release the global rebuild lock.
    
    Args:
        graphname: Name of the graph releasing rebuild lock
    """
    global _currently_rebuilding_graph
    
    lock = _get_rebuild_lock()
    if lock.locked():
        with _rebuild_graph_lock:
            _currently_rebuilding_graph = None
        lock.release()
        logger.info(f"Global rebuild lock released for graph: {graphname}")


def get_rebuilding_graph() -> str:
    """
    Get the name of the graph currently being rebuilt.
    Returns None if no rebuild is in progress.
    """
    with _rebuild_graph_lock:
        return _currently_rebuilding_graph

