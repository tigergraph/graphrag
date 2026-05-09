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

import json
import re
from typing import List
import logging

from common.extractors.BaseExtractor import BaseExtractor
from common.llm_services import LLM_Model
from common.py_schemas import KnowledgeGraph
from langchain_community.graphs.graph_document import Node, Relationship, GraphDocument
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

class LLMEntityRelationshipExtractor(BaseExtractor):
    def __init__(
        self,
        llm_service: LLM_Model,
        allowed_entity_types: List[str] = None,
        allowed_relationship_types: List[str] = None,
        strict_mode: bool = False,
        entity_type_definitions: dict = None,
        relationship_type_definitions: dict = None,
        domain_edge_endpoints: dict = None,
    ):
        self.llm_service = llm_service
        self.allowed_vertex_types = allowed_entity_types
        self.allowed_edge_types = allowed_relationship_types
        # When True the existing parser filter (drop nodes/rels whose
        # type isn't in the allowed list) is enforced AND the prompt
        # tells the LLM to stay within the schema. Read from
        # graphrag_config.strict_mode by the ECC builder.
        self.strict_mode = strict_mode
        self.entity_type_definitions = dict(entity_type_definitions or {})
        self.relationship_type_definitions = dict(
            relationship_type_definitions or {}
        )
        # Per-edge ``{name: [(from_vt, to_vt), ...]}`` derived from the
        # live schema. Used by the prompt to tell the LLM the valid
        # source/target pairs per relationship type, and by the ingest
        # worker to validate that an extracted relationship's endpoints
        # match a declared pair before writing IS_HEAD_OF / HAS_TAIL.
        self.domain_edge_endpoints = {
            k: list(v) for k, v in (domain_edge_endpoints or {}).items()
        }

    def _format_definitions(self, defs: dict) -> str:
        """Render a ``{type_name: definition}`` dict as one
        ``- <Name>: <definition>`` line per type, sorted by name. Used
        when assembling the schema-aware extraction prompt.
        """
        if not defs:
            return ""
        return "\n".join(
            f"- {name}: {definition}"
            for name, definition in sorted(defs.items())
            if definition
        )

    def _format_edge_endpoints(self) -> str:
        """Render ``{edge_name: [(from, to), ...]}`` as
        ``- <name>: <from> -> <to>[, <from2> -> <to2>]`` lines, sorted
        by edge name. Empty when no endpoints are configured.
        """
        if not self.domain_edge_endpoints:
            return ""
        lines = []
        for name, pairs in sorted(self.domain_edge_endpoints.items()):
            pair_strs = ", ".join(f"{f} -> {t}" for f, t in pairs) or "<none>"
            defn = self.relationship_type_definitions.get(name, "")
            tail = f" — {defn}" if defn else ""
            lines.append(f"- {name}: {pair_strs}{tail}")
        return "\n".join(lines)

    def _build_schema_prompt_messages(self) -> list:
        """Return the human-message tuples that describe the domain
        schema to the LLM. Used by both sync and async extraction paths.
        Empty list when no schema is configured.
        """
        msgs = []
        entity_def_block = self._format_definitions(self.entity_type_definitions)
        rel_def_block = self._format_definitions(self.relationship_type_definitions)
        endpoints_block = self._format_edge_endpoints()
        if not (entity_def_block or rel_def_block or endpoints_block):
            return msgs

        if self.strict_mode:
            msgs.append((
                "human",
                "STRICT SCHEMA MODE: only emit entities whose entity_type "
                "matches one of the schema entity types listed below, and "
                "only emit relationships whose relation_type matches a "
                "schema relationship type AND whose source / target "
                "entity types match a declared (FROM, TO) endpoint pair "
                "for that relationship. Drop any entity or relationship "
                "that doesn't fit. Do NOT invent new types.",
            ))
        else:
            msgs.append((
                "human",
                "When deciding the entity_type / relationship_type for an "
                "extraction, strongly prefer the schema types listed below "
                "and use their definitions to disambiguate similar types. "
                "Ignore page-structure / chart / layout artifacts (axes, "
                "segments, percentages, page numbers, sections, navigation "
                "menus, captions). Prefer concrete real-world entities over "
                "abstract categorical groupings. Only invent a new type "
                "when nothing in the schema fits.",
            ))
        if entity_def_block:
            msgs.append((
                "human",
                f"Schema entity types with definitions:\n{entity_def_block}",
            ))
        if endpoints_block:
            msgs.append((
                "human",
                "Schema relationship types — each line lists the valid "
                "(source -> target) endpoint pairs for that relationship "
                "and the relationship's definition:\n" + endpoints_block,
            ))
        elif rel_def_block:
            msgs.append((
                "human",
                f"Schema relationship types with definitions:\n{rel_def_block}",
            ))
        return msgs

    def _parse_json_output(self, content: str) -> dict:
        """Parse JSON from LLM output with multiple fallback strategies.

        Tries in order:
          1. Direct json.loads
          2. Extract from ```json code fences
          3. Regex extraction of first JSON object
        """
        # Try direct parse
        try:
            return json.loads(content.strip("content="))
        except (json.JSONDecodeError, ValueError):
            pass

        # Try ```json code fence
        if "```json" in content:
            try:
                return json.loads(
                    content.split("```")[1].strip("```").strip("json").strip()
                )
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        # Regex fallback: extract first JSON object
        match = re.search(r'\{[\s\S]*\}', content)
        if match:
            return json.loads(match.group())

        raise ValueError(f"Could not extract JSON from LLM output: {content[:200]}")

    async def _aextract_kg_from_doc(self, doc, chain, parser) -> list[GraphDocument]:
        try:
            logger.debug(str(doc))
            out = await chain.ainvoke(
                {"input": doc, "format_instructions": parser.get_format_instructions()}
            )
            logger.debug(str(out))
        except Exception as e:
            return [GraphDocument(nodes=[], relationships=[], source=Document(page_content=doc))]
        try:
            json_out = self._parse_json_output(out.content)

            formatted_rels = []
            for rels in json_out["rels"]:
                if isinstance(rels["source"], str) and isinstance(rels["target"], str):
                    formatted_rels.append(
                        {
                            "source": rels["source"],
                            "target": rels["target"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                elif isinstance(rels["source"], dict) and isinstance(
                    rels["target"], str
                ):
                    formatted_rels.append(
                        {
                            "source": rels["source"]["id"],
                            "target": rels["target"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                elif isinstance(rels["source"], str) and isinstance(
                    rels["target"], dict
                ):
                    formatted_rels.append(
                        {
                            "source": rels["source"],
                            "target": rels["target"]["id"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                elif isinstance(rels["source"], dict) and isinstance(
                    rels["target"], dict
                ):
                    formatted_rels.append(
                        {
                            "source": rels["source"]["id"],
                            "target": rels["target"]["id"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                else:
                    raise Exception("Relationship parsing error")
            formatted_nodes = []
            for node in json_out["nodes"]:
                formatted_nodes.append(
                    {
                        "id": node["id"],
                        "type": node["node_type"].replace(" ", "_").capitalize(),
                        "definition": node["definition"],
                    }
                )

            # filter relationships and nodes based on allowed types
            if self.strict_mode:
                if self.allowed_vertex_types:
                    formatted_nodes = [
                        node
                        for node in formatted_nodes
                        if node["type"] in self.allowed_vertex_types
                    ]
                if self.allowed_edge_types:
                    formatted_rels = [
                        rel
                        for rel in formatted_rels
                        if rel["type"] in self.allowed_edge_types
                    ]

            nodes = []
            for node in formatted_nodes:
                nodes.append(Node(id=node["id"],
                                  type=node["type"],
                                  properties={"description": node["definition"]}))
            relationships = []
            for rel in formatted_rels:
                relationships.append(Relationship(source=Node(id=rel["source"], type=rel["source"],
                                                  properties={"description": rel["definition"]}),
                                                  target=Node(id=rel["target"], type=rel["target"],
                                                  properties={"description": rel["definition"]}), type=rel["type"]))

            return [GraphDocument(nodes=nodes, relationships=relationships, source=Document(page_content=doc))]

        except:
            return [GraphDocument(nodes=[], relationships=[], source=Document(page_content=doc))]

    def _extract_kg_from_doc(self, doc, chain, parser) -> list[GraphDocument]:
        try:
            out = chain.invoke(
                {"input": doc, "format_instructions": parser.get_format_instructions()}
            )
        except Exception as e:
            return [GraphDocument(nodes=[], relationships=[], source=Document(page_content=doc))]
        try:
            json_out = self._parse_json_output(out.content)

            formatted_rels = []
            for rels in json_out["rels"]:
                if isinstance(rels["source"], str) and isinstance(rels["target"], str):
                    formatted_rels.append(
                        {
                            "source": rels["source"],
                            "target": rels["target"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                elif isinstance(rels["source"], dict) and isinstance(
                    rels["target"], str
                ):
                    formatted_rels.append(
                        {
                            "source": rels["source"]["id"],
                            "target": rels["target"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                elif isinstance(rels["source"], str) and isinstance(
                    rels["target"], dict
                ):
                    formatted_rels.append(
                        {
                            "source": rels["source"],
                            "target": rels["target"]["id"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                elif isinstance(rels["source"], dict) and isinstance(
                    rels["target"], dict
                ):
                    formatted_rels.append(
                        {
                            "source": rels["source"]["id"],
                            "target": rels["target"]["id"],
                            "type": rels["relation_type"].replace(" ", "_").upper(),
                            "definition": rels["definition"],
                        }
                    )
                else:
                    raise Exception("Relationship parsing error")
            formatted_nodes = []
            for node in json_out["nodes"]:
                formatted_nodes.append(
                    {
                        "id": node["id"],
                        "type": node["node_type"].replace(" ", "_").capitalize(),
                        "definition": node["definition"],
                    }
                )

            # filter relationships and nodes based on allowed types
            if self.strict_mode:
                if self.allowed_vertex_types:
                    formatted_nodes = [
                        node
                        for node in formatted_nodes
                        if node["type"] in self.allowed_vertex_types
                    ]
                if self.allowed_edge_types:
                    formatted_rels = [
                        rel
                        for rel in formatted_rels
                        if rel["type"] in self.allowed_edge_types
                    ]
        
            nodes = []
            for node in formatted_nodes:
                nodes.append(Node(id=node["id"],
                                  type=node["type"],
                                  properties={"description": node["definition"]}))
            relationships = []
            for rel in formatted_rels:
                relationships.append(Relationship(source=Node(id=rel["source"], type=rel["source"],
                                                  properties={"description": rel["definition"]}),
                                                  target=Node(id=rel["target"], type=rel["target"],
                                                  properties={"description": rel["definition"]}), type=rel["type"]))

            return [GraphDocument(nodes=nodes, relationships=relationships, source=Document(page_content=doc))]

        except:
            return [GraphDocument(nodes=[], relationships=[], source=Document(page_content=doc))]
        
    async def adocument_er_extraction(self, document):
        from langchain.prompts import ChatPromptTemplate
        from langchain.output_parsers import PydanticOutputParser

    
        parser = PydanticOutputParser(pydantic_object=KnowledgeGraph)
        prompt = [
            ("system", self.llm_service.entity_relationship_extraction_prompt),
            (
                "human",
                "Tip: Make sure to answer in the correct format and do "
                "not include any explanations. "
                "Use the given format to extract information from the "
                "following input: {input}",
            ),
            (
                "human",
                "Mandatory: Make sure to answer in the correct format, specified here: {format_instructions}",
            ),
        ]
        if self.allowed_vertex_types or self.allowed_edge_types:
            prompt.append(
                (
                    "human",
                    "Tip: Make sure to use the following types if they are applicable. "
                    "If the input does not contain any of the types, you may create your own.",
                )
            )
        if self.allowed_vertex_types:
            prompt.append(("human", f"Allowed Node Types: {self.allowed_vertex_types}"))
        if self.allowed_edge_types:
            prompt.append(("human", f"Allowed Edge Types: {self.allowed_edge_types}"))
        prompt.extend(self._build_schema_prompt_messages())
        prompt = ChatPromptTemplate.from_messages(prompt)
        chain = prompt | self.llm_service.llm  # | parser
        er = await self._aextract_kg_from_doc(document, chain, parser)
        return er


    def document_er_extraction(self, document):
        from langchain.prompts import ChatPromptTemplate
        from langchain.output_parsers import PydanticOutputParser

    
        parser = PydanticOutputParser(pydantic_object=KnowledgeGraph)
        prompt = [
            ("system", self.llm_service.entity_relationship_extraction_prompt),
            (
                "human",
                "Tip: Make sure to answer in the correct format and do "
                "not include any explanations. "
                "Use the given format to extract information from the "
                "following input: {input}",
            ),
            (
                "human",
                "Mandatory: Make sure to answer in the correct format, specified here: {format_instructions}",
            ),
        ]
        if self.allowed_vertex_types or self.allowed_edge_types:
            prompt.append(
                (
                    "human",
                    "Tip: Make sure to use the following types if they are applicable. "
                    "If the input does not contain any of the types, you may create your own.",
                )
            )
        if self.allowed_vertex_types:
            prompt.append(("human", f"Allowed Node Types: {self.allowed_vertex_types}"))
        if self.allowed_edge_types:
            prompt.append(("human", f"Allowed Edge Types: {self.allowed_edge_types}"))
        prompt.extend(self._build_schema_prompt_messages())
        prompt = ChatPromptTemplate.from_messages(prompt)
        chain = prompt | self.llm_service.llm  # | parser
        er = self._extract_kg_from_doc(document, chain, parser)
        return er

    def extract(self, text):
        return self.document_er_extraction(text)
    
    async def aextract(self, text) -> list[GraphDocument]:
        return await self.adocument_er_extraction(text)
    

