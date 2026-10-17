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

"""Whether a graph query result holds any rows.

Shared by the agentic structural tool and the classic engine, so both treat
the same result the same way.
"""

import json
from typing import Any


def is_empty_result(result: Any) -> bool:
    """True when ``result`` holds no rows.

    Results arrive as JSON text (pyTigerGraph function calls) or as nested
    containers (openCypher, e.g. ``{"VS_XXX": []}``), so both are read
    through. A container is empty when every value in it is empty. A scalar
    such as a count of 0 is an answer, not an empty result.
    """
    if result is None:
        return True
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return True
        try:
            parsed = json.loads(text)
        except ValueError:
            return False
        if parsed is None or isinstance(parsed, (list, dict)):
            return is_empty_result(parsed)
        return False
    if isinstance(result, dict):
        return all(is_empty_result(v) for v in result.values())
    if isinstance(result, (list, tuple, set, frozenset, range)):
        return all(is_empty_result(v) for v in result)
    return False


def count_rows(result: Any) -> int:
    """How many rows a graph query result holds, for summaries.

    A list holds one row per item, except that a list of result blocks
    (``[{"VS": [...]}]``) is counted through. A dict holding lists
    (``{"VS_XXX": [...]}``) counts the list items; a dict of plain values
    (``{"T": {"n": 4}}``) is one row. JSON text is read first.
    """
    if result is None:
        return 0
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return 1 if result.strip() else 0
    if isinstance(result, list):
        if result and all(isinstance(r, dict) and any(isinstance(v, list) for v in r.values()) for r in result):
            return sum(count_rows(r) for r in result)
        return len(result)
    if isinstance(result, dict):
        lists = [v for v in result.values() if isinstance(v, list)]
        if lists:
            return sum(len(v) for v in lists)
        return 0 if is_empty_result(result) else 1
    return 1


def describe_rows(source: str, result: Any) -> str:
    """A one-line summary like "graph query returned 4 rows, 1,234 chars"."""
    n = count_rows(result)
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return f"{source} returned {n:,} row{'' if n == 1 else 's'}, {len(text):,} chars"
