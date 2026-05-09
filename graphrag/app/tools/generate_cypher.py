# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Iterable
from langchain_core.output_parsers import StrOutputParser
from langchain.prompts import PromptTemplate
from langchain.tools import BaseTool
from langchain.llms.base import LLM
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.db.connections import get_schema_ver
from common.db.schema_utils import read_type_metadata
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
        schema_ver = get_schema_ver(self.conn)
        if schema_ver is not None and self.schema_ver == schema_ver:
            logger.info(f"Reusing existing schema rep for schema version {schema_ver}")
            return self.schema_rep
        verts = self.conn.getVertexTypes()
        edges = self.conn.getEdgeTypes()
        try:
            entity_descs, rel_defs = read_type_metadata(self.conn)
        except Exception as exc:
            logger.warning(f"read_type_metadata failed: {exc}")
            entity_descs, rel_defs = {}, {}
        vertex_schema = []
        for vert in verts:
            primary_id = self.conn.getVertexType(vert)["PrimaryId"]["AttributeName"]
            attributes = "\n\t\t".join([attr["AttributeName"] + " of type " + attr["AttributeType"]["Name"]
                                        for attr in self.conn.getVertexType(vert)["Attributes"]])
            if attributes == "":
                attributes = "No attributes"
            defn_line = ""
            if entity_descs.get(vert):
                defn_line = f"\n\tDefinition: {entity_descs[vert]}"
            vertex_schema.append(f"{vert}{defn_line}\n\tPrimary Id Attribute: {primary_id}\n\tAttributes: \n\t\t{attributes}")

        edge_schema = []
        for edge in edges:
            from_vertex = self.conn.getEdgeType(edge)["FromVertexTypeName"]
            to_vertex = self.conn.getEdgeType(edge)["ToVertexTypeName"]
            direction = "Directed" if self.conn.getEdgeType(edge)["IsDirected"] else "Undirected"
            #reverse_edge = conn.getEdgeType(edge)["Config"].get("REVERSE_EDGE")
            attributes = "\n\t\t".join([attr["AttributeName"] + " of type " + attr["AttributeType"]["Name"]
                                        for attr in self.conn.getEdgeType(edge)["Attributes"]])
            if attributes == "":
                attributes = "No attributes"
            defn_line = ""
            if rel_defs.get(edge):
                defn_line = f"\n\tDefinition: {rel_defs[edge]}"
            if from_vertex == "*" or to_vertex == "*":
                edge_pairs = self.conn.getEdgeType(edge)["EdgePairs"]
                for an_edge in edge_pairs:
                    edge_info = f"""From Vertex: {an_edge["From"]}\n\tTo Vertex: {an_edge["To"]}"""
                    edge_schema.append(f"""{edge}{defn_line}\n\t{edge_info}\n\tEdge direction: {direction}\n\tAttributes: \n\t\t{attributes}""")
            else:
                edge_info = f"""From Vertex: {from_vertex}\n\tTo Vertex: {to_vertex}"""
                edge_schema.append(f"""{edge}{defn_line}\n\t{edge_info}\n\tEdge direction: {direction}\n\tAttributes: \n\t\t{attributes}""")

        self.schema_rep = f"""The schema of the graph is as follows:
Vertex Types:
{chr(10).join(vertex_schema)}

Edge Types:
{chr(10).join(edge_schema)}
"""
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
            ]
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
