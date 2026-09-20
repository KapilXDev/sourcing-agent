"""The decision gate: every rejection here costs zero tokens.

This is the cheapest and most important stage in the system. Most postings a
crawl returns are disqualified for reasons that need no intelligence at all -
wrong seniority, wrong continent, no sponsorship, a title you would never take,
a job you already assessed last week. Spending a model call to discover that is
pure waste, and worse, it is a *non-reproducible* rejection: you cannot explain
six months later why a posting was dropped.

So the gate is pure Python. Deterministic, ordered, and fully explainable - each
rejection carries a stable rule id such as ``seniority:overqualified``. Only
survivors are allowed to cost money.

Rules are cheap-to-expensive: identity and recency checks fire before any string
scanning, and salary parsing runs last.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from .config import GateConfig, Profile
from .models import GateDecision, Posting

# --------------------------------------------------------------------------
# Lexicons
# --------------------------------------------------------------------------

_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|new ?grad|entry[- ]level|graduate|apprentice)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\b(staff)\b", re.I)),
    ("lead", re.compile(r"\b(lead|tech lead)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|experienced)\b", re.I)),
    ("executive", re.compile(r"\b(director|vp|vice president|head of|chief|cto|manager)\b", re.I)),
]

_NO_SPONSORSHIP = re.compile(
    r"(no\s+(visa\s+)?sponsorship|not\s+(able|willing)\s+to\s+sponsor|"
    r"without\s+(the\s+)?(need\s+for\s+)?sponsorship|"
    r"unable\s+to\s+(provide\s+)?sponsor|"
    r"do(es)?\s+not\s+(offer|provide)\s+sponsorship|"
    r"must\s+be\s+(a\s+)?(us|u\.s\.)\s+citizen|citizens?\s+only)",
    re.I,
)
_SPONSORSHIP_OFFERED = re.compile(r"(visa\s+sponsorship\s+(is\s+)?(available|offered|provided)|we\s+sponsor)", re.I)
_CLEARANCE = re.compile(r"\b(security\s+clearance|ts/sci|top\s+secret|polygraph)\b", re.I)
_ONSITE_ONLY = re.compile(r"\b(on-?site\s+only|in-?office\s+\d\s*days|no\s+remote|100%\s+on-?site)\b", re.I)
_REMOTE_HINT = re.compile(r"\b(remote|distributed|work\s+from\s+home|anywhere)\b", re.I)

# "Remote" is a working arrangement, not a place. Most remote postings still
# carry a geographic restriction in the same field - "Remote - US", "Remote
# (EU)", "Remote, Berlin" - so the marker has to be stripped off before what
# remains can be tested against the profile's eligible locations.
_REMOTE_MARKER = re.compile(
    r"\b(?:fully\s+|100%\s+)?(?:remote|distributed|telecommute|work\s+from\s+home|wfh)\b",
    re.I,
)
_UNRESTRICTED = re.compile(
    r"\b(?:anywhere|world\s*wide|global(?:ly)?|international|any\s+location)\b", re.I
)
_SEPARATORS = " \t,;:/|·-–—()[]"

# Profile entries that describe an arrangement rather than a place. Listing
# one imposes no geographic constraint, so it is not compiled into the
# eligible-location set - otherwise "Remote - Tokyo" would satisfy a profile
# that only said "remote", which is the bug this rule exists to prevent.
_ARRANGEMENT_WORDS = frozenset(
    {"remote", "anywhere", "worldwide", "global", "distributed", "work from home", "wfh", "hybrid"}
)

# Equivalence classes, applied in both directions: a profile saying "united
# states" matches a posting saying "US", and vice versa. Deliberately short -
# an over-eager table produces false eligibility, which is the expensive
# direction. Ambiguous abbreviations ("CA" is both California and Canada) are
# left out on purpose.
_LOCATION_SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset(
        {"united states", "united states of america", "usa", "u.s.", "u.s.a.", "us", "america"}
    ),
    frozenset({"united kingdom", "uk", "u.k.", "great britain", "britain", "england"}),
    frozenset({"european union", "eu", "europe", "emea"}),
    frozenset({"canada", "canadian"}),
    frozenset({"germany", "deutschland"}),
    frozenset({"netherlands", "holland"}),
    frozenset({"australia", "new zealand", "anz"}),
    frozenset({"latin america", "latam", "south america"}),
    frozenset({"asia pacific", "apac"}),
)


def expand_location(name: str) -> set[str]:
    """A location plus every form the boards write it in."""
    key = name.strip().lower()
    forms = {key}
    for group in _LOCATION_SYNONYMS:
        if key in group:
            forms |= set(group)
    return forms


def remote_residue(where: str) -> str:
    """What a location field says *besides* being remote.

    ``"Remote - US"`` -> ``"US"``; ``"Fully Remote (EU)"`` -> ``"EU"``;
    ``"Remote"`` -> ``""``. An empty residue means genuinely unrestricted.
    """
    collapsed = re.sub(r"\s+", " ", _REMOTE_MARKER.sub(" ", where))
    return collapsed.strip(_SEPARATORS).strip()

# $120,000 - $150,000  |  $120k-$150k  |  $120000 to $150000
# The k-suffixed form is tried first, or "$150k" would match as a bare "150"
# and the range half would be lost. A currency symbol is required on the low
# end: without it, "500,000 users" parses as a salary.
_NUM = r"(?:\d{1,3}(?:\.\d+)?\s?[kK]|\d{1,3}(?:,\d{3})+|\d{4,7}(?:\.\d+)?)"
_SALARY = re.compile(
    rf"[$€£]\s?({_NUM})(?:\s*(?:-|–|—|to|up to)\s*(?:[$€£]\s?)?({_NUM}))?"
)


def _money(token: str) -> int | None:
    """Parse ``150,000`` / ``150k`` / ``150.5k`` into an integer."""
    t = token.strip().replace(",", "")
    try:
        if t.lower().endswith("k"):
            return int(float(t[:-1].strip()) * 1000)
        value = int(float(t))
    except ValueError:
        return None
    # Bare 2-3 digit figures in a $ context are thousands ("$150" means $150k).
    if value < 1000:
        value *= 1000
    return value


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Best-effort salary range. Returns ``(None, None)`` when unsure.

    Deliberately conservative: hourly rates and equity figures are ignored, and
    anything outside a plausible annual band is discarded rather than guessed at.
    """
    best_low: int | None = None
    best_high: int | None = None
    for match in _SALARY.finditer(text):
        low = _money(match.group(1))
        high = _money(match.group(2)) if match.group(2) else low
        if low is None or high is None:
            continue
        if not (20_000 <= low <= 1_000_000) or not (20_000 <= high <= 1_000_000):
            continue
        if best_high is None or high > best_high:
            best_low, best_high = low, high
    return best_low, best_high


