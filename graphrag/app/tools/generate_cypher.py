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

import logging
import re
from typing import Iterable
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import BaseTool
from langchain_core.language_models.llms import LLM
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.db.connections import get_schema_ver
from common.db.schema_utils import render_schema_rep
from common.logs.logwriter import LogWriter
from common.logs.log import req_id_cv

logger = logging.getLogger(__name__)

# Language names a model may put on the first line of a code block.
_FENCE_LANGUAGES = {"cypher", "opencypher", "gsql", "sql", "graphql"}


def strip_code_fence(text: str) -> str:
    """The query inside a model reply: without a code fence (```cypher,
    ```opencypher, plain ```) and without a line that has only a language
    name."""
    t = (text or "").strip()
    m = re.match(r"^```[^\n]*\n(.*?)\n?```\s*$", t, re.S)
    if m:
        t = m.group(1).strip()
    else:
        t = t.strip("`").strip()
    lines = t.split("\n", 1)
    if lines[0].strip().lower() in _FENCE_LANGUAGES:
        t = lines[1].strip() if len(lines) > 1 else ""
    return t


def _interpreted(conn, cypher: str) -> str:
    return ("INTERPRET OPENCYPHER QUERY () FOR GRAPH " + str(conn.graphname)
            + " {\n" + cypher + "\n}")


def run_cypher(conn, cypher: str) -> list:
    """Run an openCypher query in interpreted mode on the graph of ``conn``
    and return its result blocks. Raises the connection's exception with
    TigerGraph's message when the query fails."""
    return conn.runInterpretedQuery(_interpreted(conn, cypher)) or []


# Clauses that change data. Retrieval only reads, whatever the model writes.
_WRITE_CLAUSE = re.compile(r"\b(CREATE|MERGE|SET|REMOVE|DELETE)\b", re.I)
_STRING_LITERAL = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")

# TigerGraph limits worth a direct hint when the check reports them.
_HINTS = (
    (re.compile(r"\bdatetime\s*\(", re.I),
     'TigerGraph does not support datetime(). Use to_datetime("YYYY-MM-DD hh:mm:ss").'),
    (re.compile(r"\bdate\s*\(", re.I),
     'TigerGraph does not support date(). Use to_datetime("YYYY-MM-DD").'),
)


# Run errors that pass the check, with the query construct that causes them.
# TigerGraph 4.3 fails a query that has a null test and STARTS WITH, CONTAINS
# or CASE, and its message does not name the construct.
_NULL_TEST = re.compile(r"\bIS\s+(NOT\s+)?NULL\b", re.I)
_RUN_HINTS = (
    (re.compile(r"instruction mode and legacy mode", re.I), _NULL_TEST,
     'TigerGraph cannot run IS NULL / IS NOT NULL in this query. An attribute that the schema '
     'does not mark NULLABLE is never NULL: compare with the default of its type instead '
     '(= "" or <> "" for STRING, = 0 or <> 0 for numbers).'),
)


def run_error_hints(error: str, cypher: str) -> list:
    """Hints for a run error of ``cypher``: the construct that likely caused
    it and how to write it instead."""
    query = _STRING_LITERAL.sub('""', cypher)
    return [hint for err_pattern, query_pattern, hint in _RUN_HINTS
            if err_pattern.search(error) and query_pattern.search(query)]


def check_cypher(conn, cypher: str):
    """Errors in ``cypher`` found without running it, as readable lines.

    Uses TigerGraph's query check (TigerGraph 4.0 and later). Returns [] for
    a valid query, or None when the check is not available, so the caller
    runs the query to find out.
    """
    if _WRITE_CLAUSE.search(_STRING_LITERAL.sub('""', cypher)):
        return ["The query changes data. Write a query that only reads (MATCH ... RETURN)."]
    try:
        result = conn.checkQuerySemantic(_interpreted(conn, cypher))
    except Exception as exc:
        logger.info(f"generate_cypher: query check unavailable ({exc}); running the query instead")
        return None
    if not isinstance(result, dict):
        return None
    errors = []
    for err in result.get("errors") or []:
        # The check counts the INTERPRET wrapper as line 1; report query lines.
        line = err.get("line")
        where = f"line {line - 1}, column {err.get('charpositioninline')}: " if isinstance(line, int) and line > 1 else ""
        errors.append(where + str(err.get("msg", "")).strip())
    if errors:
        errors.extend(hint for pattern, hint in _HINTS if pattern.search(cypher))
    return errors


CYPHER_MAX_ATTEMPTS = 10


