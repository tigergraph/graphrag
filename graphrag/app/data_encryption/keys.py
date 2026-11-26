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

"""Key rings, data key files and the AES-GCM value format.

A key is identified by its fingerprint, so the same key always has the same
id, whether it comes from a generated key ring or from a raw key upload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
ID_BYTES = 8
VALUE_VERSION = b"\x01"
FORMAT = 1


class KeyFileError(Exception):
    """A key file or an uploaded key is not valid."""


class DecryptError(Exception):
    """A value cannot be decrypted with the keys that are loaded."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def key_id(key: bytes) -> str:
    """The id of a key: a fingerprint that does not reveal the key."""
    return hmac.new(key, b"graphrag-key-id", hashlib.sha256).hexdigest()[: ID_BYTES * 2]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyFileError("not valid base64") from exc


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad or None)


def unseal(key: bytes, sealed: bytes, aad: bytes = b"") -> bytes:
    try:
        return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], aad or None)
    except InvalidTag as exc:
        raise DecryptError("wrong key or damaged value") from exc


def write_atomic(path: str, data: dict, mode: int = 0o600) -> None:
    """Write JSON to a temporary file in the same folder, then rename it."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Master key ring
# --------------------------------------------------------------------------

@dataclass
class KeyRing:
    """All versions of one master key; new wraps use the active version."""
    versions: Dict[str, bytes] = field(default_factory=dict)
    created: Dict[str, str] = field(default_factory=dict)
    active: str = ""

    @classmethod
    def generate(cls) -> "KeyRing":
        ring = cls()
        ring.add(new_key())
        return ring

    def add(self, key: bytes) -> str:
        if len(key) != KEY_BYTES:
            raise KeyFileError(f"a key must be {KEY_BYTES} bytes")
        kid = key_id(key)
        self.versions[kid] = key
        self.created.setdefault(kid, now())
        self.active = kid
        return kid

    def remove(self, kid: str) -> None:
        if kid == self.active:
            raise KeyFileError("the active key version cannot be removed")
        self.versions.pop(kid, None)
        self.created.pop(kid, None)

    @property
    def active_key(self) -> bytes:
        return self.versions[self.active]

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "active": self.active,
            "versions": [
                {"id": kid, "key": _b64(k), "created_at": self.created.get(kid, "")}
                for kid, k in self.versions.items()
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "KeyRing":
        if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
            raise KeyFileError("not a GraphRAG key file")
        ring = cls()
        for v in data["versions"]:
            key = _unb64(str((v or {}).get("key", "")))
            if len(key) != KEY_BYTES:
                raise KeyFileError(f"a key must be {KEY_BYTES} bytes")
            kid = key_id(key)
            if v.get("id") and v["id"] != kid:
                raise KeyFileError("a key does not match its id")
            ring.versions[kid] = key
            ring.created[kid] = str(v.get("created_at") or "")
        active = str(data.get("active") or "")
        if active not in ring.versions:
            raise KeyFileError("the active key version is missing")
        ring.active = active
        return ring

    @classmethod
    def parse(cls, content: str) -> "KeyRing":
        """A GraphRAG key file, or keys in base64, one per line, the current
        key first (the form a secret manager usually writes)."""
        text = (content or "").strip()
        if not text:
            raise KeyFileError("the key is empty")
        if text.startswith("{"):
            try:
                return cls.from_json(json.loads(text))
            except json.JSONDecodeError as exc:
                raise KeyFileError("not a GraphRAG key file") from exc
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        ring = cls()
        for line in reversed(lines):        # the first line ends up active
            ring.add(_unb64(line))
        return ring

    @classmethod
    def load(cls, path: str) -> Optional["KeyRing"]:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return cls.parse(f.read())

    def save(self, path: str) -> None:
        write_atomic(path, self.to_json())


# --------------------------------------------------------------------------
# Data keys (wrapped with the master key, kept next to the data)
# --------------------------------------------------------------------------

@dataclass
class WrappedKey:
    id: str
    wrapped: bytes
    master_id: str
    created_at: str


@dataclass
class DataKeyFile:
    """The wrapped data keys of one target (a graph or the chat history)."""
    master_scope: str = "global"            # "global" or "graph"
    active: str = ""
    keys: List[WrappedKey] = field(default_factory=list)

    def entry(self, kid: str) -> Optional[WrappedKey]:
        return next((k for k in self.keys if k.id == kid), None)

    def master_ids(self) -> List[str]:
        return sorted({k.master_id for k in self.keys})

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "master_scope": self.master_scope,
            "active": self.active,
            "keys": [{"id": k.id, "wrapped": _b64(k.wrapped), "master_id": k.master_id,
                      "created_at": k.created_at} for k in self.keys],
        }

    @classmethod
    def from_json(cls, data: dict) -> "DataKeyFile":
        return cls(
            master_scope=str(data.get("master_scope") or "global"),
            active=str(data.get("active") or ""),
            keys=[WrappedKey(str(k["id"]), _unb64(k["wrapped"]), str(k["master_id"]),
                             str(k.get("created_at") or "")) for k in data.get("keys") or []],
        )

    @classmethod
    def load(cls, path: str) -> Optional["DataKeyFile"]:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return cls.from_json(json.load(f))

    def save(self, path: str) -> None:
        write_atomic(path, self.to_json())


def wrap(master: bytes, master_kid: str, dek: bytes, target: str) -> WrappedKey:
    return WrappedKey(key_id(dek), seal(master, dek, _aad(target)), master_kid, now())


def unwrap(master: bytes, entry: WrappedKey, target: str) -> bytes:
    dek = unseal(master, entry.wrapped, _aad(target))
    if key_id(dek) != entry.id:
        raise DecryptError("data key does not match its id")
    return dek


def _aad(target: str) -> bytes:
    return f"graphrag-data-key:{target}".encode()


# --------------------------------------------------------------------------
# Value format
# --------------------------------------------------------------------------

class DataKeys:
    """The unwrapped data keys of one target, used to encrypt values."""

    def __init__(self, keys: Dict[str, bytes], active: str):
        self.keys = keys
        self.active = active

    def encrypt(self, plaintext: bytes) -> bytes:
        kid = self.active
        return VALUE_VERSION + bytes.fromhex(kid) + seal(self.keys[kid], plaintext)

    def decrypt(self, blob: bytes) -> bytes:
        kid = value_key_id(blob)
        key = self.keys.get(kid or "")
        if key is None:
            raise DecryptError(f"no data key {kid}")
        return unseal(key, blob[1 + ID_BYTES:])


def value_key_id(blob: bytes) -> Optional[str]:
    """The data key id of an encrypted value, or None if it is not one."""
    if not blob or blob[:1] != VALUE_VERSION or len(blob) < 1 + ID_BYTES + NONCE_BYTES + 16:
        return None
    return blob[1:1 + ID_BYTES].hex()


# Text form, for chat history fields.
TEXT_PREFIX = "enc:v1:"


def is_encrypted_text(text) -> bool:
    return isinstance(text, str) and text.startswith(TEXT_PREFIX)


def encrypt_text(keys: DataKeys, text: str) -> str:
    return TEXT_PREFIX + _b64(keys.encrypt(text.encode("utf-8")))


def text_blob(text: str) -> bytes:
    return _unb64(text[len(TEXT_PREFIX):])


def decrypt_text(keys: DataKeys, text: str) -> str:
    return keys.decrypt(text_blob(text)).decode("utf-8")
