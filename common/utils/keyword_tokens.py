"""Tokenizer for BM25 keyword search over document chunks.

The graphrag service tokenizes each chunk's text when it builds a graph's
keyword index (``supportai.keyword_index``), and tokenizes the question the
same way at search time. Nothing is stored in the graph.

The tokenizer needs no dictionary: Latin words and numbers are kept whole, and
runs of Chinese, Japanese or Korean characters become overlapping character
pairs. Tokens keep every occurrence, in text order.
"""

import unicodedata

import regex

# Saved keyword indexes record this; an index from another version is built
# again. Change it whenever tokenize() changes its output.
TOKENIZER_VERSION = "cjk2-v1"

_CJK = r"\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}ー々"
_WORD_CHAR = rf"(?:(?![{_CJK}])[\p{{L}}\p{{N}}])"
_TOKEN = regex.compile(rf"[{_CJK}]+|{_WORD_CHAR}+(?:[._\-]{_WORD_CHAR}+)*")
_IS_CJK = regex.compile(rf"[{_CJK}]")

STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or s that "
    "the this to was were what when where which who why with".split()
)


def tokenize(text: str) -> list[str]:
    """Tokens of ``text`` in order, duplicates kept."""
    out: list[str] = []
    for match in _TOKEN.finditer(unicodedata.normalize("NFKC", text or "").lower()):
        word = match.group(0)
        if _IS_CJK.match(word):
            if len(word) == 1:
                out.append(word)
            else:
                out.extend(word[i : i + 2] for i in range(len(word) - 1))
        elif word not in STOPWORDS:
            out.append(word)
    return out