def detect_seniority(title: str) -> str:
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    return "mid"


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Word-boundary match so ``go`` does not fire on ``Google``."""
    escaped = re.escape(keyword.strip())
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![\w+#]){escaped}(?![\w+#])", re.I)


@dataclass
class _Rule:
    id: str
    fn: Callable[[Posting, str], str | None]


class DecisionGate:
    """Deterministic pre-filter. No network, no model, no randomness."""

    def __init__(
        self,
        profile: Profile,
        config: GateConfig,
        seen_keys: Iterable[str] = (),
        now: datetime | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.seen_keys = set(seen_keys)
        self.now = now or datetime.now(timezone.utc)

        self._titles = [_keyword_pattern(t) for t in profile.titles]
        self._exclude_titles = [_keyword_pattern(t) for t in profile.exclude_titles]
        self._must_have = [(k, _keyword_pattern(k)) for k in profile.must_have_any]
        self._nice = [(k, _keyword_pattern(k)) for k in profile.nice_to_have]
        self._exclude_kw = [(k, _keyword_pattern(k)) for k in profile.exclude_keywords]
        self._exclude_co = [c.lower().strip() for c in profile.exclude_companies]

        # Places only. Arrangement words ("remote") are dropped, and each real
        # place is expanded to the forms job boards actually write.
        geo = [
            form
            for loc in profile.locations
            if loc.strip().lower() not in _ARRANGEMENT_WORDS
            for form in sorted(expand_location(loc))
        ]
        self._locations = [_keyword_pattern(name) for name in geo]

        self.rules: list[_Rule] = [
            _Rule("dedupe:already_assessed", self._r_seen),
            _Rule("staleness:too_old", self._r_stale),
            _Rule("content:too_thin", self._r_thin),
            _Rule("company:excluded", self._r_company),
            _Rule("title:excluded", self._r_title_excluded),
            _Rule("title:no_match", self._r_title_match),
            _Rule("seniority:mismatch", self._r_seniority),
            _Rule("location:ineligible", self._r_location),
            _Rule("remote:onsite_only", self._r_remote),
            _Rule("authorization:no_sponsorship", self._r_sponsorship),
            _Rule("authorization:clearance_required", self._r_clearance),
            _Rule("keywords:excluded", self._r_excluded_keywords),
            _Rule("keywords:insufficient", self._r_keyword_hits),
            _Rule("comp:below_floor", self._r_salary),
        ]

        self._last_hits: list[str] = []
        self._last_missing: list[str] = []

    # -- public ------------------------------------------------------------

    def evaluate(self, posting: Posting) -> GateDecision:
        """Run every rule. All failures are collected, not just the first.

        Collecting them matters: 'rejected for three independent reasons' is a
        much stronger signal when tuning a profile than 'rejected'.
        """
        self._last_hits = []
        self._last_missing = []
        haystack = f"{posting.title}\n{posting.company}\n{posting.location or ''}\n{posting.description}"

        rejections: list[str] = []
        for rule in self.rules:
            outcome = rule.fn(posting, haystack)
            if outcome:
                rejections.append(outcome)

        passed = not rejections
        return GateDecision(
            passed=passed,
            rejections=rejections,
            prefilter_score=self.score(posting, haystack) if passed else 0,
            matched_keywords=list(self._last_hits),
            missing_required=list(self._last_missing),
        )

    def partition(
        self, postings: Iterable[Posting]
    ) -> tuple[list[tuple[Posting, GateDecision]], list[tuple[Posting, GateDecision]]]:
        """Split into ``(passed, rejected)``, passed sorted best-first."""
        passed: list[tuple[Posting, GateDecision]] = []
        rejected: list[tuple[Posting, GateDecision]] = []
        for p in postings:
            decision = self.evaluate(p)
            (passed if decision.passed else rejected).append((p, decision))
        passed.sort(key=lambda pair: pair[1].prefilter_score, reverse=True)
        return passed, rejected

    def score(self, posting: Posting, haystack: str | None = None) -> int:
        """Deterministic 0-100 ranking signal for postings that pass.

        Used only to order the queue, never to reject - ordering matters because
        a tight budget means the tail of the queue may never be reached.
        """
        text = haystack or f"{posting.title}\n{posting.description}"
        total = 0

        must_hits = sum(1 for _, pat in self._must_have if pat.search(text))
        if self._must_have:
            total += int(40 * min(1.0, must_hits / max(1, len(self._must_have))))
        else:
            total += 20

        nice_hits = sum(1 for _, pat in self._nice if pat.search(text))
        total += min(15, nice_hits * 5)

        if any(pat.search(posting.title) for pat in self._titles):
            total += 15

        age = posting.age_days(self.now)
        if age is None:
            total += 5
        elif age <= 3:
            total += 15
        elif age <= 7:
            total += 11
        elif age <= 14:
            total += 7
        elif age <= 30:
            total += 3

        if posting.remote or _REMOTE_HINT.search(text):
            total += 5

        low, high = parse_salary(text)
        if high is not None:
            floor = self.profile.min_base_salary
            if floor is None or high >= floor * 1.2:
                total += 10
            elif high >= floor:
                total += 5

        return max(0, min(100, total))

    # -- rules -------------------------------------------------------------

    def _r_seen(self, posting: Posting, _: str) -> str | None:
        return "dedupe:already_assessed" if posting.key in self.seen_keys else None

    def _r_stale(self, posting: Posting, _: str) -> str | None:
        age = posting.age_days(self.now)
        if age is not None and age > self.config.max_age_days:
            return f"staleness:too_old({int(age)}d)"
        return None

    def _r_thin(self, posting: Posting, _: str) -> str | None:
        if len(posting.description) < self.config.min_description_chars:
            return f"content:too_thin({len(posting.description)}c)"
        return None

    def _r_company(self, posting: Posting, _: str) -> str | None:
        company = posting.company.lower().strip()
        for excluded in self._exclude_co:
            if excluded and excluded in company:
                return f"company:excluded({posting.company})"
        return None

    def _r_title_excluded(self, posting: Posting, _: str) -> str | None:
        for pattern in self._exclude_titles:
            if pattern.search(posting.title):
                return "title:excluded"
        return None

    def _r_title_match(self, posting: Posting, _: str) -> str | None:
        if not self._titles:
            return None
        if any(pattern.search(posting.title) for pattern in self._titles):
            return None
        return "title:no_match"

    def _r_seniority(self, posting: Posting, _: str) -> str | None:
        wanted = {s.lower() for s in self.profile.seniority}
        if not wanted:
            return None
        level = detect_seniority(posting.title)
        if level in wanted:
            return None
        order = ["intern", "junior", "mid", "senior", "staff", "lead", "principal", "executive"]
        try:
            direction = (
                "overqualified"
                if order.index(level) < min(order.index(w) for w in wanted if w in order)
                else "underqualified"
            )
        except (ValueError, TypeError):
            direction = "mismatch"
        return f"seniority:{direction}({level})"

    def _r_location(self, posting: Posting, haystack: str) -> str | None:
        """Eligibility, not preference.

        The subtlety is remote work. Treating ``remote`` as a free pass - the
        obvious implementation - lets "Remote (EU only)" and "Remote, Tokyo"
        through a profile that can only work in the US, and they then cost
        money at every downstream stage. So the remote marker is stripped and
        whatever remains is tested: nothing left means genuinely unrestricted,
        anything left is a restriction that has to match.
        """
        if not self._locations:
            return None  # the profile states no geographic constraint

        where = posting.location or ""
        if _UNRESTRICTED.search(where):
            return None  # "Worldwide", "Anywhere" - open to everyone

        residue = remote_residue(where)
        is_remote = bool(posting.remote) or _REMOTE_MARKER.search(where) is not None

        if is_remote and not residue:
            return None  # plain "Remote", no place named

        target = residue or where
        if any(pattern.search(target) for pattern in self._locations):
            return None

        return f"location:ineligible({target or 'unspecified'})"

    def _r_remote(self, posting: Posting, haystack: str) -> str | None:
        if not self.profile.remote_only:
            return None
        if _ONSITE_ONLY.search(haystack):
            return "remote:onsite_only"
        if posting.remote is False:
            return "remote:onsite_only"
        if posting.remote is None and not _REMOTE_HINT.search(haystack):
            return "remote:not_advertised"
        return None

    def _r_sponsorship(self, posting: Posting, haystack: str) -> str | None:
        if not self.profile.work_authorization.needs_sponsorship:
            return None
        if _SPONSORSHIP_OFFERED.search(haystack):
            return None
        if _NO_SPONSORSHIP.search(haystack):
            return "authorization:no_sponsorship"
        return None

    def _r_clearance(self, posting: Posting, haystack: str) -> str | None:
        if self.profile.work_authorization.has_clearance:
            return None
        return "authorization:clearance_required" if _CLEARANCE.search(haystack) else None

    def _r_excluded_keywords(self, posting: Posting, haystack: str) -> str | None:
        hits = [kw for kw, pat in self._exclude_kw if pat.search(haystack)]
        return f"keywords:excluded({','.join(hits[:3])})" if hits else None

    def _r_keyword_hits(self, posting: Posting, haystack: str) -> str | None:
        if not self._must_have:
            return None
        hits = [kw for kw, pat in self._must_have if pat.search(haystack)]
        self._last_hits = hits
        self._last_missing = [kw for kw, _ in self._must_have if kw not in hits]
        needed = self.config.min_keyword_hits
        if len(hits) < needed:
            return f"keywords:insufficient({len(hits)}/{needed})"
        return None

    def _r_salary(self, posting: Posting, haystack: str) -> str | None:
        floor = self.profile.min_base_salary
        if floor is None:
            return None
        text = posting.compensation_raw or haystack
        low, high = parse_salary(text)
        if high is None:
            return None  # unstated comp is not a rejection
        if high < floor:
            return f"comp:below_floor({high}<{floor})"
        return None
