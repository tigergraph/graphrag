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
        allowed_schema=None,
        strict_mode: bool = False,
    ):
        """Build an LLM-driven entity/relationship extractor.

        ``allowed_schema`` is the consolidated description of the
        domain schema the extractor must respect. It carries the
        LLM-facing text rendering plus the structured maps the worker
        layer uses for coercion and endpoint validation. Pass ``None``
        for "no schema — extract anything" mode.

        ``strict_mode`` (default ``False``) — when ``True`` the parser
        drops nodes / relationships whose type isn't in the schema
        AND the prompt tells the LLM to stay within it. Read from
        ``graphrag_config.strict_mode`` by the ECC builder.
        """
        from common.db.schema_utils import AllowedSchema
        self.llm_service = llm_service
        self.allowed_schema = allowed_schema or AllowedSchema()
        self.strict_mode = strict_mode

    # Thin @property accessors so the worker can read schema fields
    # directly off the extractor without unpacking ``allowed_schema``.

    @property
    def allowed_vertex_types(self):
        return self.allowed_schema.vertex_types or None

    @property
    def allowed_edge_types(self):
        return self.allowed_schema.edge_types or None

    @property
    def entity_type_definitions(self):
        return self.allowed_schema.vertex_definitions

    @property
    def relationship_type_definitions(self):
        return self.allowed_schema.edge_definitions

    @property
    def domain_edge_endpoints(self):
        return self.allowed_schema.edge_endpoints

    @property
    def entity_type_attributes(self):
        return self.allowed_schema.vertex_attributes

    @property
    def relationship_type_attributes(self):
        return self.allowed_schema.edge_attributes

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

    @staticmethod
    def _rel_props(rels: dict) -> dict:
        """Pull a ``properties`` / ``attributes`` dict off an LLM-
        emitted relationship object. Empty dict when neither key is
        present or the value isn't a dict.
        """
        p = rels.get("properties") or rels.get("attributes") or {}
        return p if isinstance(p, dict) else {}

    def _format_type_attributes(self, type_attrs: dict) -> str:
        """Render ``{type_name: {attr_name: tg_type}}`` as a nested
        block the LLM can read::

            - Filing
              - filed_at (DATETIME)
              - amount (DOUBLE)
              - jurisdiction (STRING)
            - Company
              - founded_year (INT)
              - industry (STRING)

        Empty when no types carry attributes.
        """
        if not type_attrs:
            return ""
        lines = []
        for name in sorted(type_attrs.keys()):
            attrs = type_attrs.get(name) or {}
            if not attrs:
                continue
            lines.append(f"- {name}")
            for attr_name in sorted(attrs.keys()):
                lines.append(f"  - {attr_name} ({attrs[attr_name]})")
        return "\n".join(lines)

    def _build_schema_prompt_messages(self) -> list:
        """Return the human-message tuples that describe the domain
        schema to the LLM. Used by both sync and async extraction paths.
        Empty list when no schema is configured.
        """
        msgs = []
        schema_rep = (self.allowed_schema.schema_rep or "").strip()
        if not schema_rep:
            return msgs

        if self.strict_mode:
            msgs.append((
                "human",
                "STRICT SCHEMA MODE: only emit entities whose entity_type "
                "matches one of the vertex types in the schema below, and "
                "only emit relationships whose relation_type matches an "
                "edge type AND whose source / target match a declared "
                "(FROM, TO) endpoint pair. Drop any entity or relationship "
                "that doesn't fit. Do NOT invent new types.",
            ))
        else:
            msgs.append((
                "human",
                "When choosing the entity_type / relationship_type for an "
                "extraction, strongly prefer the schema types listed below "
                "and use their definitions to disambiguate similar types. "
                "Ignore page-structure / chart / layout artifacts (axes, "
                "segments, percentages, page numbers, sections, navigation "
                "menus, captions). Prefer concrete real-world entities over "
                "abstract categorical groupings. Only invent a new type "
                "when nothing in the schema fits.",
            ))
        msgs.append(("human", schema_rep))
        msgs.append((
            "human",
            "For every node and relationship, populate a `properties` map "
            "with values you find in the text for the attributes shown in "
            "the schema. Use the exact attribute names listed. Match the "
            "declared type: INT / UINT as integers, DOUBLE / FLOAT as "
            "numbers, BOOL as true/false, DATETIME as an ISO-8601 string "
            "(e.g. \"2024-01-15\" or \"2024-01-15T09:30:00\"). Omit "
            "attributes you can't find values for — partial coverage is "
            "fine. Do NOT invent attribute names beyond those in the "
            "schema. The `id` / primary-id attribute lives on the node's "
            "`id` field — do NOT also put it in `properties`.",
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

            formatted_rels = self._format_rels(json_out["rels"])
            formatted_nodes = self._format_nodes(json_out["nodes"])

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

            return [GraphDocument(
                nodes=self._build_nodes(formatted_nodes),
                relationships=self._build_rels(formatted_rels),
                source=Document(page_content=doc),
            )]

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

            formatted_rels = self._format_rels(json_out["rels"])
            formatted_nodes = self._format_nodes(json_out["nodes"])

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

            return [GraphDocument(
                nodes=self._build_nodes(formatted_nodes),
                relationships=self._build_rels(formatted_rels),
                source=Document(page_content=doc),
            )]

        except:
            return [GraphDocument(nodes=[], relationships=[], source=Document(page_content=doc))]

    # --- LLM-output normalization helpers (shared by sync + async) ----

    @staticmethod
    def _resolve_id_and_props(value):
        """Source / target in the LLM's ``rels`` list may come as a
        bare id string or as a dict with ``id`` + optional
        ``node_type`` + optional ``properties``. Return
        ``(id_str, node_type_str, props_dict)``. ``node_type`` is the
        empty string when the LLM didn't carry one on this endpoint;
        callers should fall back to the entity's own node entry to
        recover the type in that case.
        """
        if isinstance(value, dict):
            props = value.get("properties") or value.get("attributes") or {}
            node_type = value.get("node_type") or value.get("type") or ""
            return (
                str(value.get("id", "")),
                str(node_type),
                props if isinstance(props, dict) else {},
            )
        return str(value), "", {}

    def _format_rels(self, rels_in: list) -> list:
        formatted = []
        for rels in rels_in or []:
            try:
                src_id, src_type, src_props = self._resolve_id_and_props(rels["source"])
                tgt_id, tgt_type, tgt_props = self._resolve_id_and_props(rels["target"])
                if not (src_id and tgt_id):
                    continue
                # Edge-level properties (typed attrs the LLM extracted
                # for this edge type, e.g. ``MONEY_TRANSFER.amount``)
                # live directly under the rel object. Source/target
                # vertex attrs are kept separately so the worker can
                # apply each to the right row.
                rel_props = self._rel_props(rels)
                formatted.append({
                    "source": src_id,
                    "target": tgt_id,
                    "source_type": src_type.replace(" ", "_").capitalize() if src_type else "",
                    "target_type": tgt_type.replace(" ", "_").capitalize() if tgt_type else "",
                    "source_props": src_props,
                    "target_props": tgt_props,
                    "type": rels["relation_type"].replace(" ", "_").upper(),
                    "definition": rels.get("definition", ""),
                    "properties": rel_props,
                })
            except (KeyError, TypeError):
                continue
        return formatted

    def _format_nodes(self, nodes_in: list) -> list:
        formatted = []
        for node in nodes_in or []:
            try:
                # ``properties`` (or ``attributes``) is optional — the
                # LLM may omit it when nothing in the text fits the
                # typed attribute schema we sent.
                props = node.get("properties") or node.get("attributes") or {}
                formatted.append({
                    "id": node["id"],
                    "type": node["node_type"].replace(" ", "_").capitalize(),
                    "definition": node.get("definition", ""),
                    "properties": props if isinstance(props, dict) else {},
                })
            except (KeyError, TypeError):
                continue
        return formatted

    def _build_nodes(self, formatted_nodes: list) -> list:
        nodes = []
        for node in formatted_nodes:
            # Forward LLM-emitted typed attributes alongside the
            # description text. The worker splits ``description``
            # (-> Entity row) from typed attributes (-> domain VT row)
            # and coerces / filters the latter to the live schema.
            node_props = {**(node.get("properties") or {}),
                          "description": node["definition"]}
            nodes.append(Node(id=node["id"],
                              type=node["type"],
                              properties=node_props))
        return nodes

    def _build_rels(self, formatted_rels: list) -> list:
        relationships = []
        for rel in formatted_rels:
            src_props = {**(rel.get("source_props") or {}),
                         "description": rel["definition"]}
            tgt_props = {**(rel.get("target_props") or {}),
                         "description": rel["definition"]}
            edge_props = {**(rel.get("properties") or {}),
                          "description": rel["definition"]}
            # Use the canonical entity types when the LLM provided them
            # on the relationship endpoints; fall back to the id so the
            # field is never empty. Downstream endpoint-pair validation
            # in the worker relies on these values matching the live
            # schema's declared edge endpoints.
            src_type = rel.get("source_type") or rel["source"]
            tgt_type = rel.get("target_type") or rel["target"]
            relationships.append(Relationship(
                source=Node(id=rel["source"], type=src_type,
                            properties=src_props),
                target=Node(id=rel["target"], type=tgt_type,
                            properties=tgt_props),
                type=rel["type"],
                properties=edge_props,
            ))
        return relationships
        
    async def adocument_er_extraction(self, document):
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import PydanticOutputParser

    
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
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import PydanticOutputParser

    
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
    

