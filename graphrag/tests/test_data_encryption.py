"""Encryption at rest of the agent memory and the chat history."""
import base64
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import httpx

import data_encryption
from data_encryption import chat_crypto, key_manager as manager_mod, memory_crypto
from data_encryption.keys import (DataKeyFile, KeyFileError, KeyRing, is_encrypted_text, new_key,
                                  text_blob, value_key_id)
from data_encryption.key_manager import (CHAT_TARGET, EncryptionManager, Locked, NeedsPreviousKey,
                                     Rejected)


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "configs")
        os.makedirs(os.path.join(self.root, "graph_data"))
        os.makedirs(os.path.join(self.root, "graph_configs"))
        os.environ.pop("GRAPHRAG_ENCRYPTION_KEY_FILE", None)
        self.audit = []
        p = mock.patch.object(manager_mod, "_audit",
                              lambda action, actor, graph, result, **f: self.audit.append(
                                  dict(action=action, user=actor, graph=graph, result=result, **f)))
        p.start()
        self.addCleanup(p.stop)
        self.fresh()

    def tearDown(self):
        data_encryption.set_manager_for_tests(None)
        os.environ.pop("GRAPHRAG_ENCRYPTION_KEY_FILE", None)
        self._tmp.cleanup()

    def fresh(self) -> EncryptionManager:
        """A new manager on the same files (a restart)."""
        m = data_encryption.register(EncryptionManager(self.root))
        m.schedule = m.run_sweep            # run jobs in the test thread
        data_encryption.set_manager_for_tests(m)
        self.m = m
        return m

    def cfg(self, *parts):
        return os.path.join(self.root, "graph_configs", *parts)

    def memory_path(self, g):
        return os.path.join(self.root, "graph_data", g, "agent_memory.sqlite")

    def dk(self, g):
        name = "chat_history.data_key.json" if g == CHAT_TARGET else os.path.join(g, "data_key.json")
        return DataKeyFile.load(self.cfg(name))

    def mount(self, ring_or_text) -> str:
        path = os.path.join(self._tmp.name, "secret", "master.key")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(ring_or_text if isinstance(ring_or_text, str) else json.dumps(ring_or_text.to_json()))
        os.environ["GRAPHRAG_ENCRYPTION_KEY_FILE"] = path
        return path


class TestKeyFiles(unittest.TestCase):
    def test_key_file_and_base64_forms(self):
        ring = KeyRing.generate()
        self.assertEqual(KeyRing.parse(json.dumps(ring.to_json())).active, ring.active)
        raw = base64.b64encode(ring.active_key).decode()
        self.assertEqual(KeyRing.parse(raw).active, ring.active)          # same key, same id
        new, old = new_key(), new_key()
        two = KeyRing.parse(base64.b64encode(new).decode() + "\n" +
                            base64.b64encode(old).decode() + "\n")
        self.assertEqual(two.active_key, new)                             # first line is current
        self.assertEqual(len(two.versions), 2)

    def test_bad_keys_are_rejected(self):
        for bad in ["", "not base64!", "{}", base64.b64encode(b"short").decode()]:
            with self.assertRaises(KeyFileError):
                KeyRing.parse(bad)
        ring = KeyRing.generate().to_json()
        ring["versions"][0]["id"] = "0" * 16
        with self.assertRaises(KeyFileError):
            KeyRing.from_json(ring)

    def test_key_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "master.key")
            KeyRing.generate().save(p)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


