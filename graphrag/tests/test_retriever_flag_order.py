import ast
import glob
import os
import unittest

_RETRIEVERS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "supportai", "retrievers")
)
_ROUTER = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "routers", "supportai.py")
)
# Flags that select behaviour rather than tune it; transposing two of these
# swaps the installed query being run, silently.
_FLAGS = ("withHyDE", "expand", "combine", "chunk_only", "doc_only")


def _parse(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _params(tree, name):
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name),
        None,
    )
    return [a.arg for a in fn.args.args if a.arg != "self"] if fn else None


def _flag_order(params):
    return [p for p in params or [] if p in _FLAGS and p != "combine"]


class TestRetrieverFlagOrderIsConsistent(unittest.TestCase):
    """A retriever's own two entry points must order their flags alike.

    ``SiblingRetriever.search`` declared ``expand`` before ``withHyDE`` while
    ``retrieve_answer`` — and both positional callers — passed the reverse, so
    every call transposed them (GML-2198). The flags pick different installed
    GSQL queries, so the swap changed which query ran, with no error.
    """

    def test_search_and_retrieve_answer_agree_within_each_retriever(self):
        for path in sorted(glob.glob(os.path.join(_RETRIEVERS, "*.py"))):
            name = os.path.basename(path)
            if name in ("__init__.py", "BaseRetriever.py"):
                continue
            tree = _parse(path)
            search = _flag_order(_params(tree, "search"))
            answer = _flag_order(_params(tree, "retrieve_answer"))
            with self.subTest(retriever=name):
                self.assertEqual(
                    search,
                    answer,
                    f"{name}: search{search} vs retrieve_answer{answer}",
                )

    def test_sibling_and_similarity_share_a_flag_order(self):
        """These two take the same pair; disagreeing is what caused the swap."""
        sibling = _flag_order(_params(_parse(os.path.join(_RETRIEVERS, "SiblingRetriever.py")), "search"))
        similarity = _flag_order(_params(_parse(os.path.join(_RETRIEVERS, "SimilarityRetriever.py")), "search"))
        self.assertEqual(sibling, similarity)
        self.assertEqual(sibling, ["withHyDE", "expand"])


class TestPositionalCallsMatchSignatures(unittest.TestCase):
    """Positional calls are what make a transposition possible at all."""

    def _self_call(self, tree, caller, callee):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == caller)
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == callee
            ):
                return [ast.unparse(a) for a in node.args]
        return None

    def test_each_retriever_calls_its_own_search_in_order(self):
        for path in sorted(glob.glob(os.path.join(_RETRIEVERS, "*.py"))):
            name = os.path.basename(path)
            if name in ("__init__.py", "BaseRetriever.py"):
                continue
            tree = _parse(path)
            passed = self._self_call(tree, "retrieve_answer", "search")
            signature = _params(tree, "search")
            if not passed or not signature:
                continue
            # Compare only the flag positions. Parameter names may legitimately
            # differ either side of the call (HybridRetriever passes `index`
            # into `indices`); a flag landing in another flag's slot may not.
            for position, param in enumerate(signature[: len(passed)]):
                if param not in _FLAGS:
                    continue
                with self.subTest(retriever=name, param=param):
                    self.assertEqual(
                        passed[position],
                        param,
                        f"{name}: {passed[position]!r} passed as {param!r}",
                    )

    def test_router_passes_sibling_flags_in_signature_order(self):
        """``supportai.py`` calls ``search`` positionally with method_params."""
        signature = _params(
            _parse(os.path.join(_RETRIEVERS, "SiblingRetriever.py")), "search"
        )
        flags = {"withHyDE", "expand"}
        for node in ast.walk(_parse(_ROUTER)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "search"
                and len(node.args) == len(signature)
            ):
                continue
            passed = [ast.unparse(a) for a in node.args]
            named = [p for p in passed if any(f"'{f}'" in p for f in flags)]
            if len(named) != 2:
                continue
            for arg, param in zip(passed, signature):
                if param in flags:
                    with self.subTest(line=node.lineno, param=param):
                        self.assertIn(
                            f"'{param}'",
                            arg,
                            f"line {node.lineno}: {arg} passed as {param}",
                        )
            return
        self.skipTest("no positional sibling search call found in the router")


if __name__ == "__main__":
    unittest.main()
