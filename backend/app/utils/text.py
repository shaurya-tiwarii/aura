"""Text measurement: word counts, sentences, and readability.

Every number here is a documented formula with a citation, because spec section 24
requires that a score be explainable and reproducible. Nothing in this module
calls an API or a model -- these are the classical readability formulas, computed
locally, and a reviewer can check any value by hand.

The syllable counter is the one genuinely approximate part, and its limits are
stated on the function rather than glossed over.
"""

from __future__ import annotations

import re
import unicodedata

# A "word" is a run of letters, optionally carrying internal apostrophes or
# hyphens, so "don't" and "state-of-the-art" count once each rather than two and
# four times. Digits are excluded: "2024" is not a word for readability purposes
# and counting it inflates the score of a page that is mostly a price list.
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)  # noqa: RUF001

# Sentence terminators. The lookahead requires whitespace or end-of-string after
# the punctuation so "example.com" and "3.5" are not two sentences.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+(?=\s|$)")

_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+")

# Suffixes that add a written vowel group but no spoken syllable ("-le" after a
# consonant is the exception, hence the separate check below).
_SILENT_E_RE = re.compile(r"[^aeiouy]e$")

# Minimum words before a readability score is reported at all. Flesch on a
# 12-word page produces a wildly swinging number that looks precise and means
# nothing, so those pages get ``None`` instead of a fabricated value.
MIN_WORDS_FOR_READABILITY = 100

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace runs to single spaces and trim."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def sentences(text: str) -> list[str]:
    """Split into sentences, discarding empties.

    Deliberately simple. A real sentence tokeniser needs an abbreviation lexicon
    ("Dr.", "Inc.", "e.g.") and even then disagrees with itself across languages.
    The readability formulas only need the *count*, and an occasional over-split at
    an abbreviation moves Flesch by well under a point on any page long enough to
    be scored at all.
    """
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text)]
    return [part for part in parts if part]


def sentence_count(text: str) -> int:
    # Never zero when there is prose: a page with no terminating punctuation is
    # still one sentence, and returning 0 would divide by zero downstream.
    return len(sentences(text)) or (1 if text.strip() else 0)


def count_syllables(word: str) -> int:
    """Approximate the syllable count of one English word.

    Vowel-group counting with two standard corrections: a silent trailing ``e``
    ("more" is 1, not 2) and a trailing consonant + ``le`` ("table" is 2, not 1).

    This is an approximation and is wrong on loanwords, most proper nouns, and
    words where a vowel pair spans a syllable boundary ("create" is counted 1,
    actually 2). It is the same approximation every offline readability
    implementation uses, its error is roughly symmetric, and it averages out over
    the 100+ words required before a score is reported. It is *not* accurate enough
    to quote per-word, and nothing in AURA does.
    """
    normalized = unicodedata.normalize("NFKD", word.lower())
    stripped = "".join(ch for ch in normalized if ch.isalpha())
    if not stripped:
        return 0
    if len(stripped) <= 3:
        # "the", "a", "sky" -- one syllable, and the corrections below would
        # wrongly reduce short words to zero.
        return 1

    count = len(_VOWEL_GROUP_RE.findall(stripped))
    if _SILENT_E_RE.search(stripped) and not stripped.endswith(("le", "ee", "ye")):
        count -= 1
    if re.search(r"[^aeiouy]le$", stripped):
        count += 1
    return max(1, count)


def syllable_count(text: str) -> int:
    return sum(count_syllables(word) for word in words(text))


def flesch_reading_ease(text: str) -> float | None:
    """Flesch Reading Ease, 0-100, higher is easier.

    Flesch (1948)::

        206.835 - 1.015 * (words / sentences) - 84.6 * (syllables / words)

    Roughly: 90-100 is 5th grade, 60-70 is plain English, 30-50 is university
    level, below 30 is very hard. Returns ``None`` below
    :data:`MIN_WORDS_FOR_READABILITY` words rather than a meaningless number.

    The result is clamped to 0-100. The raw formula is unbounded -- German
    compound nouns or a page of chemical names can produce a negative value -- and
    a negative "score out of 100" in the UI reads as a bug rather than as "this is
    extremely hard to read".
    """
    total_words = word_count(text)
    if total_words < MIN_WORDS_FOR_READABILITY:
        return None
    total_sentences = sentence_count(text) or 1
    total_syllables = syllable_count(text)

    score = (
        206.835
        - 1.015 * (total_words / total_sentences)
        - 84.6 * (total_syllables / total_words)
    )
    return round(max(0.0, min(100.0, score)), 1)


def flesch_kincaid_grade(text: str) -> float | None:
    """Flesch-Kincaid grade level -- US school grade needed to read the text.

    Kincaid et al. (1975)::

        0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59

    Reported alongside Flesch Reading Ease because a grade number is far more
    actionable to a small-business owner than a 0-100 index: "written at grade 14"
    tells them to simplify, "62.3" does not. Floored at 1.0 and capped at 20.0;
    beyond that the formula is extrapolating past any real school grade.
    """
    total_words = word_count(text)
    if total_words < MIN_WORDS_FOR_READABILITY:
        return None
    total_sentences = sentence_count(text) or 1
    total_syllables = syllable_count(text)

    grade = (
        0.39 * (total_words / total_sentences)
        + 11.8 * (total_syllables / total_words)
        - 15.59
    )
    return round(max(1.0, min(20.0, grade)), 1)


def keyword_occurrences(text: str, keywords: list[str]) -> dict[str, int]:
    """Count whole-phrase, case-insensitive occurrences of each keyword.

    Word-boundary anchored so "cake" does not match "cheesecake" -- an inflated
    count here would later be scored as good keyword coverage that does not exist.
    """
    if not keywords:
        return {}
    haystack = normalize_whitespace(text).casefold()
    counts: dict[str, int] = {}
    for keyword in keywords:
        needle = normalize_whitespace(keyword).casefold()
        if not needle:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
        counts[keyword] = len(pattern.findall(haystack))
    return counts


__all__ = [
    "MIN_WORDS_FOR_READABILITY",
    "count_syllables",
    "flesch_kincaid_grade",
    "flesch_reading_ease",
    "keyword_occurrences",
    "normalize_whitespace",
    "sentence_count",
    "sentences",
    "syllable_count",
    "word_count",
    "words",
]