class TestManagedKey(_Env):
    def test_keys_are_config(self):
        keys = self.m.keys("G")
        self.assertTrue(os.path.exists(self.cfg("master.key")))
        self.assertEqual(self.dk("G").active, keys.active)
        self.assertEqual(os.listdir(os.path.join(self.root, "graph_data")), [])
        self.assertEqual(keys.decrypt(keys.encrypt(b"secret")), b"secret")

    def test_missing_key_locks_and_upload_restores(self):
        self.m.keys("G")
        saved = open(self.cfg("master.key")).read()
        os.unlink(self.cfg("master.key"))
        m = self.fresh()
        m.startup()
        self.assertFalse(os.path.exists(self.cfg("master.key")))      # not generated again
        lock = m.locked("G")
        self.assertEqual((lock.reason, lock.needed), ("master_key_missing", [json.loads(saved)["active"]]))
        with self.assertRaises(Rejected):
            m.upload_key(json.dumps(KeyRing.generate().to_json()), actor="admin")
        self.assertFalse(os.path.exists(self.cfg("master.key")))
        m.upload_key(saved, actor="admin")
        self.assertIsNone(m.locked("G"))
        self.assertEqual([a["result"] for a in self.audit if a["action"] == "upload"],
                         ["rejected", "ok"])

    def test_upload_only_when_locked(self):
        self.m.keys("G")
        with self.assertRaises(Rejected):
            self.m.upload_key(open(self.cfg("master.key")).read())

    def test_rotate_generates_or_uses_own_key(self):
        keys = self.m.keys("G")
        blob = keys.encrypt(b"x")
        old = KeyRing.load(self.cfg("master.key")).active
        self.m.rotate_master(actor="admin")
        ring = KeyRing.load(self.cfg("master.key"))
        self.assertNotEqual(ring.active, old)
        self.assertEqual(list(ring.versions), [ring.active])           # old version removed
        self.assertEqual(self.dk("G").master_ids(), [ring.active])
        own = new_key()
        self.m.rotate_master(key=base64.b64encode(own).decode(), actor="admin")
        self.assertEqual(KeyRing.load(self.cfg("master.key")).active_key, own)
        self.assertEqual(self.fresh().keys("G").decrypt(blob), b"x")
        with self.assertRaises(Rejected):                               # same key again
            self.m.rotate_master(key=base64.b64encode(own).decode())
        self.assertEqual(self.audit[-1]["result"], "rejected")

    def test_interrupted_rotation_resumes(self):
        self.m.keys("G")
        self.m.keys("H")
        ring = KeyRing.load(self.cfg("master.key"))
        old = ring.active
        ring.add(new_key())                     # new version saved, data keys not wrapped yet
        ring.save(self.cfg("master.key"))
        m = self.fresh()
        self.assertIsNone(m.locked("G"))
        m.startup()
        ring = KeyRing.load(self.cfg("master.key"))
        self.assertNotIn(old, ring.versions)
        for g in ("G", "H"):
            self.assertEqual(self.dk(g).master_ids(), [ring.active])

    def test_graph_key(self):
        self.m.keys("G")
        self.assertEqual(self.m.use_graph_key("G")["scope"], "graph")
        g = KeyRing.load(self.cfg("G", "master.key"))
        self.assertNotIn(KeyRing.load(self.cfg("master.key")).active, g.versions)
        os.unlink(self.cfg("G", "master.key"))
        self.assertEqual(self.fresh().locked("G").reason, "master_key_missing")
        g.save(self.cfg("G", "master.key"))
        m = self.fresh()
        self.assertIsNone(m.locked("G"))
        m.rotate_master("G")
        with self.assertRaises(Rejected):
            m.use_graph_key("G")
        m.use_global_key("G")
        self.assertFalse(os.path.exists(self.cfg("G", "master.key")))
        self.assertIsNone(self.fresh().locked("G"))

    def test_reset_is_for_chat_history_only(self):
        self.m.keys("G")
        with self.assertRaises(Rejected):
            self.m.reset("G")
    def test_delete_memory(self):
        for g in ("Live", "Gone"):
            os.makedirs(os.path.dirname(self.memory_path(g)), exist_ok=True)
            sqlite3.connect(self.memory_path(g)).close()
            self.m.keys(g)
        st = {t["target"]: t["removed"] for t in self.m.status(None, ["Live"])["targets"]}
        self.assertEqual((st["Live"], st["Gone"], st["chat_history"]), (False, True, None))
        self.assertIsNone(self.m.status()["targets"][0]["removed"])     # not checked
        key = self.dk("Live").active
        self.m.delete_memory("Live", ["Live"], actor="admin")             # fresh start, keys kept
        self.assertFalse(os.path.exists(self.memory_path("Live")))
        self.assertEqual(self.dk("Live").active, key)
        self.m.delete_memory("Gone", ["Live"], actor="admin")             # removed graph: keys too
        self.assertFalse(os.path.exists(self.cfg("Gone", "data_key.json")))
        self.assertNotIn("Gone", [t["target"] for t in self.m.status()["targets"]])
        self.assertEqual([a["keys_deleted"] for a in self.audit if a["action"] == "delete_memory"],
                         [False, True])

    def test_delete_memory_of_locked_graph_starts_fresh(self):
        self.m.keys("G")
        dk = self.dk("G")
        dk.keys[0].master_id = "0" * 16
        dk.save(self.cfg("G", "data_key.json"))
        m = self.fresh()
        self.assertEqual(m.locked("G").reason, "wrong_key")
        m.delete_memory("G", ["G"], actor="admin")
        self.assertIsNone(m.locked("G"))

    def test_status_and_audit_never_contain_a_key(self):
        self.m.keys("G")
        self.m.rotate_master(actor="admin")
        key = KeyRing.load(self.cfg("master.key")).to_json()["versions"][0]["key"]
        self.assertNotIn(key, json.dumps(self.m.status()) + json.dumps(self.m.status("G")))
        self.assertNotIn(key, json.dumps(self.audit))
        self.assertTrue(all(a["user"] for a in self.audit))


