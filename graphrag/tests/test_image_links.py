"""Image links in final answers: every engine returns links the chat UI can
load (tg:// -> /ui/image_vertex/<graph>/<id>; S3 presigned on AWS Bedrock)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from common.py_schemas import GraphRAGResponse
from common.utils import image_links

TG_ANSWER = "Sales rose. ![Bar chart of sales](tg://report_pdf_image_1) See also ![x](https://e.com/a.png)."
CONVERTED = "![Bar chart of sales](/ui/image_vertex/G/report_pdf_image_1)"


class TestLinkRewrite(unittest.TestCase):
    def test_tg_links_become_the_image_endpoint(self):
        out = image_links.resolve_image_links(TG_ANSWER, "G")
        self.assertIn(CONVERTED, out)
        self.assertNotIn("tg://", out)
        self.assertIn("![x](https://e.com/a.png)", out)

    def test_text_without_images_is_unchanged(self):
        self.assertEqual(image_links.resolve_image_links("No images.", "G"), "No images.")
        self.assertIsNone(image_links.resolve_image_links(None, "G"))

    def test_s3_links_are_presigned_only_on_bedrock(self):
        from common.llm_services.aws_bedrock_service import AWSBedrock
        text = "![p](s3://bucket/k/img.png)"
        client = mock.Mock()
        client.generate_presigned_url.return_value = "https://signed/url"
        with mock.patch("boto3.client", return_value=client):
            bedrock = AWSBedrock.__new__(AWSBedrock)
            self.assertEqual(image_links.resolve_image_links(text, "G", bedrock), "![p](https://signed/url)")
            self.assertEqual(image_links.resolve_image_links(text, "G", object()), text)

    def test_a_link_that_cannot_be_signed_stays(self):
        client = mock.Mock()
        client.generate_presigned_url.side_effect = RuntimeError("no creds")
        with mock.patch("boto3.client", return_value=client):
            self.assertEqual(image_links.presign_s3_urls("(s3://b/k.png)"), "(s3://b/k.png)")


class TestClassicEngine(unittest.TestCase):
    def test_answer_links_are_rewritten(self):
        from agent import agent_graph
        g = agent_graph.TigerGraphAgentGraph.__new__(agent_graph.TigerGraphAgentGraph)
        g.llm_provider = object()
        g.db_connection = SimpleNamespace(graphname="G")
        g.q = None
        gen = mock.Mock()
        gen.generate_answer.return_value = SimpleNamespace(generated_answer=TG_ANSWER)
        state = {"lookup_source": "inquiryai", "question": "q", "context": {"result": []},
                 "error_history": []}
        with mock.patch.object(agent_graph, "TigerGraphAgentGenerator", return_value=gen):
            out = g.generate_answer(state)["answer"]
        self.assertIn(CONVERTED, out.natural_language_response)
        self.assertNotIn("tg://", out.natural_language_response)


class TestAgenticEngine(unittest.TestCase):
    def _agent(self, style):
        from agent import agentic_agent as aa
        a = aa.AgenticAgent.__new__(aa.AgenticAgent)
        a.agent_style = style
        a.conn = SimpleNamespace(graphname="G", username="u")
        a.llm = SimpleNamespace(config={})
        a.model_name = "m"
        a.embedding_model = a.embedding_store = a.mq2s = a.gen_func = a.cypher_gen = None
        a.use_cypher = False
        a.q = None
        a._ws = None
        return aa, a

    def test_planned_and_reactive_answers_are_rewritten(self):
        for style, runner in (("planned", "run_agentic"), ("reactive", "run_react")):
            with self.subTest(style=style):
                aa, agent = self._agent(style)
                resp = GraphRAGResponse(natural_language_response=TG_ANSWER, answered_question=True,
                                        response_type="agentic", query_sources={})
                with mock.patch.object(aa, "_triage_question", return_value=None), \
                     mock.patch.object(aa, "get_graphrag_config", return_value={}), \
                     mock.patch.object(aa, runner, return_value=resp):
                    out = agent.question_for_agent("q")
                self.assertIn(CONVERTED, out.natural_language_response)
                self.assertNotIn("tg://", out.natural_language_response)


if __name__ == "__main__":
    unittest.main()
