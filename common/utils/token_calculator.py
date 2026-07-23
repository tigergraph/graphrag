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

import logging
import tiktoken
import sys
from typing import List, Any, Optional

logger = logging.getLogger(__name__)

# Cache for TokenCalculator instances to avoid re-initialization
_token_calculator_cache: dict[tuple[str, int], 'TokenCalculator'] = {}

def get_token_calculator(token_limit: int = 0, model_name: str = None) -> 'TokenCalculator':
    """
    Factory function to get or create a TokenCalculator instance.
    Reuses existing instances with the same model_name and token_limit to avoid re-initialization.

    Args:
        token_limit: Maximum number of tokens allowed for retrieved context
        model_name: Name of the model to use for token counting

    Returns:
        TokenCalculator instance (cached if parameters match)
    """
    model_name = model_name if model_name else "gpt-4"
    token_limit = token_limit if token_limit else 0
    cache_key = (model_name, token_limit)

    if cache_key not in _token_calculator_cache:
        _token_calculator_cache[cache_key] = TokenCalculator(token_limit=token_limit, model_name=model_name)
        logger.debug(f"Created new TokenCalculator instance for model={model_name}, token_limit={token_limit}")
    else:
        logger.debug(f"Reusing cached TokenCalculator instance for model={model_name}, token_limit={token_limit}")

    return _token_calculator_cache[cache_key]

