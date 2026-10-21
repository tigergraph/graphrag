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

"""The part of the conversation the agentic engine shows a model.

Triage, the planner, the reactive agent and the answer step all read the
conversation through this window, so they see the same, most recent turns.
"""

import json

# Conversation budget, in characters of the JSON sent.
CONVERSATION_CHARS = 4000


def recent_turns(convo, limit: int = CONVERSATION_CHARS) -> list:
    """The most recent turns that fit ``limit`` as JSON, oldest first.

    Follow-ups refer to the latest turns, so the budget is spent from the
    end. If even the newest turn is too long, its response is shortened and
    its question kept.
    """
    def dumps(turns):
        return json.dumps(turns, ensure_ascii=False)

    kept = []
    for turn in reversed(convo or []):
        if len(dumps([turn] + kept)) > limit:
            break
        kept.insert(0, turn)
    if not kept and convo:
        newest = dict(convo[-1])
        room = limit - len(dumps([{**newest, "response": ""}]))
        newest["response"] = str(newest.get("response", ""))[:max(room, 0)]
        # JSON escaping can still push it over; trim until it fits.
        while newest["response"] and len(dumps([newest])) > limit:
            newest["response"] = newest["response"][: int(len(newest["response"]) * 0.9)]
        kept = [newest]
    return kept


def recent_conversation(convo, limit: int = CONVERSATION_CHARS) -> str:
    """``recent_turns`` as the JSON text placed in a prompt."""
    return json.dumps(recent_turns(convo, limit), ensure_ascii=False)