class TestMountedKey(_Env):
    def test_never_generated_or_written(self):
        path = self.mount("")
        os.unlink(path)
        m = self.fresh()
        self.assertEqual(m.locked("G").reason, "master_key_missing")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(self.cfg("master.key")))

    def test_rotate_with_same_key_does_nothing(self):
        self.mount(base64.b64encode(new_key()).decode())
        m = self.fresh()
        m.keys("G")
        self.assertEqual(m.rotate_master()["result"], "unchanged")

    def test_new_key_while_running_needs_no_previous_key(self):
        path = self.mount(base64.b64encode(new_key()).decode())
        m = self.fresh()
        blob = m.keys("G").encrypt(b"x")
        new = new_key()
        with open(path, "w") as f:
            f.write(base64.b64encode(new).decode())      # the secret now has only the new key
        self.assertEqual(m.rotate_master(actor="admin")["result"], "rotated")
        self.assertEqual(self.fresh().keys("G").decrypt(blob), b"x")   # a restart works

    def test_new_key_after_restart_needs_previous_key(self):
        old = new_key()
        path = self.mount(base64.b64encode(old).decode())
        m = self.fresh()
        blob = m.keys("G").encrypt(b"x")
        with open(path, "w") as f:
            f.write(base64.b64encode(new_key()).decode())
        m = self.fresh()                                 # restarted: old key not in memory
        self.assertEqual(m.locked("G").reason, "wrong_key")
        with self.assertRaises(NeedsPreviousKey) as cm:
            m.rotate_master()
        self.assertEqual(cm.exception.needed, [KeyRing.parse(base64.b64encode(old).decode()).active])
        with self.assertRaises(Rejected):
            m.rotate_master(previous_key=base64.b64encode(new_key()).decode())
        # Upload key on the locked data does the same with the previous key.
        m.upload_key(base64.b64encode(old).decode(), actor="admin")
        self.assertEqual(self.fresh().keys("G").decrypt(blob), b"x")
        self.assertFalse(os.path.exists(self.cfg("master.key")))      # previous key not stored

    def test_secret_with_both_keys(self):
        old = new_key()
        path = self.mount(base64.b64encode(old).decode())
        blob = self.fresh().keys("G").encrypt(b"x")
        with open(path, "w") as f:
            f.write(base64.b64encode(new_key()).decode() + "\n" + base64.b64encode(old).decode())
        m = self.fresh()
        self.assertIsNone(m.locked("G"))
        self.assertTrue(m.status()["key"]["rotation_pending"])
        m.rotate_master()
        self.assertFalse(m.status()["key"]["rotation_pending"])
        self.assertEqual(m.keys("G").decrypt(blob), b"x")

    def test_mounted_key_cannot_be_downloaded_or_rotated_by_upload(self):
        self.mount(base64.b64encode(new_key()).decode())
        m = self.fresh()
        m.keys("G")
        with self.assertRaises(Rejected):
            m.download()


