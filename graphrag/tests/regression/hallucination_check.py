"""Graded hallucination grader for the regression evaluation pipeline (GML-2088).

A 0.0-1.0 confidence variant used ONLY by tests/regression/evaluator.py to score answers.
The production agent uses the binary check in
graphrag/app/agent/agent_hallucination_check.py; this copy is kept separate so
eval-scoring changes never alter runtime code.
"""

import logging
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

from pydantic import BaseModel, Field
from common.logs.logwriter import LogWriter
from common.logs.log import req_id_cv

logger = logging.getLogger(__name__)


class HallucinationCheckResponse(BaseModel):
    confidence: float = Field(
        description=(
            "Confidence score between 0.0 and 1.0 that the answer is hallucinated. "
            "0.0 = fully grounded in the context, 1.0 = completely hallucinated. "
            "Use the full range — e.g. 0.2 for mostly grounded with minor gaps, "
            "0.8 for mostly unsupported claims."
        )
    )
    reason: str = Field(
        description=(
            "A concise one-to-two sentence explanation of why you assigned this "
            "confidence score, citing specific claims from the answer and context."
        )
    )


class TigerGraphAgentHallucinationCheck:
    def __init__(self, llm_model):
        self.llm = llm_model

    def check_hallucination(
        self,
        generation: str,
        context: str,
        question: str = "",
    ) -> HallucinationCheckResponse:
        """Score how likely the generated answer is hallucinated.

        Args:
            generation: The answer produced by the RAG pipeline.
            context:    The retrieved context chunks passed to the LLM.
            question:   The original user question (improves grounding judgement).

        Returns:
            HallucinationCheckResponse with confidence (0–1) and reason.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY check_hallucination")

        hallucination_parser = PydanticOutputParser(
            pydantic_object=HallucinationCheckResponse
        )

        question_block = (
            f"\nORIGINAL QUESTION:\n{question}\n" if question.strip() else ""
        )

        prompt = PromptTemplate(
            template="""You are an expert grader evaluating whether a GraphRAG \
pipeline answer is grounded in the retrieved context passages.

GraphRAG answers are SYNTHESIZED — they combine, rephrase, and connect \
information from multiple passages. Reasonable synthesis and inference are \
acceptable. However, specific facts (numbers, percentages, names, dates) \
must be correctly attributed — using a figure from context but applying it \
to the wrong category or time period IS a hallucination.
{question_block}
RETRIEVED CONTEXT PASSAGES:
-------
{context}
-------

ANSWER TO EVALUATE:
{generation}

SCORING GUIDE — confidence that the answer is hallucinated (0.0 = fully \
grounded, 1.0 = fully hallucinated):

0.0–0.2  Fully grounded: all key claims are directly stated in or correctly \
inferred from the context. Paraphrasing and synthesis are expected.

0.2–0.4  Mostly grounded: core ideas are supported; minor details extend \
beyond the context but are logically consistent and do not contradict anything.

0.4–0.6  Partially grounded: some claims are only loosely supported OR \
specific facts are misattributed (e.g. a correct number applied to the wrong \
category, time period, or entity).

0.6–0.8  Mostly hallucinated: several key claims are unsupported or specific \
facts clearly contradict the context.

0.8–1.0  Fully hallucinated: the answer is largely unrelated to the context \
or fabricates significant facts.

RULES:
- Accept synthesis and logical combination across multiple passages.
- Accept domain knowledge that logically follows from the context topic.
- Context may be incomplete. Judge consistency, not exhaustiveness.
- Use semantic alignment, NOT keyword matching.
- Ignore references to diagrams, figures, or images — context is text only.
- PENALISE when: (1) a specific number/percentage/date is used but applied \
to the wrong subject; (2) a claim directly contradicts the context; OR \
(3) the answer introduces topics entirely unrelated to the context.

Provide your verdict as JSON.
Format: {format_instructions}""",
            input_variables=["generation", "context", "question_block"],
            partial_variables={
                "format_instructions": hallucination_parser.get_format_instructions()
            },
        )

        prediction = self.llm.invoke_with_parser(
            prompt,
            hallucination_parser,
            {
                "context":        context,
                "generation":     generation,
                "question_block": question_block,
            },
            caller_name="check_hallucination",
        )
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT check_hallucination")
        return prediction
