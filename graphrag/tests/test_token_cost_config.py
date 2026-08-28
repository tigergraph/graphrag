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

from common.llm_services.base_llm import estimate_cost_from_config, _record_usage


class TestTokenCostConfig(unittest.TestCase):
    def test_none_when_rates_missing(self):
        self.assertIsNone(estimate_cost_from_config({}, 1000, 100))
        self.assertIsNone(
            estimate_cost_from_config({"input_cost_per_1m": 2.5}, 1000, 100)
        )
        self.assertIsNone(
            estimate_cost_from_config({"output_cost_per_1m": 15.0}, 1000, 100)
        )

    def test_computes_from_per_1m_rates(self):
        # 1M in @ $2.50 + 1M out @ $15.00 = $17.50
        cost = estimate_cost_from_config(
            {"input_cost_per_1m": 2.5, "output_cost_per_1m": 15.0},
            1_000_000,
            1_000_000,
        )
        self.assertAlmostEqual(cost, 17.5, places=9)

    def test_small_token_counts(self):
        # 1000 in @ $2.50/1M + 100 out @ $15/1M = 0.0025 + 0.0015 = 0.004
        cost = estimate_cost_from_config(
            {"input_cost_per_1m": "2.5", "output_cost_per_1m": "15"},
            1000,
            100,
        )
        self.assertAlmostEqual(cost, 0.004, places=9)

    def test_record_usage_overrides_langchain(self):
        usage = {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cost": 0.123,  # LangChain non-zero — still overridden
        }
        _record_usage(
            "test",
            usage,
            {"input_cost_per_1m": 2.5, "output_cost_per_1m": 15.0},
        )
        self.assertAlmostEqual(usage["cost"], 0.004, places=9)

    def test_record_usage_keeps_langchain_when_unconfigured(self):
        usage = {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cost": 0.123,
        }
        _record_usage("test", usage, {"llm_model": "gpt-5.4"})
        self.assertEqual(usage["cost"], 0.123)


if __name__ == "__main__":
    unittest.main()