def _plain_memory(path: str):
    """A memory file written before encryption."""
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    saver = SqliteSaver(conn)
    saver.setup()
    cp = empty_checkpoint()
    cp["channel_values"] = {"note": "PLAINTEXT-CHECKPOINT"}
    cfg = saver.put({"configurable": {"thread_id": "G:u:c", "checkpoint_ns": ""}}, cp, {}, {})
    saver.put_writes(cfg, [("note", "PLAINTEXT-WRITE")], "task1")
    st = SqliteStore(sqlite3.connect(path, check_same_thread=False, isolation_level=None))
    st.setup()
    st.put(("deep_agent", "G", "u", "memories"), "/AGENTS.md", {"content": "PLAINTEXT-MEMORY"})
    conn.close()


class TestMemory(_Env):
    def _open(self, g="G"):
        from deep_agent import memory as mem
        from deep_agent.settings import MemorySettings
        mem.reset_backends_for_tests(None)
        mem.close_backends()
        return mem, mem.init_backends(MemorySettings("sqlite", self.memory_path(g)), g)

    def tearDown(self):
        from deep_agent import memory as mem
        mem.close_backends()
        super().tearDown()

    def raw(self, g="G") -> bytes:
        conn = sqlite3.connect(self.memory_path(g))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.close()
        with open(self.memory_path(g), "rb") as f:
            return f.read()

    def test_existing_rows_are_encrypted_and_readable(self):
        _plain_memory(self.memory_path("G"))
        mem, b = self._open()           # first use: data key created, job runs
        self.assertFalse(self.m._has_plain_memory("G"))
        raw = self.raw()
        for text in (b"PLAINTEXT-CHECKPOINT", b"PLAINTEXT-WRITE", b"PLAINTEXT-MEMORY"):
            self.assertNotIn(text, raw)
        self.assertEqual(mem.read_memory("G", "u", "AGENTS.md"), "PLAINTEXT-MEMORY")
        tup = b.checkpointer.get_tuple({"configurable": {"thread_id": "G:u:c"}})
        self.assertEqual(tup.checkpoint["channel_values"]["note"], "PLAINTEXT-CHECKPOINT")
        self.assertEqual(tup.pending_writes[0][2], "PLAINTEXT-WRITE")
        self.assertEqual([t["conversation_id"] for t in mem.list_threads("G", "u")], ["c"])

    def test_new_writes_are_encrypted(self):
        mem, b = self._open()
        mem.write_memory("G", "u", "prefs.md", "NEW-SECRET-MEMORY")
        self.assertEqual(mem.read_memory("G", "u", "prefs.md"), "NEW-SECRET-MEMORY")
        self.assertNotIn(b"NEW-SECRET-MEMORY", self.raw())

    def test_data_key_rotation_reencrypts_rows(self):
        _plain_memory(self.memory_path("G"))
        mem, b = self._open()
        old = self.m.keys("G").active
        self.m.rotate_data_key("G", actor="admin")      # runs the job here
        new = self.m.keys("G").active
        self.assertNotEqual(old, new)
        self.assertEqual([e.id for e in self.dk("G").keys], [new])
        conn = sqlite3.connect(self.memory_path("G"))
        ids = {value_key_id(r[0]) for r in conn.execute("SELECT checkpoint FROM checkpoints")}
        self.assertEqual(ids, {new})
        self.assertEqual(mem.read_memory("G", "u", "AGENTS.md"), "PLAINTEXT-MEMORY")

    def test_unfinished_data_key_rotation_resumes_after_restart(self):
        _plain_memory(self.memory_path("G"))
        mem, b = self._open()
        self.m.schedule = lambda t: None                # the job does not run: a stop
        self.m.rotate_data_key("G")
        self.assertEqual(len(self.dk("G").keys), 2)
        mem.close_backends()
        m = self.fresh()
        m.startup()
        self.assertEqual(len(self.dk("G").keys), 1)

    def test_locked_graph_raises_and_opens_after_upload(self):
        mem, b = self._open()
        mem.write_memory("G", "u", "a.md", "x")
        saved = open(self.cfg("master.key")).read()
        mem.close_backends()
        os.unlink(self.cfg("master.key"))
        self.fresh()
        from deep_agent.settings import MemorySettings
        with self.assertRaises(Locked):
            mem.init_backends(MemorySettings("sqlite", self.memory_path("G")), "G")
        self.assertNotIn("G", mem._backends)        # not cached, no in-memory fallback
        self.m.upload_key(saved)
        mem.init_backends(MemorySettings("sqlite", self.memory_path("G")), "G")
        self.assertEqual(mem.read_memory("G", "u", "a.md"), "x")

    def test_encrypted_data_without_data_key_is_locked(self):
        from langgraph.checkpoint.base import empty_checkpoint
        mem, b = self._open()
        b.checkpointer.put({"configurable": {"thread_id": "G:u:c", "checkpoint_ns": ""}},
                           empty_checkpoint(), {}, {})
        mem.close_backends()
        os.unlink(self.cfg("G", "data_key.json"))
        self.assertEqual(self.fresh().locked("G").reason, "data_key_missing")


