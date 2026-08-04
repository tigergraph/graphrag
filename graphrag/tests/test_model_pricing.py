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

import unittest
from unittest.mock import patch

from common.llm_services import model_pricing as mp


FAKE_CATALOG = {
    "gpt-5.4": {
        "input_cost_per_token": 2.5e-6,
        "output_cost_per_token": 1.5e-5,
        "cache_read_input_token_cost": 2.5e-7,
    },
    "openai/gpt-5.4": {
        "input_cost_per_token": 2.5e-6,
        "output_cost_per_token": 1.5e-5,
        "cache_read_input_token_cost": 2.5e-7,
    },
    "gemini/gemini-2.0-flash": {
        "input_cost_per_token": 1e-7,
        "output_cost_per_token": 4e-7,
    },
    # Wrong provider prefix — must not be picked when provider=genai
    "openai/gemini-2.0-flash": {
        "input_cost_per_token": 9.0,
        "output_cost_per_token": 9.0,
    },
}


class TestModelPricing(unittest.TestCase):
    def setUp(self):
        mp.clear_pricing_cache()

    def tearDown(self):
        mp.clear_pricing_cache()

    def test_loads_only_configured_models(self):
        mp.ensure_model_rates(["gpt-5.4"], provider="openai", catalog=FAKE_CATALOG)
        self.assertEqual(set(mp._rates.keys()), {"openai:gpt-5.4"})

    def test_gpt54_cost_nonzero(self):
        mp.ensure_model_rates(["gpt-5.4"], provider="openai", catalog=FAKE_CATALOG)
        self.assertAlmostEqual(
            mp.estimate_cost("gpt-5.4", 1000, 100, provider="openai"), 0.004, places=9
        )

    def test_genai_uses_gemini_prefix(self):
        mp.ensure_model_rates(
            ["gemini-2.0-flash"], provider="genai", catalog=FAKE_CATALOG
        )
        cost = mp.estimate_cost(
            "gemini-2.0-flash", 1_000_000, 1_000_000, provider="genai"
        )
        self.assertAlmostEqual(cost, 0.10 + 0.40, places=9)

    def test_provider_prefix_preferred_over_wrong_alias(self):
        # Without provider we'd risk scanning; with genai we hit gemini/ first.
        rates = mp._lookup(FAKE_CATALOG, "gemini-2.0-flash", "genai")
        self.assertEqual(rates[0], 1e-7)

    def test_resolve_keeps_nonzero_langchain_cost(self):
        mp.ensure_model_rates(["gpt-5.4"], provider="openai", catalog=FAKE_CATALOG)
        self.assertEqual(
            mp.resolve_usage_cost(
                "gpt-5.4", 1000, 100, langchain_cost=0.123, provider="openai"
            ),
            0.123,
        )

    def test_resolve_falls_back_to_litellm_when_langchain_zero(self):
        mp.ensure_model_rates(["gpt-5.4"], provider="openai", catalog=FAKE_CATALOG)
        self.assertAlmostEqual(
            mp.resolve_usage_cost(
                "gpt-5.4", 1000, 100, langchain_cost=0.0, provider="openai"
            ),
            0.004,
            places=9,
        )

    def test_resolve_zero_when_both_unavailable(self):
        mp.ensure_model_rates(["unknown-model-xyz"], provider="openai", catalog={})
        self.assertEqual(
            mp.resolve_usage_cost(
                "unknown-model-xyz", 10, 5, langchain_cost=0.0, provider="openai"
            ),
            0.0,
        )

    @patch.object(mp, "_fetch")
    def test_no_refetch_when_cached(self, mock_fetch):
        mp.ensure_model_rates(["gpt-5.4"], provider="openai", catalog=FAKE_CATALOG)
        mock_fetch.reset_mock()
        mp.ensure_model_rates(["gpt-5.4"], provider="openai")
        mock_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
