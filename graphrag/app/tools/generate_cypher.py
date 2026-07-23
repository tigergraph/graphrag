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
        ).strip("```cypher").strip("```").strip()

        # Validate the LLM output looks like a Cypher query
        out_upper = out.upper()
        if not any(kw in out_upper for kw in ("MATCH", "RETURN", "WITH", "UNWIND", "CALL")):
            LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_cypher - LLM did not produce a valid Cypher query")
            raise ValueError(f"LLM did not produce a valid Cypher query: {out[:200]}")

        query_header = "USE GRAPH " + self.conn.graphname + " "+ "\n" + "INTERPRET OPENCYPHER QUERY () {" + "\n"
        query_footer = "\n}"
        cypher = query_header + out + query_footer
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_cypher with:\n{cypher}")
        return cypher

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
