# Copyright (c) 2025 TigerGraph, Inc.
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
from langchain_community.callbacks.manager import get_openai_callback
from langchain_core.output_parsers import StrOutputParser
from langchain.prompts import PromptTemplate
from langchain.tools import BaseTool
from langchain.llms.base import LLM
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.db.connections import get_schema_ver

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
        vertex_schema = []
        for vert in verts:
            primary_id = self.conn.getVertexType(vert)["PrimaryId"]["AttributeName"]
            attributes = "\n\t\t".join([attr["AttributeName"] + " of type " + attr["AttributeType"]["Name"] 
                                        for attr in self.conn.getVertexType(vert)["Attributes"]])
            if attributes == "":
                attributes = "No attributes"
            vertex_schema.append(f"{vert}\n\tPrimary Id Attribute: {primary_id}\n\tAttributes: \n\t\t{attributes}")

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
            if from_vertex == "*" or to_vertex == "*":
                edge_pairs = self.conn.getEdgeType(edge)["EdgePairs"]
                for an_edge in edge_pairs:
                    edge_info = f"""From Vertex: {an_edge["From"]}\n\tTo Vertex: {an_edge["To"]}"""
                    edge_schema.append(f"""{edge}\n\t{edge_info}\n\tEdge direction: {direction}\n\tAttributes: \n\t\t{attributes}""")
            else:
                edge_info = f"""From Vertex: {from_vertex}\n\tTo Vertex: {to_vertex}"""
                edge_schema.append(f"""{edge}\n\t{edge_info}\n\tEdge direction: {direction}\n\tAttributes: \n\t\t{attributes}""")

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

        schema = self._generate_schema_rep()
    
        logger.debug_pii("Prompt to LLM:\n" + PROMPT.invoke({"question": question, "schema": schema, "history": history}).to_string())

        chain = PROMPT | self.llm.model | StrOutputParser()
        usage_data = {}
        with get_openai_callback() as cb:
            out = chain.invoke({"question": question, "schema": schema, "history": history}).strip("```cypher").strip("```")

            usage_data["input_tokens"] = cb.prompt_tokens
            usage_data["output_tokens"] = cb.completion_tokens
            usage_data["total_tokens"] = cb.total_tokens
            usage_data["cost"] = cb.total_cost
            logger.info(f"generate_cypher usage: {usage_data}")

        query_header = "USE GRAPH " + self.conn.graphname + " "+ "\n" + "INTERPRET OPENCYPHER QUERY () {" + "\n"
        query_footer = "\n}"
        return query_header + out + query_footer
    
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