class TokenCalculator:
    """Utility class for token counting and text truncation operations."""

    def __init__(self, token_limit: int = 0, model_name: str = None):
        """
        Initialize the token calculator.

        Args:
            token_limit: Maximum number of tokens allowed for retrieved context
            model_name: Name of the model to use for token counting
                               Use <= 0 for unlimited tokens (no truncation).
        """
        self.max_context_tokens = token_limit if token_limit else 0
        self.model_name = model_name if model_name else "gpt-4"
        try:
            self.token_encoding = tiktoken.encoding_for_model(self._normalize_model_name(self.model_name))
        except Exception as e:
            self.token_encoding = tiktoken.get_encoding("cl100k_base")
            logger.info(f"No tiktoken mapping for model {self.model_name}, using cl100k_base")
        logger.info(f"Initialized TokenCalculator with max_context_tokens: {self.max_context_tokens} and encoding: {self.token_encoding}")

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        """Normalize provider-specific model names for tiktoken lookup.

        Examples:
            anthropic.claude-3-5-haiku-20241022-v1:0 → claude-3-5-haiku
            us.anthropic.claude-3-5-haiku-20241022-v1:0 → claude-3-5-haiku
            gpt-4o-mini → gpt-4o-mini (unchanged)
        """
        name = model_name
        # Strip Bedrock provider prefix (e.g., "anthropic." or "us.anthropic.")
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        # Strip version suffix (e.g., "-20241022-v1:0")
        # Pattern: date stamp followed by version
        import re
        name = re.sub(r'-\d{8}-v\d+.*$', '', name)
        return name

    def set_max_context_tokens(self, max_tokens: int):
        """Set the maximum number of tokens allowed for retrieved context."""
        self.max_context_tokens = max_tokens
        if self.is_unlimited_tokens():
            logger.info("Set token limit to unlimited (no truncation)")
        else:
            logger.info(f"Set max context tokens to: {max_tokens}")

    def get_max_context_tokens(self) -> int:
        """Get the current maximum number of tokens allowed for retrieved context."""
        return self.max_context_tokens if not self.is_unlimited_tokens() else sys.maxsize

    def is_unlimited_tokens(self) -> bool:
        """Check if token limit is set to unlimited."""
        return (self.max_context_tokens <= 0)

    def count_tokens(self, text: str | dict) -> int:
        """Count the number of tokens in the given text."""
        try:
            if not isinstance(text, str):
                text = str(text)
            return len(self.token_encoding.encode(text))
        except Exception as e:
            logger.warning(f"Error counting tokens: {e}, using character-based estimation")
            # Fallback: rough estimation (1 token ≈ 4 characters for English text)
            return len(text) // 4

    def truncate_dict_to_token_limit(self, sources_dict: dict, max_tokens: Optional[int] = None) -> dict:
        """
        Truncate dictionary to fit within the token limit by keeping original values
        until hitting the max_tokens limit, then ignoring remaining values.

        Args:
            sources_dict: Dictionary to truncate
            max_tokens: Maximum number of tokens allowed (defaults to self.max_context_tokens)

        Returns:
            Dictionary of sources that fit within the token limit
        """
        if max_tokens is None:
            max_tokens = self.max_context_tokens

        if not sources_dict:
            return sources_dict

        total_tokens = self.count_tokens(sources_dict)

        # If unlimited tokens is enabled, return all sources without truncation
        if self.is_unlimited_tokens() or max_tokens <= 0 or total_tokens <= max_tokens:
            return sources_dict

        # Convert dict to list of (key, value) pairs for processing
        items = sorted(sources_dict.items(), key=lambda x: (".png" not in x, -len(x)))
        truncated_sources = {}
        current_tokens = 0

        for key, value in items:
            # Calculate tokens for this key-value pair
            item_tokens = self.count_tokens({key: value})

            # Check if adding this item would exceed the limit
            if current_tokens + item_tokens <= max_tokens:
                # Add the complete item
                truncated_sources[key] = value
                current_tokens += item_tokens
                logger.debug(f"Added complete item '{key}' ({item_tokens} tokens, total: {current_tokens})")
            else:
                # Check if we can add a partial version of this item
                remaining_tokens = max_tokens - current_tokens
                if remaining_tokens > 0:
                    # Try to add a truncated version of this item
                    if isinstance(value, str):
                        # Truncate string to fit remaining tokens
                        truncated_value = self.truncate_text_to_token_limit(value, remaining_tokens)
                        if truncated_value:
                            truncated_sources[key] = truncated_value
                            current_tokens += self.count_tokens(truncated_value)
                            logger.debug(f"Added truncated string '{key}' ({self.count_tokens(truncated_value)} tokens, total: {current_tokens})")
                    elif isinstance(value, list):
                        # Add as many list items as possible
                        truncated_list = []
                        for item in value:
                            if isinstance(item, str):
                                item_tokens = self.count_tokens(item)
                                if current_tokens + item_tokens <= max_tokens:
                                    truncated_list.append(item)
                                    current_tokens += item_tokens
                                else:
                                    # Try to add a truncated version of this item
                                    remaining = max_tokens - current_tokens
                                    if remaining > 0:
                                        truncated_item = self.truncate_text_to_token_limit(item, remaining)
                                        if truncated_item:
                                            truncated_list.append(truncated_item)
                                            current_tokens += self.count_tokens(truncated_item)
                                    break
                            else:
                                # For non-string items, add if there's space
                                item_tokens = self.count_tokens(item)
                                if current_tokens + item_tokens <= max_tokens:
                                    truncated_list.append(item)
                                    current_tokens += item_tokens
                                else:
                                    break
                        if truncated_list:
                            truncated_sources[key] = truncated_list
                            logger.debug(f"Added truncated list '{key}' ({len(truncated_list)} items, total: {current_tokens})")
                    elif isinstance(value, dict):
                        # Recursively truncate sub-dictionary
                        remaining = max_tokens - current_tokens
                        if remaining > 0:
                            truncated_subdict = self.truncate_dict_to_token_limit(value, remaining)
                            if truncated_subdict:
                                truncated_sources[key] = truncated_subdict
                                current_tokens += self.count_tokens(truncated_subdict)
                                logger.debug(f"Added truncated sub-dict '{key}' ({self.count_tokens(truncated_subdict)} tokens, total: {current_tokens})")
                    else:
                        # For other types, add if there's space
                        if current_tokens + item_tokens <= max_tokens:
                            truncated_sources[key] = value
                            current_tokens += item_tokens
                            logger.debug(f"Added complete non-string item '{key}' ({item_tokens} tokens, total: {current_tokens})")
                else:
                    # No more space, stop processing
                    logger.debug(f"Stopping truncation - no more space for item '{key}'")
                    break

        logger.info(f"Final truncated context tokens: {current_tokens} (limit: {max_tokens})")
        return truncated_sources

    def truncate_text_to_token_limit(self, text: str, max_tokens: Optional[int] = None) -> str:
        """
        Truncate text to fit within the specified token limit.

        Args:
            text: Text to truncate
            max_tokens: Maximum number of tokens allowed

        Returns:
            Truncated text
        """
        if max_tokens is None:
            max_tokens = self.max_context_tokens

        try:
            tokens = self.token_encoding.encode(text)
            if len(tokens) <= max_tokens:
                return text

            # Truncate to max_tokens and decode back to text
            truncated_tokens = tokens[:max_tokens]
            truncated_text = self.token_encoding.decode(truncated_tokens)

            # Add ellipsis to indicate truncation
            #if len(tokens) > max_tokens:
            #    truncated_text += "..."

            return truncated_text
        except Exception as e:
            logger.warning(f"Error truncating text: {e}, using character-based truncation")
            # Fallback: rough estimation (1 token ≈ 4 characters)
            max_chars = max_tokens * 4
            if len(text) <= max_chars:
                return text
            return text[:max_chars] #+ "..."

    def truncate_to_token_limit(self, text: str | dict, max_tokens: Optional[int] = None) -> str:
        """
        Truncate text to fit within the specified token limit.

        Args:
            text: Text to truncate
            max_tokens: Maximum number of tokens allowed

        Returns:
            Truncated text
        """
        if isinstance(text, dict):
            return self.truncate_dict_to_token_limit(text, max_tokens)
        else:
            return self.truncate_text_to_token_limit(text, max_tokens)

