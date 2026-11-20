# Copyright (c) 2026 TigerGraph, Inc.
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

"""Kept document files: paths, document ids, checksums, states, and the
loading jobs that store the checksum."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from common.utils import graph_docs as gd
from common.utils.text_extractors import TextExtractor

G = "Docs"


class _InTempDir:
    def __enter__(self):
        self.prev = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="graph_docs_test_")
        os.chdir(self.tmp)
        return Path(self.tmp)

    def __exit__(self, *exc):
        os.chdir(self.prev)


def _keep(source, name, content=None, records=None):
    """Write a kept file and, if ``content`` or ``records`` is given, its
    converted JSONL. The file's bytes are ``content`` (or "x"), so the file's
    checksum is ``gd.content_checksum(content)``."""
    path = Path(gd.source_dir(G, source)) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((content if content is not None else "x").encode("utf-8"))
    if content is not None or records is not None:
        conv = Path(gd.converted_dir(G, source)) / gd.converted_name(name)
        conv.parent.mkdir(parents=True, exist_ok=True)
        recs = records or [{"doc_id": gd.doc_id_text(name), "doc_type": "markdown", "content": content}]
        with open(conv, "w") as f:
            for r in gd.add_checksums(recs, gd.file_checksum(str(path))):
                f.write(json.dumps(r) + "\n")


class TestPathsAndIds(unittest.TestCase):
    def test_files_are_kept_under_configs_graph_docs(self):
        self.assertEqual(gd.source_dir(G, "uploads"), os.path.join("configs", "graph_docs", "uploads", G))
        self.assertEqual(gd.source_dir(G, "downloads"), os.path.join("configs", "graph_docs", "downloads", G))
        self.assertEqual(
            gd.converted_dir(G, "uploads"),
            os.path.join("configs", "graph_docs", "uploads", "ingestion_temp", G),
        )

    def test_unknown_source_and_unsafe_names_are_refused(self):
        with self.assertRaises(ValueError):
            gd.source_dir(G, "../configs")
        for bad in ("", ".", "..", ".env"):
            with self.assertRaises(ValueError):
                gd.safe_file_name(bad)
        self.assertEqual(gd.safe_file_name("../../server_config.json"), "server_config.json")

    def test_document_id_keeps_the_extension(self):
        self.assertEqual(gd.doc_id("Notes.MD"), "notes.md")
        self.assertEqual(gd.doc_id("report.pdf"), "report.pdf")
        self.assertNotEqual(gd.doc_id("notes.md"), gd.doc_id("notes.pdf"))
        self.assertEqual(gd.legacy_doc_id("Report.pdf"), "report")

    def test_converted_name_uses_the_full_file_name(self):
        self.assertEqual(gd.converted_name("notes.md"), "notes.md.jsonl")
        self.assertEqual(gd.converted_name("batch.jsonl"), "batch.jsonl")

    def test_converted_records_carry_the_source_file_checksum(self):
        recs = gd.add_checksums([
            {"doc_id": "a.pdf", "content": "hello"},
            {"doc_id": "a.pdf_image_1", "image_data": "..."},
        ], "filehash")
        self.assertEqual(recs[0]["source_checksum"], "filehash")
        self.assertEqual(recs[1]["source_checksum"], "")
        # Without a file (a .jsonl record), the checksum is of the content.
        self.assertEqual(gd.add_checksums([{"content": "hello"}])[0]["source_checksum"],
                         gd.content_checksum("hello"))


class TestConverter(unittest.TestCase):
    def test_converted_file_has_the_id_with_extension_and_a_checksum(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            Path(src, "notes.md").write_text("# Notes\n\nBody text.")
            Path(src, "notes.txt").write_text("Other text.")
            asyncio.run(TextExtractor()._process_folder_async(src, G, out))
            self.assertTrue(Path(out, "notes.md.jsonl").exists())
            self.assertTrue(Path(out, "notes.txt.jsonl").exists())
            rec = json.loads(Path(out, "notes.md.jsonl").read_text().splitlines()[0])
            self.assertEqual(rec["doc_id"], "notes.md")
            self.assertEqual(rec["source_checksum"], gd.file_checksum(str(Path(src, "notes.md"))))

    def test_jsonl_source_gets_a_checksum_when_copied(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            Path(src, "batch.jsonl").write_text(json.dumps({"doc_id": "d1", "doc_type": "", "content": "c"}) + "\n")
            asyncio.run(TextExtractor()._process_folder_async(src, G, out))
            rec = json.loads(Path(out, "batch.jsonl").read_text().splitlines()[0])
            self.assertEqual(rec["source_checksum"], gd.content_checksum("c"))

    def test_only_listed_files_are_converted(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            Path(src, "a.txt").write_text("a")
            Path(src, "b.txt").write_text("b")
            TextExtractor().process_folder(src, G, out, filenames=["b.txt"])
            self.assertFalse(Path(out, "a.txt.jsonl").exists())
            self.assertTrue(Path(out, "b.txt.jsonl").exists())


class TestFoldersAndSubfolders(unittest.TestCase):
    """Source folders besides uploads and downloads, and their subfolders."""

    def test_other_folders_are_sources_and_subfolders_are_listed(self):
        with _InTempDir():
            _keep("uploads", "a.md")
            _keep("server", "reports/2023/q1.pdf")
            os.makedirs(os.path.join(gd.DOCS_ROOT, "server", "ingestion_temp", G), exist_ok=True)
            self.assertEqual(gd.all_sources(), ["uploads", "downloads", "server"])
            files = {(f.source, f.name): f for f in gd.list_kept_files(G)}
            self.assertEqual(set(files), {("uploads", "a.md"), ("server", "reports/2023/q1.pdf")})
            # The document id is the path under the graph's folder, with __.
            self.assertEqual(files[("server", "reports/2023/q1.pdf")].doc_ids, ["reports__2023__q1.pdf"])
            self.assertEqual(files[("uploads", "a.md")].doc_ids, ["a.md"])

    def test_same_file_name_in_two_subfolders_does_not_conflict(self):
        with _InTempDir():
            _keep("server", "2023/q1.pdf")
            _keep("server", "2024/q1.pdf")
            files = {f.name: f for f in gd.assign_states(gd.list_kept_files(G), {}, True)}
            self.assertEqual(files["2023/q1.pdf"].state, gd.NOT_INGESTED)
            self.assertEqual(files["2024/q1.pdf"].doc_ids, ["2024__q1.pdf"])

    def test_same_path_in_two_sources_conflicts(self):
        with _InTempDir():
            _keep("server", "2023/q1.pdf")
            _keep("downloads", "2023/Q1.pdf")
            files = {(f.source, f.name): f for f in gd.assign_states(gd.list_kept_files(G), {}, True)}
            self.assertEqual(files[("server", "2023/q1.pdf")].state, gd.CONFLICT)
            self.assertEqual(files[("server", "2023/q1.pdf")].conflict_with, ["2023/Q1.pdf"])

    def test_a_subfolder_file_is_never_an_older_version(self):
        # Earlier releases added files at the top of the folder only, so a
        # Document named after the file stem belongs to another file.
        with _InTempDir():
            _keep("server", "2023/q1.pdf")
            files = gd.assign_states(gd.list_kept_files(G), {"q1": {"outdated": False}}, True)
            self.assertEqual(files[0].state, gd.NOT_INGESTED)

    def test_rename_keeps_the_file_in_its_subfolder(self):
        with _InTempDir():
            _keep("server", "2023/q1.pdf", records=[
                {"doc_id": "2023__q1.pdf", "doc_type": "markdown", "content": "a"}])
            _keep("downloads", "2023/Q1.pdf")
            self.assertEqual(gd.rename_kept_file(G, "server", "2023/q1.pdf", "q1-2023.pdf"), "2023/q1-2023.pdf")
            self.assertTrue(os.path.isfile(gd.kept_file_path(G, "server", "2023/q1-2023.pdf")))
            with open(gd.converted_path(G, "server", "2023/q1-2023.pdf")) as f:
                self.assertEqual(json.loads(f.readline())["doc_id"], "2023__q1-2023.pdf")
            with self.assertRaises(gd.RenameError):
                gd.rename_kept_file(G, "server", "2024/q1.pdf", "other/q1.pdf")

    def test_cloud_objects_keep_their_path_below_the_prefix(self):
        self.assertEqual(gd.cloud_rel_path("eval/reports/2023/q1.pdf", "eval/"), "reports/2023/q1.pdf")
        self.assertEqual(gd.cloud_rel_path("eval/reports/q1.pdf", "eval/rep"), "reports/q1.pdf")
        self.assertEqual(gd.cloud_rel_path("eval/q1.pdf", "eval"), "eval/q1.pdf")
        self.assertEqual(gd.cloud_rel_path("q1.pdf", ""), "q1.pdf")
        for bad in ("eval/../x.pdf", "eval/.hidden/x.pdf", "/abs.pdf"):
            self.assertIsNone(gd.cloud_rel_path(bad, ""), bad)

    def test_paths_and_source_names_cannot_leave_the_folder(self):
        for bad in ("../x.md", "/etc/passwd", "a/../../x.md", ".hidden/x.md"):
            with self.assertRaises(ValueError, msg=bad):
                gd.kept_file_path(G, "server", bad)
        for bad in ("..", "../uploads", "ingestion_temp", "a/b", ""):
            with self.assertRaises(ValueError, msg=bad):
                gd.source_dir(G, bad)

    def test_converter_keeps_subfolders_and_ids_the_path(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            for year in ("2023", "2024"):
                Path(src, year).mkdir()
                Path(src, year, "notes.md").write_text(f"# Notes {year}\n\nBody.")
            TextExtractor().process_folder(src, G, out, filenames=["2024/notes.md"])
            self.assertFalse(Path(out, "2023", "notes.md.jsonl").exists())
            rec = json.loads(Path(out, "2024", "notes.md.jsonl").read_text().splitlines()[0])
            self.assertEqual(rec["doc_id"], "2024__notes.md")


class TestStates(unittest.TestCase):
    def _states(self, graph, has_checksum=True):
        files = gd.list_kept_files(G)
        gd.assign_states(files, graph, has_checksum)
        return {(f.source, f.name): f for f in files}

    def test_each_state(self):
        with _InTempDir():
            _keep("uploads", "new.md", "new")
            _keep("uploads", "same.md", "same")
            _keep("uploads", "edited.md", "edited v2")
            _keep("uploads", "old.md", "old")
            _keep("downloads", "plain.html", "plain")
            _keep("uploads", "raw.pdf")  # not converted yet; its bytes are "x"
            _keep("uploads", "copy.pdf")  # not converted yet
            graph = {
                "same.md": {"source_checksum": gd.content_checksum("same"), "outdated": False},
                "edited.md": {"source_checksum": gd.content_checksum("edited v1"), "outdated": False},
                "old.md": {"source_checksum": "", "outdated": False},
                "plain": {"source_checksum": "", "outdated": False},
                "raw.pdf": {"source_checksum": "abc", "outdated": False},
                "copy.pdf": {"source_checksum": gd.content_checksum("x"), "outdated": False},
            }
            st = self._states(graph)
            self.assertEqual(st[("uploads", "new.md")].state, gd.NOT_INGESTED)
            self.assertEqual(st[("uploads", "same.md")].state, gd.INGESTED)
            self.assertEqual(st[("uploads", "edited.md")].state, gd.CHANGED)
            self.assertEqual(st[("uploads", "old.md")].state, gd.NOT_VERIFIED)
            self.assertEqual(st[("downloads", "plain.html")].state, gd.OLDER_VERSION)
            self.assertEqual(st[("downloads", "plain.html")].legacy_id, "plain")
            # The state of a file that is not converted is known from its bytes.
            self.assertEqual(st[("uploads", "raw.pdf")].state, gd.CHANGED)
            self.assertEqual(st[("uploads", "copy.pdf")].state, gd.INGESTED)
            self.assertFalse(st[("uploads", "copy.pdf")].converted)

    def test_outdated_older_version_is_not_offered_again(self):
        with _InTempDir():
            _keep("uploads", "plain.html", "plain")
            graph = {
                "plain": {"source_checksum": "", "outdated": True},
                "plain.html": {"source_checksum": gd.content_checksum("plain"), "outdated": False},
            }
            self.assertEqual(self._states(graph)[("uploads", "plain.html")].state, gd.INGESTED)

    def test_without_the_checksum_schema_an_existing_document_is_not_verified(self):
        with _InTempDir():
            _keep("uploads", "same.md", "same")
            graph = {"same.md": {"source_checksum": "", "outdated": False}}
            self.assertEqual(self._states(graph, has_checksum=False)[("uploads", "same.md")].state, gd.NOT_VERIFIED)

    def test_files_with_the_same_document_id_conflict(self):
        with _InTempDir():
            _keep("uploads", "Report.pdf", "a")
            _keep("downloads", "report.pdf", "b")
            _keep("uploads", "notes.md", "n")
            st = self._states({})
            self.assertEqual(st[("uploads", "Report.pdf")].state, gd.CONFLICT)
            self.assertEqual(st[("uploads", "Report.pdf")].conflict_with, ["report.pdf"])
            self.assertEqual(st[("downloads", "report.pdf")].state, gd.CONFLICT)
            self.assertEqual(st[("uploads", "notes.md")].state, gd.NOT_INGESTED)
            counts = gd.state_counts(st.values())
            self.assertEqual(counts[gd.CONFLICT], 2)
            self.assertEqual(counts[gd.NOT_INGESTED], 1)

    def test_upload_check_compares_document_ids(self):
        with _InTempDir():
            _keep("uploads", "report.pdf", "a")
            _keep("downloads", "notes.md", "n")
            conflicts = gd.upload_id_conflicts(G, ["Report.pdf", "report.pdf", "Notes.md", "new.md"])
            self.assertEqual(conflicts["Report.pdf"], ["report.pdf"])
            self.assertNotIn("report.pdf", conflicts)  # same name replaces the kept upload
            self.assertEqual(conflicts["Notes.md"], ["notes.md"])
            self.assertNotIn("new.md", conflicts)


class TestRename(unittest.TestCase):
    """Rename of a kept file in conflict."""

    def test_rename_moves_the_file_and_updates_the_converted_ids(self):
        with _InTempDir():
            records = [
                {"doc_id": "Report.pdf", "doc_type": "markdown",
                 "content": "Intro ![a](tg://report.pdf_image_1) end"},
                {"doc_id": "report.pdf_image_1", "doc_type": "image",
                 "parent_doc": "Report.pdf", "image_data": "x"},
            ]
            _keep("downloads", "Report.pdf", records=records)
            _keep("uploads", "report.pdf", "other")
            self.assertEqual(gd.rename_kept_file(G, "downloads", "Report.pdf", "report-2025.pdf"), "report-2025.pdf")
            self.assertFalse(os.path.exists(gd.kept_file_path(G, "downloads", "Report.pdf")))
            self.assertTrue(os.path.isfile(gd.kept_file_path(G, "downloads", "report-2025.pdf")))
            self.assertFalse(os.path.exists(gd.converted_path(G, "downloads", "Report.pdf")))
            with open(gd.converted_path(G, "downloads", "report-2025.pdf")) as f:
                text, image = [json.loads(line) for line in f]
            self.assertEqual(text["doc_id"], "report-2025.pdf")
            self.assertIn("tg://report-2025.pdf_image_1", text["content"])
            # Renaming does not change the file's bytes, so its checksum stays.
            self.assertEqual(text["source_checksum"], gd.content_checksum("x"))
            self.assertEqual(image["doc_id"], "report-2025.pdf_image_1")
            self.assertEqual(image["parent_doc"], "report-2025.pdf")
            files = gd.assign_states(gd.list_kept_files(G), {}, True)
            self.assertEqual({f.state for f in files}, {gd.NOT_INGESTED})

    def test_rename_refuses_names_that_do_not_resolve_the_conflict(self):
        with _InTempDir():
            _keep("downloads", "Notes.md", "a")
            _keep("uploads", "notes.md", "b")
            _keep("uploads", "plan.md", "c")
            for new in ("NOTES.md", "notes2.txt", "plan.md", "Plan.md", "", ".hidden.md"):
                with self.assertRaises(gd.RenameError, msg=new):
                    gd.rename_kept_file(G, "downloads", "Notes.md", new)
            self.assertTrue(os.path.isfile(gd.kept_file_path(G, "downloads", "Notes.md")))

    def test_jsonl_file_cannot_be_renamed(self):
        with _InTempDir():
            _keep("downloads", "a.jsonl")
            with self.assertRaises(gd.RenameError):
                gd.rename_kept_file(G, "downloads", "a.jsonl", "b.jsonl")


class TestLoadingJobs(unittest.TestCase):
    def setUp(self):
        import common
        from supportai import supportai
        self.sa = supportai
        # The GSQL files are read relative to the folder that holds common/.
        self.prev = os.getcwd()
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(common.__file__))))
        with open("common/gsql/supportai/SupportAI_InitialLoadJSON_WithImages.gsql") as f:
            self.with_images = f.read()
        with open("common/gsql/supportai/SupportAI_InitialLoadJSON.gsql") as f:
            self.plain = f.read()

    def tearDown(self):
        os.chdir(self.prev)

    NEW = ["epoch_added", "epoch_processing", "epoch_processed", "source_checksum", "outdated"]
    OLD = ["epoch_added", "epoch_processing", "epoch_processed"]
    # Set up with an earlier 2.1.0 build: the old attribute stays, the new one
    # is added at the end.
    DEV = ["epoch_added", "epoch_processing", "epoch_processed", "content_checksum", "outdated",
           "source_checksum"]

    def test_converted_files_job_loads_the_checksum_where_the_graph_has_it(self):
        job = self.sa.document_loading_job(self.with_images, self.NEW)
        self.assertIn('gsql_current_time_epoch(0), _, _, $"source_checksum", _)', job)
        job = self.sa.document_loading_job(self.with_images, self.DEV)
        self.assertIn('gsql_current_time_epoch(0), _, _, _, _, $"source_checksum")', job)

    def test_other_jobs_get_empty_values(self):
        job = self.sa.document_loading_job(self.plain, self.NEW)
        self.assertIn("gsql_current_time_epoch(0), _, _, _, _)", job)
        self.assertNotIn("source_checksum", job)

    def test_graph_without_the_schema_gets_four_document_values(self):
        for text in (self.with_images, self.plain):
            job = self.sa.document_loading_job(text, self.OLD)
            self.assertIn("gsql_current_time_epoch(0), _, _)", job)
            self.assertNotIn("source_checksum", job)

    def test_an_installed_job_is_replaced_when_it_does_not_fit(self):
        def ls(values):
            return ("- CREATE LOADING JOB j {\n LOAD f TO VERTEX Document VALUES(gsql_lower($\"doc_id\"), "
                    f"{values}) WHERE $\"content\" != \"\" USING JSON_FILE=\"true\"; }}")
        four = self.sa._loading_job_text(ls('gsql_current_time_epoch("0"), _, _'), "j")
        self.assertTrue(self.sa._loading_job_fits(four, self.OLD, wants_checksum=True))
        self.assertFalse(self.sa._loading_job_fits(four, self.NEW, wants_checksum=True))
        old = self.sa._loading_job_text(ls('gsql_current_time_epoch("0"), _, _, $"content_checksum", _'), "j")
        self.assertFalse(self.sa._loading_job_fits(old, self.NEW, wants_checksum=True))
        new = self.sa._loading_job_text(ls('gsql_current_time_epoch("0"), _, _, $"source_checksum", _'), "j")
        self.assertTrue(self.sa._loading_job_fits(new, self.NEW, wants_checksum=True))
        self.assertIsNone(self.sa._loading_job_text(ls("_"), "other"))

    def test_schema_job_adds_what_the_graph_lacks(self):
        gsql = self.sa.document_versions_schema_gsql(G, self.OLD, False)
        self.assertIn("ALTER VERTEX Document ADD ATTRIBUTE (source_checksum STRING, outdated BOOL)", gsql)
        self.assertIn("ADD DIRECTED EDGE SUPERSEDED_BY(FROM Document, TO Document, epoch_added UINT)", gsql)
        self.assertTrue(gsql.rstrip().endswith("DROP JOB add_document_versions"))
        # A graph set up with the old checksum only gets the new attribute.
        dev = self.sa.document_versions_schema_gsql(G, self.OLD + ["content_checksum", "outdated"], True)
        self.assertIn("ALTER VERTEX Document ADD ATTRIBUTE (source_checksum STRING);", dev)
        self.assertNotIn("ADD DIRECTED EDGE", dev)
        self.assertIsNone(self.sa.document_versions_schema_gsql(G, self.NEW, True))


def _schema(with_versions: bool) -> dict:
    attrs = [{"AttributeName": "epoch_added"}]
    edges = [{"Name": "HAS_CONTENT"}]
    if with_versions:
        attrs += [{"AttributeName": "source_checksum"}, {"AttributeName": "outdated"}]
        edges.append({"Name": "SUPERSEDED_BY"})
    return {"VertexTypes": [{"Name": "Document", "Attributes": attrs}], "EdgeTypes": edges}


class TestDocumentRoutes(unittest.TestCase):
    """The Stored Files routes: who can use them, and what an ingest loads."""

    @classmethod
    def setUpClass(cls):
        import sys
        try:
            from fastapi.testclient import TestClient
            from app.main import app
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        sys.modules.setdefault("app.routers.ui", sys.modules["routers.ui"])
        cls.client = TestClient(app)

    def _conn(self, docs=None, with_versions=True):
        conn = MagicMock()
        conn.getSchema.return_value = _schema(with_versions)
        conn.getVertices.return_value = [
            {"v_id": k, "attributes": v} for k, v in (docs or {}).items()
        ]
        return conn

    def _call(self, method, url, roles, conn=None, **kw):
        p = patch
        with p("app.routers.ui.auth", return_value=([G], MagicMock())), \
             p("app.routers.ui._get_user_role_details", return_value=roles), \
             p("app.routers.ui.get_db_connection_pwd_manual", return_value=conn or self._conn()):
            return getattr(self.client, method)(url, auth=("u", "p"), **kw)

    def test_only_admins_of_the_graph_can_use_document_routes(self):
        with _InTempDir():
            for roles, code in (
                (([], {"Other": ["admin"]}, "u"), 403),
                (([], {G: ["designer"]}, "u"), 403),
                (([], {G: ["admin"]}, "u"), 200),
                ((["globaldesigner"], {}, "u"), 200),
                ((["superuser"], {}, "u"), 200),
            ):
                resp = self._call("get", f"/ui/{G}/documents", roles)
                self.assertEqual(resp.status_code, code, (roles, resp.text))
                resp = self._call("get", f"/ui/{G}/cloud/list", roles)
                self.assertEqual(resp.status_code, code, (roles, resp.text))

    def test_list_reports_states_and_counts(self):
        with _InTempDir():
            _keep("uploads", "new.md", "new")
            _keep("uploads", "same.md", "same")
            conn = self._conn({"same.md": {"source_checksum": gd.content_checksum("same"), "outdated": False}})
            body = self._call("get", f"/ui/{G}/documents", (["superuser"], {}, "u"), conn).json()
            self.assertEqual(body["total"], 2)
            self.assertTrue(body["versions_ready"])
            self.assertEqual(body["counts"]["not_ingested"], 1)
            self.assertEqual(body["counts"]["ingested"], 1)

    def test_ui_create_ingest_accepts_only_the_graph_folders(self):
        with _InTempDir():
            for path in ("configs", "/code/configs", "configs/graph_docs/uploads/Other"):
                resp = self._call(
                    "post", f"/ui/{G}/create_ingest", (["superuser"], {}, "u"),
                    json={"data_source": "server", "file_format": "multi",
                          "data_source_config": {"data_path": path}},
                )
                self.assertEqual(resp.status_code, 400, (path, resp.text))

    def test_ui_ingest_loads_only_the_graph_converted_files(self):
        with _InTempDir():
            resp = self._call(
                "post", f"/ui/{G}/ingest", (["superuser"], {}, "u"),
                json={"load_job_id": "load_documents_content_json_with_images",
                      "file_path": "in_temp_storage",
                      "data_source_id": {"data_source": "server", "data_path": "configs"}},
            )
            self.assertEqual(resp.status_code, 400, resp.text)

    def test_rename_route_renames_only_files_in_conflict(self):
        with _InTempDir():
            _keep("downloads", "Notes.md", "a")
            _keep("uploads", "notes.md", "b")
            _keep("uploads", "plan.md", "c")
            su = (["superuser"], {}, "u")
            url = f"/ui/{G}/documents/rename"
            resp = self._call("post", url, su, json={"source": "uploads", "name": "plan.md", "new_name": "plan2.md"})
            self.assertEqual(resp.status_code, 400, resp.text)
            resp = self._call("post", url, su, json={"source": "downloads", "name": "Notes.md", "new_name": "notes.txt"})
            self.assertEqual(resp.status_code, 400, resp.text)
            resp = self._call("post", url, ([], {G: ["designer"]}, "u"),
                              json={"source": "downloads", "name": "Notes.md", "new_name": "notes-cloud.md"})
            self.assertEqual(resp.status_code, 403, resp.text)
            resp = self._call("post", url, su, json={"source": "downloads", "name": "Notes.md", "new_name": "notes-cloud.md"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["name"], "notes-cloud.md")
            body = self._call("get", f"/ui/{G}/documents", su).json()
            self.assertEqual(body["counts"]["conflict"], 0)

    def test_conflict_messages_name_the_other_file(self):
        with _InTempDir():
            _keep("downloads", "Notes.md", "a")
            _keep("uploads", "notes.md", "b")
            su = (["superuser"], {}, "u")
            resp = self._call("post", f"/ui/{G}/uploads", su,
                              files=[("files", ("NOTES.md", b"c", "text/markdown"))])
            self.assertEqual(resp.status_code, 409, resp.text)
            self.assertIn("NOTES.md (same as notes.md, Notes.md)", resp.json()["detail"])
            body = self._call("post", f"/ui/{G}/documents/ingest", su, json={
                "files": [{"source": "downloads", "name": "Notes.md"}]}).json()
            self.assertEqual(body["skipped"][0]["reason"],
                             "Same name as notes.md when letter case is ignored. Rename or delete one of them.")

    def test_a_file_in_a_server_subfolder_lists_and_ingests(self):
        def fake_ingest(graphname, info, _conn):
            return {"ingested_files": [
                {"jsonl_file": f, "status": "success"} for f in info.data_source_id["files"]
            ]}
        with _InTempDir():
            _keep("server", "reports/2023/q1.md", "q1 text")
            su = (["superuser"], {}, "u")
            listed = self._call("get", f"/ui/{G}/documents", su).json()["files"]
            self.assertEqual([(f["source"], f["name"], f["doc_ids"]) for f in listed],
                             [("server", "reports/2023/q1.md", ["reports__2023__q1.md"])])
            with patch("app.routers.ui.supportai.ingest", side_effect=fake_ingest) as ing:
                body = self._call("post", f"/ui/{G}/documents/ingest", su, json={
                    "files": [{"source": "server", "name": "reports/2023/q1.md"}],
                    "override": False}).json()
            self.assertEqual([f["name"] for f in body["loaded"]], ["reports/2023/q1.md"])
            info = ing.call_args.args[1]
            self.assertEqual(info.data_source_id["files"], ["reports/2023/q1.md.jsonl"])
            self.assertTrue(info.data_source_id["data_path"].endswith(os.path.join("server", "ingestion_temp", G)))

    def test_folder_upload_keeps_subfolders_and_old_routes_follow(self):
        import io
        su = (["superuser"], {}, "u")
        with _InTempDir():
            resp = self._call("post", f"/ui/{G}/uploads", su, files=[
                ("files", ("corpus/2023/q1.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("corpus/2024/q1.md", io.BytesIO(b"b"), "text/markdown")),
                ("files", ("top.md", io.BytesIO(b"c"), "text/markdown")),
            ])
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(os.path.isfile(gd.kept_file_path(G, "uploads", "corpus/2023/q1.md")))
            names = sorted(f["filename"] for f in self._call("get", f"/ui/{G}/uploads/list", su).json()["files"])
            self.assertEqual(names, ["corpus/2023/q1.md", "corpus/2024/q1.md", "top.md"])
            listed = {f["name"]: f["doc_ids"] for f in self._call("get", f"/ui/{G}/documents", su).json()["files"]}
            self.assertEqual(listed["corpus/2024/q1.md"], ["corpus__2024__q1.md"])
            check = self._call("post", f"/ui/{G}/uploads/check", su, json={"filenames": ["corpus/2023/q1.md"]}).json()
            self.assertEqual(check["conflicts"], ["corpus/2023/q1.md"])
            resp = self._call("delete", f"/ui/{G}/uploads", su, params={"filename": "corpus/2023/q1.md"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertFalse(os.path.exists(gd.kept_file_path(G, "uploads", "corpus/2023/q1.md")))
            # The emptied subfolder goes too; its sibling stays.
            self.assertFalse(os.path.exists(os.path.join(gd.source_dir(G, "uploads"), "corpus", "2023")))
            self.assertTrue(os.path.isdir(os.path.join(gd.source_dir(G, "uploads"), "corpus", "2024")))
            resp = self._call("delete", f"/ui/{G}/uploads", su, params={"filename": "../../server_config.json"})
            self.assertEqual(resp.status_code, 400, resp.text)
            self._call("delete", f"/ui/{G}/uploads", su)
            self.assertEqual(gd.list_kept_files(G), [])

    def test_upload_refuses_a_name_that_leaves_the_folder(self):
        import io
        with _InTempDir():
            resp = self._call("post", f"/ui/{G}/uploads", (["superuser"], {}, "u"), files=[
                ("files", ("../escape.md", io.BytesIO(b"x"), "text/markdown"))])
            self.assertEqual(resp.status_code, 400, resp.text)

    def test_a_file_edited_on_disk_is_changed_and_converted_again(self):
        def fake_ingest(graphname, info, _conn):
            return {"ingested_files": [
                {"jsonl_file": f, "status": "success"} for f in info.data_source_id["files"]
            ]}
        with _InTempDir():
            _keep("uploads", "notes.md", "version 1")
            conn = self._conn({"notes.md": {"source_checksum": gd.content_checksum("version 1"),
                                            "outdated": False}})
            su = (["superuser"], {}, "u")
            listed = self._call("get", f"/ui/{G}/documents", su, conn).json()["files"]
            self.assertEqual((listed[0]["state"], listed[0]["converted"]), ("ingested", True))
            # The file changes on disk; its converted copy is from the old bytes.
            Path(gd.kept_file_path(G, "uploads", "notes.md")).write_text("version 2")
            listed = self._call("get", f"/ui/{G}/documents", su, conn).json()["files"]
            self.assertEqual((listed[0]["state"], listed[0]["converted"]), ("changed", False))
            with patch("app.routers.ui.supportai.ingest", side_effect=fake_ingest):
                body = self._call("post", f"/ui/{G}/documents/ingest", su, conn,
                                  json={"files": [{"source": "uploads", "name": "notes.md"}],
                                        "override": True}).json()
            self.assertEqual([f["name"] for f in body["loaded"]], ["notes.md"])
            with open(gd.converted_path(G, "uploads", "notes.md")) as f:
                rec = json.loads(f.readline())
            self.assertEqual(rec["source_checksum"], gd.content_checksum("version 2"))
            self.assertIn("version 2", rec["content"])

    def _ingest(self, conn, override):
        def fake_ingest(graphname, info, _conn):
            return {"ingested_files": [
                {"jsonl_file": f, "status": "success"} for f in info.data_source_id["files"]
            ]}
        with patch("app.routers.ui.supportai.ingest", side_effect=fake_ingest) as ing:
            resp = self._call(
                "post", f"/ui/{G}/documents/ingest", (["superuser"], {}, "u"), conn,
                json={"override": override, "files": [
                    {"source": "uploads", "name": n} for n in ("new.md", "same.md", "edited.md", "plain.html")
                ]},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json(), ing

    def _graph(self):
        return {
            "same.md": {"source_checksum": gd.content_checksum("same"), "outdated": False},
            "edited.md": {"source_checksum": gd.content_checksum("v1"), "outdated": False},
            "plain": {"source_checksum": "", "outdated": False},
        }

    def _keep_all(self):
        _keep("uploads", "new.md", "new")
        _keep("uploads", "same.md", "same")
        _keep("uploads", "edited.md", "v2")
        _keep("uploads", "plain.html", "plain")

    def test_ingest_without_override_loads_only_new_files(self):
        with _InTempDir():
            self._keep_all()
            conn = self._conn(self._graph())
            body, _ = self._ingest(conn, override=False)
            self.assertEqual([f["name"] for f in body["loaded"]], ["new.md"])
            self.assertEqual(
                sorted((f["name"], f["state"]) for f in body["skipped"]),
                [("edited.md", "changed"), ("plain.html", "older_version"), ("same.md", "ingested")],
            )
            conn.upsertVertex.assert_not_called()
            conn.upsertEdge.assert_not_called()

    def test_override_reloads_changed_and_supersedes_older_versions(self):
        with _InTempDir():
            self._keep_all()
            conn = self._conn(self._graph())
            body, _ = self._ingest(conn, override=True)
            self.assertEqual(
                sorted(f["name"] for f in body["loaded"]), ["edited.md", "new.md", "plain.html"]
            )
            self.assertEqual([f["name"] for f in body["skipped"]], ["same.md"])
            conn.upsertVertex.assert_any_call(
                "Document", "edited.md", {"epoch_processing": 0, "epoch_processed": 0}
            )
            conn.upsertVertex.assert_any_call("Document", "plain", {"outdated": True})
            args = conn.upsertEdge.call_args[0]
            self.assertEqual(args[:5], ("Document", "plain", "SUPERSEDED_BY", "Document", "plain.html"))

    def test_older_version_needs_the_schema_to_be_replaced(self):
        with _InTempDir():
            _keep("uploads", "plain.html", "plain")
            conn = self._conn({"plain": {}}, with_versions=False)
            def fake_ingest(graphname, info, _conn):
                return {"ingested_files": []}
            with patch("app.routers.ui.supportai.ingest", side_effect=fake_ingest):
                body = self._call(
                    "post", f"/ui/{G}/documents/ingest", (["superuser"], {}, "u"), conn,
                    json={"override": True, "files": [{"source": "uploads", "name": "plain.html"}]},
                ).json()
            self.assertEqual(body["loaded"], [])
            self.assertIn("Migration Assistant", body["skipped"][0]["reason"])

    def test_delete_removes_kept_and_converted_files_only(self):
        with _InTempDir():
            _keep("uploads", "a.md", "a")
            conn = self._conn()
            resp = self._call(
                "post", f"/ui/{G}/documents/delete", (["superuser"], {}, "u"), conn,
                json={"files": [{"source": "uploads", "name": "a.md"}]},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertFalse(os.path.exists(gd.kept_file_path(G, "uploads", "a.md")))
            self.assertFalse(os.path.exists(gd.converted_path(G, "uploads", "a.md")))
            conn.delVerticesById.assert_not_called()

    def test_upload_refuses_a_file_with_the_document_id_of_another_file(self):
        import io
        with _InTempDir():
            _keep("downloads", "report.pdf", "r")
            resp = self._call(
                "post", f"/ui/{G}/uploads", (["superuser"], {}, "u"),
                files=[("files", ("Report.pdf", io.BytesIO(b"x"), "application/pdf"))],
            )
            self.assertEqual(resp.status_code, 409, resp.text)
            self.assertIn("Report.pdf (same as report.pdf)", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
