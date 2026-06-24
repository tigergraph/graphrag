
import logging
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

from pydantic import BaseModel, Field
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter


logger = logging.getLogger(__name__)

class QuestionRewriteResponse(BaseModel):
    rewritten_question: str = Field(description="The rewritten question.")

class TigerGraphAgentRewriter:
    def __init__(self, llm_model):
        self.llm = llm_model

    def rewrite_question(self, question: str) -> str:
        """Rewrite a new verison of the question.
        Args:
            question: str: The question to generate an answer for.
        Returns:
            str: The rewritten question.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY rewrite_question")

        rewrite_parser = PydanticOutputParser(pydantic_object=QuestionRewriteResponse)

        re_write_prompt = PromptTemplate(
            template="""You are a question re-writer that converts an input question to a better version that is optimized \
for AI agent question answering. Look at the initial and formulate an improved question but avoid to add unnecessary context for entities. \n
Here is the initial question:
{question}
Format your response in the following manner {format_instructions}""",
            input_variables=["question"],
            partial_variables={
                "format_instructions": rewrite_parser.get_format_instructions()
            }
        )


        generation = self.llm.invoke_with_parser(
            re_write_prompt, rewrite_parser,
            {"question": question}, caller_name="rewrite_question",
        )
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT rewrite_question")
        return generation.rewritten_question
