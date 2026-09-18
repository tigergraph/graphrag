import ast
import os
import re
import unittest

_EXTRACTORS = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "common", "utils", "text_extractors.py"
    )
)


def _source():
    with open(_EXTRACTORS, encoding="utf-8") as handle:
        return handle.read()


class TestNoPlaceholderDocuments(unittest.TestCase):
    """A file that cannot be extracted must not become a document.

    Extraction failures used to return a document whose text was the error
    message (GML-2195). That placeholder reached the graph, was chunked and
    embedded, and became retrievable — indistinguishable to the agent from
    real document content — while the ingest was reported as successful.
    """

    def test_no_bracketed_placeholder_is_returned_as_content(self):
        """No extractor returns an error string as a document's content."""
        offenders = [
            line.strip()
            for line in _source().splitlines()
            if re.search(r'"content":\s*f?"\[', line)
        ]
        self.assertEqual(
            offenders,
            [],
            "extraction failures must raise, not return placeholder content",
        )

    def test_failure_paths_raise_extraction_error(self):
        """Each failure branch in the PDF/image extractors raises."""
        tree = ast.parse(_source())
        for name in (
            "_extract_pdf_with_images_as_docs",
            "_extract_standalone_image_as_doc",
        ):
            fn = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name
                ),
                None,
            )
            self.assertIsNotNone(fn, f"{name} not found")
            raises = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Raise)
                and isinstance(n.exc, ast.Call)
                and getattr(n.exc.func, "id", "") == "ExtractionError"
            ]
            self.assertTrue(raises, f"{name} never raises ExtractionError")

    def test_extraction_error_is_defined(self):
        """The error type exists and derives from Exception."""
        tree = ast.parse(_source())
        cls = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "ExtractionError"
            ),
            None,
        )
        self.assertIsNotNone(cls, "ExtractionError not defined")
        self.assertIn("Exception", [ast.unparse(b) for b in cls.bases])


class TestMessagesCarryNoInternals(unittest.TestCase):
    """Messages reach the caller in the per-file `error` field, so they must
    describe the file and the remedy — not exception text or paths."""

    def _messages(self):
        tree = ast.parse(_source())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and getattr(node.exc.func, "id", "") == "ExtractionError"
                and node.exc.args
            ):
                yield node.lineno, ast.unparse(node.exc.args[0])

    def test_no_raw_exception_interpolated(self):
        """`{e}` / `{import_err}` belong in the log line, not the message."""
        for lineno, msg in self._messages():
            for leak in ("{e}", "{str(e)}", "{import_err}", "{exc}"):
                self.assertNotIn(
                    leak, msg, f"exception text in message at line {lineno}: {msg}"
                )

    def test_no_full_path_interpolated(self):
        """Only the bare filename may appear, never the full path."""
        for lineno, msg in self._messages():
            self.assertNotIn(
                "{file_path}",
                msg,
                f"filesystem path in message at line {lineno}: {msg}",
            )


if __name__ == "__main__":
    unittest.main()
