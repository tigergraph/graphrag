class LLM_Model:
    """Base LLM_Model Class

    Used to connect to external LLM API services, and retrieve customized prompts for the tools.
    """

    def __init__(self, config):
        self.llm = None

    def _read_prompt_file(self, path):
        with open(path) as f:
            prompt = f.read()
        return prompt

    @property
    def map_question_schema_prompt(self):
        """Property to get the prompt for the MapQuestionToSchema tool."""
        raise ("map_question_schema_prompt not supported in base class")

    @property
    def generate_function_prompt(self):
        """Property to get the prompt for the GenerateFunction tool."""
        raise ("generate_function_prompt not supported in base class")

    @property
    def generate_cypher_prompt(self):
        """Property to get the prompt for the GenerateCypher tool."""
        prompt = """You're an expert in OpenCypher programming. Given the following schema: {schema}, what is the OpenCypher query that retrieves the {question}
                    Only include attributes that are found in the schema. Never include any attributes that are not found in the schema.
                    Use attributes instead of primary id if attribute name is closer to the keyword type in the question.
                    Use as less vertex type, edge type and attributes as possible. If an attribute is not found in the schema, please exclude it from the query.
                    Do not return attributes that are not explicitly mentioned in the question. If a vertex type is mentioned in the question, only return the vertex.
                    Never use directed edge pattern in the OpenCypher query. Always use and create query using undirected pattern.
                    Always use double quotes for strings instead of single quotes.

                    You cannot use the following clauses:
                    OPTIONAL MATCH
                    CREATE
                    MERGE
                    REMOVE
                    UNION
                    UNION ALL
                    UNWIND
                    SET

                    Make sure to have correct attribute names in the OpenCypher query and not to name result aliases that are vertex or edge types.

                    ONLY write the OpenCypher query in the response. Do not include any other information in the response."""
        return prompt

    @property
    def route_response_prompt(self):
        """Property to get the prompt for the RouteResponse tool."""
        prompt = """\
You are an expert at routing a user question to a vectorstore or function calls.
Use the vectorstore for questions on that would be best suited by text documents.
Use the function calls for questions that ask about structured data, or operations on structured data.
Keep in mind that some questions about documents such as "how many documents are there?" can be answered by function calls.
The function calls can be used to answer questions about these entities: {v_types} and relationships: {e_types}.
Otherwise, use vectorstore. Give a binary choice 'functions' or 'vectorstore' based on the question.
Return the a JSON with a single key 'datasource' and no premable or explaination.
Question to route: {question}
Format: {format_instructions}\
"""
        return prompt

    @property
    def hyde_prompt(self):
        """Property to get the prompt for the HyDE tool."""
        return """You are a helpful agent that is writing an example of a document that might answer this question: {question}
                  Answer:"""

    @property
    def entity_relationship_extraction_prompt(self):
        """Property to get the prompt for the EntityRelationshipExtraction tool."""
        raise ("entity_relationship_extraction_prompt not supported in base class")

    @property
    def supportai_response_prompt(self):
        """Property to get the prompt for the SupportAI response."""
        return "Answer this question: {question}\nUse this information: {sources}"

    @property
    def chatbot_response_prompt(self):
        """Property to get the prompt for the SupportAI response."""
        prompt ="""Given the answer context in JSON format, rephrase it to answer the question. \n
                   Use only the provided information in context without adding any reasoning or additional logic. \n
                   Make sure all information in the answer are covered in the generated answer.\n
                   Question: {question} \n
                   Answer: {context} \n
                   Format: {format_instructions}"""
        return prompt

    @property
    def keyword_extraction_prompt(self):
        """Property to get the prompt for the Question Expension response."""
        return """You are a helpful assistant responsible for extracting key terms (glossary) from all the questions below to represent their original meaning as much as possible. Each term should only contain a couple of words. Include a quality score for the each extracted glossary, based on how important and frequent it's in the given questions. The quality score should range from 0 (poor) to 100 (excellent), with higher scores indicating terms that are both significant and frequent in the context of the questions.\nThe output should only contain the extracted terms and their quality scores using the required format.\n\nQuestion: {question}\n\n{format_instructions}\n"""

    @property
    def question_expansion_prompt(self):
        """Property to get the prompt for the Question Expension response."""
        return """You are a helpful assistant responsible for generating 10 new questions similar to the original question below to represent its meaning in a more clear way.\nInclude a quality score for the answer, based on how well it represents the meaning of the original question. The quality score should be between 0 (poor) and 100 (excellent).\n\nQuestion: {question}\n\n{format_instructions}\n"""

    @property
    def graphrag_scoring_prompt(self):
        """Property to get the prompt for the GraphRAG Scoring response."""
        return """You are a helpful assistant responsible for generating an answer to the question below using the data provided.\nInclude a quality score for the answer, based on how well it answers the question. The quality score should be between 0 (poor) and 100 (excellent).\n\nQuestion: {question}\nContext: {context}\n\n{format_instructions}\n"""

    @property
    def model(self):
        """Property to get the external LLM model."""
        raise ("model not supported in base class")
