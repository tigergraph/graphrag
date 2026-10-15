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

"""Unit tests for the schema-aware ``LLMEntityRelationshipExtractor``:
the ``allowed_schema`` it is given (types, definitions, rendered schema)
reaches the extraction prompt.

We do not invoke the LLM. The tests exercise prompt assembly in
``adocument_er_extraction`` / ``document_er_extraction`` by capturing
the messages passed to ``ChatPromptTemplate.from_messages``.
"""

from __future__ import annotations

from common.db.schema_utils import AllowedSchema
from common.extractors.LLMEntityRelationshipExtractor import (
    LLMEntityRelationshipExtractor,
)

_SCHEMA_REP = (
    "Vertex Types:\n"
    "Company: A corporate entity.\n"
    "Fund: An investment vehicle pooling capital.\n"
    "Edge Types:\n"
    "PUBLISHES (FROM Company, TO Report): A company publishes a report."
)


def _schema(**kw):
    base = dict(
        schema_rep=_SCHEMA_REP,
        vertex_types=["Company", "Fund"],
        edge_types=["PUBLISHES"],
        vertex_definitions={"Company": "A corporate entity.",
                            "Fund": "An investment vehicle pooling capital."},
        edge_definitions={"PUBLISHES": "A company publishes a report."},
    )
    base.update(kw)
    return AllowedSchema(**base)


def _rendered(monkeypatch, **kw):
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(_StubLLMService(), **kw)
    monkeypatch.setattr(ext, "_extract_kg_from_doc", lambda *a, **kw: [])
    ext.document_er_extraction("hello")
    return "\n".join(m[1] for m in captured)


class _StubLLMService:
    entity_relationship_extraction_prompt = "(system prompt)"
    llm = object()


def _capture_prompt_messages(monkeypatch):
    """Patch ChatPromptTemplate.from_messages so we can inspect what
    messages the extractor assembles. Returns the captured list.
    """
    captured: list = []

    class _FakeTemplate:
        def __init__(self, msgs):
            self._msgs = msgs

        def __or__(self, other):
            return self  # short-circuit chain composition

    def _from_messages(msgs):
        captured.extend(msgs)
        return _FakeTemplate(msgs)

    monkeypatch.setattr(
        "langchain_core.prompts.ChatPromptTemplate.from_messages", _from_messages
    )
    return captured


def test_extractor_without_definitions_leaves_prompt_unchanged(monkeypatch):
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(_StubLLMService())

    # We patch _extract_kg_from_doc to a no-op so the chain isn't actually
    # invoked.
    monkeypatch.setattr(
        ext, "_extract_kg_from_doc", lambda *a, **kw: []
    )

    ext.document_er_extraction("hello world")

    rendered = "\n".join(m[1] for m in captured)
    assert "Schema entity types with definitions" not in rendered
    assert "Schema relationship types with definitions" not in rendered
    assert "(system prompt)" in rendered


def test_extractor_puts_the_schema_with_definitions_in_the_prompt(monkeypatch):
    rendered = _rendered(monkeypatch, allowed_schema=_schema())
    assert "Company: A corporate entity." in rendered
    assert "Fund: An investment vehicle pooling capital." in rendered
    assert "PUBLISHES (FROM Company, TO Report): A company publishes a report." in rendered
    # The definitions are used to choose between similar types.
    assert "disambiguate" in rendered.lower()


def test_extractor_lists_the_allowed_types(monkeypatch):
    rendered = _rendered(monkeypatch, allowed_schema=_schema())
    assert "Allowed Node Types: ['Company', 'Fund']" in rendered
    assert "Allowed Edge Types: ['PUBLISHES']" in rendered


def test_extractor_without_schema_text_adds_no_schema_block(monkeypatch):
    rendered = _rendered(monkeypatch, allowed_schema=_schema(schema_rep=""))
    assert "disambiguate" not in rendered.lower()
    assert "STRICT SCHEMA MODE" not in rendered


def test_strict_mode_keeps_extraction_inside_the_schema(monkeypatch):
    rendered = _rendered(monkeypatch, allowed_schema=_schema(), strict_mode=True)
    assert "STRICT SCHEMA MODE" in rendered
    assert "Company: A corporate entity." in rendered
