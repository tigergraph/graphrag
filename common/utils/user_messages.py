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

"""Chat messages for a question GraphRAG could not answer.

One wording for every engine and endpoint, so a user who gets no answer is
always told how to ask again.
"""

RETRY_HINT = (
    "Try rephrasing the question with more specific information or a narrower "
    "scope, such as the exact name, time period, or document you mean."
)

# No answer was found for the question.
NO_ANSWER = "I couldn't find an answer to this question in the knowledge graph. " + RETRY_HINT

# Generating the answer failed.
ANSWER_FAILED = (
    "I wasn't able to generate an answer for this question. Try asking again. " + RETRY_HINT
)