def generate_and_run_cypher(cypher_gen, conn, question: str, max_attempts: int = CYPHER_MAX_ATTEMPTS,
                            attempts=None):
    """Generate an openCypher query for ``question``, check it, and run it.

    A query that fails the check is not run: the errors go back to the
    generator for the next attempt. Stops at ``max_attempts``, or early when
    the generator repeats a query it already wrote. Every attempt that did not
    return rows is appended to ``attempts`` as ``{query, error, outcome}``.

    Returns ``(cypher, first_result_block)`` for the query that ran (the block
    is None when the query returned no rows), or ``(None, None)`` when no query
    ran successfully.
    """
    from common.utils.query_results import is_empty_result

    attempts = [] if attempts is None else attempts
    history: list = []
    seen: set = set()
    for i in range(max(1, int(max_attempts or CYPHER_MAX_ATTEMPTS))):
        try:
            cypher = cypher_gen._run(question, history)
        except ValueError as exc:
            history.append(f"{i}: Error: {exc}\n")
            attempts.append({"error": str(exc)[:500]})
            continue
        if cypher in seen:
            attempts.append({"query": cypher, "outcome": "repeated an earlier query; stopped"})
            break
        seen.add(cypher)
        errors = check_cypher(conn, cypher)
        if errors:
            message = "\n".join(errors)
            history.append(f"{i}: {cypher}\n\tError: {message}\n")
            attempts.append({"query": cypher, "error": message[:500], "outcome": "failed check"})
            continue
        try:
            rows = run_cypher(conn, cypher)
        except Exception as exc:
            message = "\n".join([str(exc)] + run_error_hints(str(exc), cypher))
            history.append(f"{i}: {cypher}\n\tError: {message}\n")
            attempts.append({"query": cypher, "error": message[:500]})
            continue
        first = rows[0] if rows else None
        if is_empty_result(first):
            attempts.append({"query": cypher, "outcome": "no rows"})
            return cypher, None
        return cypher, first
    return None, None


class GenerateCypher(BaseTool):
    """GenerateCypher Tool.
    Tool to generate and execute the appropriate Cypher query for the question.
    """
    name: str = "GenerateCypher"
    description: str = "Generates a Cypher query for the question."
    conn: TigerGraphConnectionProxy = None
    llm: LLM = None
    schema_rep: str = None
    schema_ver: int = None

    def __init__(self, conn: TigerGraphConnectionProxy, llm):
        """Initialize GenerateCypher.
        Args:
            conn (TigerGraphConnection):
                pyTigerGraph TigerGraphConnection connection to the appropriate database/graph with correct permissions
            llm (LLM_Model):
                LLM_Model class to interact with an external LLM API.
            prompt (str):
                prompt to use with the LLM_Model. Varies depending on LLM service.
        """
        super().__init__()
        self.conn = conn
        self.llm = llm
        self.schema_rep = ""
        self.schema_ver = -1

    def _generate_schema_rep(self):
        # Schema rendering is shared with generate_gsql + the
        # question-mapping tools via ``schema_utils.render_schema_rep``;
        # we only keep the per-instance cache here.
        snap = render_schema_rep(self.conn)
        text, schema_ver = snap.schema_rep, snap.schema_version
        if schema_ver is not None and self.schema_ver == schema_ver and self.schema_rep:
            logger.info(f"Reusing existing schema rep for schema version {schema_ver}")
            return self.schema_rep
        self.schema_rep = text
        self.schema_ver = schema_ver if schema_ver is not None else -1
        return self.schema_rep
        
    def generate_cypher(self, question: str, history: Iterable[str]) -> str:
        """Generate Cypher query for the question.
        Args:
            question (str):
                question to generate the Cypher query for.
        Returns:
            str:
                Cypher query for the question.
        """
        PROMPT = PromptTemplate(
            template=self.llm.generate_cypher_prompt,
            input_variables=[
                "question",
                "schema",
                "history"
            ],
            partial_variables={
                # Pre-bind the Query Guidance partial; empty when no
                # override is configured.
                "query_guidance": self.llm.query_guidance_block,
            },
        )

        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY generate_cypher with {question}")
        schema = self._generate_schema_rep()
        logger.debug_pii("Prompt to LLM:\n" + PROMPT.invoke({"question": question, "schema": schema, "history": history}).to_string())

        out = self.llm.invoke_with_parser(
            PROMPT, StrOutputParser(),
            {"question": question, "schema": schema, "history": history},
            caller_name="generate_cypher",
        )
        out = strip_code_fence(out)

        # Validate the LLM output looks like a Cypher query
        out_upper = out.upper()
        if not any(kw in out_upper for kw in ("MATCH", "RETURN", "WITH", "UNWIND", "CALL")):
            LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_cypher - LLM did not produce a valid Cypher query")
            raise ValueError(f"LLM did not produce a valid Cypher query: {out[:200]}")

        LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_cypher with:\n{out}")
        return out

    def _run(self, question: str, history: Iterable[str]):
        """Run the GenerateCypher tool.
        Args:
            question (str):
                question to generate the Cypher query for.
        Returns:
            str:
                Cypher query for the question.
        """
        return self.generate_cypher(question, history)

    def _arun(self, question: str, history: Iterable[str]):
        raise NotImplementedError("Asynchronous execution is not supported for this tool.")
