import unittest


class TestGoogleGenAFCDisabled(unittest.TestCase):
    """Google's SDK must not execute tools outside GraphRAG's tool loop."""

    @classmethod
    def setUpClass(cls):
        try:
            from langchain_core.messages import HumanMessage
            from common.llm_services.google_genai_service import (
                _ChatGoogleGenerativeAIWithoutAFC,
            )
        except ImportError as exc:
            raise unittest.SkipTest(f"Google Gen AI environment unavailable: {exc}")
        cls.message = HumanMessage(content="Hello")
        cls.model_class = _ChatGoogleGenerativeAIWithoutAFC

    def _request(self, **kwargs):
        model = self.model_class(
            model="gemini-3.5-flash",
            api_key="test-key",
        )
        return model._prepare_request([self.message], **kwargs)

    def test_afc_is_disabled_on_every_request(self):
        request = self._request()
        afc = request["config"].automatic_function_calling
        self.assertIsNotNone(afc)
        self.assertTrue(afc.disable)

    def test_caller_cannot_accidentally_reenable_afc(self):
        request = self._request(
            automatic_function_calling={"disable": False},
        )
        self.assertTrue(request["config"].automatic_function_calling.disable)


if __name__ == "__main__":
    unittest.main()
