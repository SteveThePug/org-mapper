"""Move detection: is the person who appeared at organisation B the one who left organisation A?"""

import re
import unicodedata

from rapidfuzz import fuzz

from radar.models import Person

MOVE_THRESHOLD = 0.95  # link automatically
POSSIBLE_THRESHOLD = 0.85  # ask a recruiter to confirm

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|esq|kc|qc|pc)\b\.?", re.I)


def name_key(name: str) -> str:
    """'José A. Smith, Jr.' -> 'jose smith' (accents, initials, suffixes removed)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = _SUFFIXES.sub(" ", ascii_name.lower())
    tokens = [t for t in re.split(r"[^a-z]+", ascii_name) if len(t) > 1]
    return " ".join(tokens)


def match_score(a: Person, b: Person) -> float:
    if not a.name or not b.name:
        return 0.0
    key_a, key_b = name_key(a.name), name_key(b.name)
    score = fuzz.token_set_ratio(key_a, key_b) / 100
    if key_a.split()[-1:] != key_b.split()[-1:]:
        score -= 0.1  # different surname is a strong negative signal
    corroboration = 0.0
    if a.seniority_year and b.seniority_year:
        corroboration += 0.05 if a.seniority_year == b.seniority_year else -0.15
    schools_a = {e.school.lower() for e in a.education if e.school}
    schools_b = {e.school.lower() for e in b.education if e.school}
    if schools_a & schools_b:
        corroboration += 0.05
    if set(a.groups) & set(b.groups):
        corroboration += 0.02
    # A name match alone tops out at 0.9 ("possible"); auto-linking needs corroboration.
    return max(0.0, min(1.0, score * 0.9 + corroboration))