class _FakeChatHistory:
    def __init__(self, messages):
        self.messages = messages        # id -> dict

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/messages":
            after = int(request.url.params.get("after", 0))
            limit = int(request.url.params.get("limit", 500))
            rows = [dict(id=i, **m) for i, m in sorted(self.messages.items()) if i > after][:limit]
            return httpx.Response(200, json=rows)
        if request.method == "POST" and request.url.path == "/messages":
            for u in json.loads(request.content):
                for m in self.messages.values():
                    if m["message_id"] == u["message_id"]:
                        m["content"], m["comment"] = u["content"], u["comment"]
            return httpx.Response(200, json={})
        if request.method == "POST" and request.url.path == "/messages/delete":
            ids = set(json.loads(request.content)["message_ids"])
            self.messages = {i: m for i, m in self.messages.items() if m["message_id"] not in ids}
            return httpx.Response(200, json={})
        return httpx.Response(404)


class TestChatHistory(_Env):
    def setUp(self):
        super().setUp()
        self.fake = _FakeChatHistory({
            1: {"message_id": "m1", "content": "hello PLAIN", "comment": ""},
            2: {"message_id": "m2", "content": "answer PLAIN", "comment": "good PLAIN"},
        })
        transport = httpx.MockTransport(self.fake.handler)
        real_client = httpx.Client
        for p in (mock.patch.object(chat_crypto, "_service", return_value=("http://ch", {})),
                  mock.patch.object(chat_crypto.httpx, "Client",
                                    lambda **kw: real_client(transport=transport, **kw))):
            p.start()
            self.addCleanup(p.stop)

    def test_message_round_trip(self):
        msg = {"message_id": "m", "content": "secret question", "comment": "", "role": "user"}
        enc = chat_crypto.encrypt_message(self.m, msg)
        self.assertTrue(is_encrypted_text(enc["content"]))
        self.assertEqual((enc["comment"], enc["role"]), ("", "user"))
        self.assertEqual(chat_crypto.decrypt_messages(self.m, [enc])[0]["content"], "secret question")

    def test_existing_messages_are_encrypted_then_rotated(self):
        self.m.keys(CHAT_TARGET)                    # first use runs the job
        for m in self.fake.messages.values():
            self.assertNotIn("PLAIN", m["content"] + m["comment"])
        rows = chat_crypto.decrypt_messages(self.m, [dict(m) for m in self.fake.messages.values()])
        self.assertEqual(rows[1]["comment"], "good PLAIN")
        self.m.rotate_data_key(CHAT_TARGET)
        new = self.m.keys(CHAT_TARGET).active
        for m in self.fake.messages.values():
            self.assertEqual(value_key_id(text_blob(m["content"])), new)

    def test_reset_deletes_only_encrypted_messages(self):
        self.m.keys(CHAT_TARGET)
        self.fake.messages[3] = {"message_id": "m3", "content": "", "comment": ""}
        dk = self.dk(CHAT_TARGET)
        dk.keys[0].master_id = "0" * 16
        dk.save(self.cfg("chat_history.data_key.json"))
        m = self.fresh()
        self.assertIsNotNone(m.locked(CHAT_TARGET))
        m.reset(CHAT_TARGET)
        self.assertEqual([x["message_id"] for x in self.fake.messages.values()], ["m3"])
        self.assertIsNone(m.locked(CHAT_TARGET))


if __name__ == "__main__":
    unittest.main()
