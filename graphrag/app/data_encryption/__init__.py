# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Encryption at rest of the Deep Agent memory and the chat history.

Always on. See ``manager.py`` for the key model and ``keys.py`` for the
value format.
"""

from __future__ import annotations

from data_encryption.key_manager import (CHAT_TARGET, EncryptionManager, Locked, Rejected,
                                     get_manager, set_manager_for_tests)


def register(manager: EncryptionManager) -> EncryptionManager:
    """Connect the manager to the data it encrypts."""
    from data_encryption import chat_crypto, memory_crypto

    def sweep_memory(target, keys, progress):
        import os
        path = os.path.realpath(manager.paths.memory(target))
        if os.path.exists(path):
            memory_crypto.sweep_memory_file(path, keys, progress)

    manager.sweepers["memory"] = sweep_memory
    manager.sweepers["chat"] = chat_crypto.sweep_chat
    manager.sweepers["reset_chat"] = chat_crypto.reset_chat

    def close_memory(target):
        if target != CHAT_TARGET:
            from deep_agent.memory import close_graph_backends
            close_graph_backends(target)

    manager.on_reset.append(close_memory)
    return manager


def manager() -> EncryptionManager:
    m = get_manager()
    if "memory" not in m.sweepers:
        register(m)
    return m


__all__ = ["CHAT_TARGET", "EncryptionManager", "Locked", "Rejected", "manager", "register",
           "set_manager_for_tests"]
