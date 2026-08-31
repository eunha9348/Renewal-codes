"""
time_parsing.py  (Career Analysis AI v1.2)
============================================================================
경력 텍스트의 시간 표현 파서

v1.2 에서 신규 추가된 모듈이다.

career_individual.py 가 "이 경험이 언제, 얼마나"를 스스로 계산하기 위한 모듈.
LLM 에게 시간 계산을 맡기지 않고 여기서 전부 확정한 뒤 사실로 전달한다.

────────────────────────────────────────────────────────────────────────
v1.2 에서 해결한 기존 한계
────────────────────────────────────────────────────────────────────────
기존(v1.1) 파서는 정규식 두 개로 1950~2049 범위의 `YYYY` / `YYYY.MM` 만
인식했다. 그 결과:

  - "재직 3년차", "3년 전", "작년 하반기" → 전부 인식 실패
  - 인식 실패 시 unresolved=True 로 처리되어, 프롬프트가 모델에게
    "기간 판단을 하지 말라"고 지시 → 기간_문제 진단이 통째로 비어버림
  - "2021.03~2021.08" 을 두 개의 개별 연월로만 읽어, 정작 중요한
    '5개월 근무' 라는 사실은 계산하지 못함

이 모듈은 아래를 모두 절대 시점/기간으로 해석한다:

  [절대]   2021년 3월 · 2021.03 · 2021-03 · 2021/03 · 2021년 · '21년 3월
  [영문]   Mar 2021 · March 2021 · 2021-03 · Since 2021 · Present
  [상대]   작년 · 재작년 · 올해 · 3년 전 · 6개월 전 · 지난달
  [기간]   1년 6개월 · 6개월간 · 2년째 · 재직 3년차 · 세 달
  [구간]   2021.03 ~ 2021.08 · 2019년~현재 · 2020부터 2022까지
  [분기]   2021년 상반기 · 2021년 1분기 · 2021년 2학기
  [진행]   현재 · 재직중 · 진행중 · present · now

해석 우선순위:
  1) 절대 시점(anchor)  — 가장 신뢰도 높음
  2) 상대 시점          — 기준일로부터 역산해 절대 시점으로 변환
  3) 기간 표현          — 시점은 몰라도 '얼마나' 는 확정 가능
  4) 아무것도 없음      — 이때만 unresolved
============================================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta


__version__ = "1.2.0"


# ══════════════════════════════════════════════
# 공용 날짜 유틸
# ══════════════════════════════════════════════
def add_months(d: datetime, months: int) -> datetime:
    """월말 보정 포함 개월 수 가산 (2026-01-31 +1개월 → 2026-02-28)."""
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    if month == 12:
        last_day = 31
    else:
        last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).day
    return d.replace(year=year, month=month, day=min(d.day, last_day))


def months_between(y1: int, m1: int, y2: int, m2: int) -> int:
    """(y1,m1) → (y2,m2) 개월 차이. 음수 가능."""
    return (y2 - y1) * 12 + (m2 - m1)


def fmt_duration(months: int) -> str:
    """개월 수를 'N년 M개월' 형태로."""
    months = max(int(months), 0)
    y, m = divmod(months, 12)
    if y and m:
        return f"{y}년 {m}개월"
    if y:
        return f"{y}년"
    return f"{m}개월"


# ══════════════════════════════════════════════
# 데이터 구조
# ══════════════════════════════════════════════
@dataclass
class TimeAnchor:
    """텍스트에서 확인된 하나의 시점."""
    text: str                 # 원문에서 매칭된 표현
    year: int
    month: int | None = None  # 월을 모르면 None
    kind: str = "absolute"    # absolute | relative | ongoing | inferred
    confidence: str = "high"  # high | medium | low

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.year, self.month or 0)

    def label(self) -> str:
        return f"{self.year}년 {self.month}월" if self.month else f"{self.year}년"


@dataclass
class DurationHint:
    """텍스트에서 확인된 기간(얼마나)."""
    text: str
    months: int
    kind: str          # explicit | nth_year | range | ongoing_range
    confidence: str = "high"


@dataclass
class TimeFacts:
    """파서의 최종 산출물. 프롬프트에는 facts 만 전달된다."""
    anchors: list[TimeAnchor] = field(default_factory=list)
    durations: list[DurationHint] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    earliest: TimeAnchor | None = None
    latest: TimeAnchor | None = None
    ongoing: bool = False
    future_mentions: list[str] = field(default_factory=list)

    months_since_latest: int | None = None
    total_duration_months: int | None = None
    resolution: str = "none"   # absolute | relative | duration_only | none

    @property
    def unresolved(self) -> bool:
        """시점도 기간도 하나도 못 찾았을 때만 True."""
        return self.resolution == "none"

    def to_dict(self) -> dict:
        return {
            "resolution": self.resolution,
            "facts": self.facts,
            "warnings": self.warnings or None,
            "ongoing": self.ongoing,
            "earliest": self.earliest.label() if self.earliest else None,
            "latest": self.latest.label() if self.latest else None,
            "months_since_latest": self.months_since_latest,
            "total_duration_months": self.total_duration_months,
            "total_duration_label": (
                fmt_duration(self.total_duration_months)
                if self.total_duration_months is not None else None
            ),
        }


# ══════════════════════════════════════════════
# 패턴 정의
# ══════════════════════════════════════════════
_Y = r"(?:19[5-9]\d|20[0-4]\d)"          # 1950~2049
_M = r"(?:0?[1-9]|1[0-2])"

# 2021년 3월 / 2021.03 / 2021-3 / 2021/03
_RE_YM = re.compile(rf"(?<!\d)({_Y})\s*[.\-/년]\s*({_M})\s*월?(?!\s*\d*\s*(?:년|개월|차|째))(?!\d)")

# 2021년 / 2021 (숫자 단위가 뒤따르면 제외: 2021명, 2021원 …)
_RE_Y = re.compile(rf"(?<![\d.])({_Y})\s*년?(?![\d.])(?!\s*(?:명|원|개|건|회|위|등|배|억|만|천))")

# '21년 3월 / '21.3  (2자리 연도 — 반드시 따옴표나 '년' 이 있어야 인식)
_RE_YY_M = re.compile(rf"['’](\d{{2}})\s*[.\-/년]\s*({_M})\s*월?(?!\d)")
_RE_YY = re.compile(r"['’](\d{2})\s*년?(?!\d)|(?<!\d)(\d{2})\s*년(?!\s*\d*\s*(?:차|째|간|동안|근무|재직))(?!\d)")

# 영문 월
_EN_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_RE_EN_MY = re.compile(
    rf"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*({_Y})\b",
    re.IGNORECASE,
)
_RE_EN_YM = re.compile(
    rf"\b({_Y})\s*[.\-/]\s*({_M})\b(?!\s*[.\-/]\s*\d)",
)

# 한글 수사 (기간 표현에만 제한적으로 사용)
_KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
           "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
_NUM = r"(?:\d{1,3}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"


def _num(token: str) -> int:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _KO_NUM.get(token, 0)


# 상대 시점
_RE_YEARS_AGO = re.compile(rf"({_NUM})\s*년\s*(?:전|앞)")
_RE_MONTHS_AGO = re.compile(rf"({_NUM})\s*(?:개월|달)\s*전")
_RE_LAST_YEAR = re.compile(r"작년|지난\s*해|전년도")
_RE_TWO_YEARS_AGO = re.compile(r"재작년|재재작년")
_RE_THIS_YEAR = re.compile(r"올해|금년|당해\s*연도|올\s*한\s*해")
_RE_NEXT_YEAR = re.compile(r"내년|명년|차년도")
_RE_LAST_MONTH = re.compile(r"지난\s*달|저번\s*달|전월")
_RE_THIS_MONTH = re.compile(r"이번\s*달|이달|금월")

# 기간
_RE_DUR_YM = re.compile(rf"({_NUM})\s*년\s*({_NUM})\s*(?:개월|달)")
_RE_DUR_Y = re.compile(rf"({_NUM})\s*년\s*(?:간|동안|째|여|남짓)")
_RE_DUR_M = re.compile(rf"({_NUM})\s*(?:개월|달)\s*(?:간|동안|째|여|남짓)?")
_RE_DUR_W = re.compile(rf"({_NUM})\s*주\s*(?:간|동안)?")
_RE_NTH_YEAR = re.compile(rf"({_NUM})\s*년\s*차")

# 진행 중
_RE_ONGOING = re.compile(
    r"현재|재직\s*중|근무\s*중|진행\s*중|수행\s*중|재학\s*중|\bpresent\b|\bcurrent\b|\bnow\b|\bongoing\b",
    re.IGNORECASE,
)

# 구간 구분자
_RE_RANGE_SEP = re.compile(r"^\s*(?:~|∼|～|-|–|—|부터|to|until|까지|―)\s*(?:부터|까지)?\s*$", re.IGNORECASE)

# 상대 연도 + 반기/분기 결합 ("작년 하반기", "올해 1분기")
_REL_YEAR_OFFSET = {"작년": -1, "지난해": -1, "전년도": -1, "재작년": -2,
                    "올해": 0, "금년": 0, "올": 0}
_RE_REL_HALF = re.compile(r"(작년|지난\s*해|전년도|재작년|올해|금년)\s*(상|하)반기")
_RE_REL_QUARTER = re.compile(r"(작년|지난\s*해|전년도|재작년|올해|금년)\s*([1-4])\s*분기")

# 반기/분기/학기
_RE_HALF = re.compile(rf"({_Y})\s*년?\s*(상|하)반기")
_RE_QUARTER = re.compile(rf"({_Y})\s*년?\s*([1-4])\s*분기")
_RE_SEMESTER = re.compile(rf"({_Y})\s*년?\s*([12])\s*학기")


def _expand_yy(yy: int) -> int:
    """2자리 연도 → 4자리. 50~99 → 19xx, 00~49 → 20xx."""
    return 1900 + yy if yy >= 50 else 2000 + yy


# ══════════════════════════════════════════════
# 파서 본체
# ══════════════════════════════════════════════
class _SpanSet:
    """이미 소비된 문자 구간을 추적해 중복 매칭을 막는다."""

    def __init__(self):
        self._spans: list[tuple[int, int]] = []

    def overlaps(self, start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in self._spans)

    def add(self, start: int, end: int) -> None:
        self._spans.append((start, end))


def _collect_anchors(text: str, now: datetime) -> tuple[list[tuple[int, int, TimeAnchor]], _SpanSet]:
    """절대·상대 시점을 위치 정보와 함께 수집."""
    found: list[tuple[int, int, TimeAnchor]] = []
    used = _SpanSet()

    def take(m: re.Match, anchor: TimeAnchor) -> None:
        if used.overlaps(m.start(), m.end()):
            return
        used.add(m.start(), m.end())
        found.append((m.start(), m.end(), anchor))

    # ── 1. 반기/분기/학기 (가장 구체적인 복합 표현부터) ──
    for m in _RE_HALF.finditer(text):
        year, half = int(m.group(1)), m.group(2)
        take(m, TimeAnchor(m.group(0), year, 3 if half == "상" else 9,
                           "absolute", "medium"))
    for m in _RE_QUARTER.finditer(text):
        year, q = int(m.group(1)), int(m.group(2))
        take(m, TimeAnchor(m.group(0), year, q * 3 - 1, "absolute", "medium"))
    for m in _RE_SEMESTER.finditer(text):
        year, s = int(m.group(1)), int(m.group(2))
        take(m, TimeAnchor(m.group(0), year, 4 if s == 1 else 10, "absolute", "medium"))

    # ── 2. 연·월 ──
    for m in _RE_YM.finditer(text):
        take(m, TimeAnchor(m.group(0), int(m.group(1)), int(m.group(2)), "absolute", "high"))
    for m in _RE_EN_MY.finditer(text):
        month = _EN_MONTHS.get(m.group(1).lower(), None)
        if month:
            take(m, TimeAnchor(m.group(0), int(m.group(2)), month, "absolute", "high"))
    for m in _RE_EN_YM.finditer(text):
        take(m, TimeAnchor(m.group(0), int(m.group(1)), int(m.group(2)), "absolute", "high"))
    for m in _RE_YY_M.finditer(text):
        take(m, TimeAnchor(m.group(0), _expand_yy(int(m.group(1))), int(m.group(2)),
                           "absolute", "medium"))

    # ── 3. 상대 시점 → 절대 시점으로 환산 ──
    # 결합 표현("작년 하반기")을 단독 표현("작년")보다 먼저 소비해야
    # 반기 정보가 유실되지 않는다.
    for m in _RE_REL_HALF.finditer(text):
        off = _REL_YEAR_OFFSET.get(re.sub(r"\s+", "", m.group(1)), 0)
        take(m, TimeAnchor(m.group(0), now.year + off,
                           3 if m.group(2) == "상" else 9, "relative", "medium"))
    for m in _RE_REL_QUARTER.finditer(text):
        off = _REL_YEAR_OFFSET.get(re.sub(r"\s+", "", m.group(1)), 0)
        take(m, TimeAnchor(m.group(0), now.year + off,
                           int(m.group(2)) * 3 - 1, "relative", "medium"))

    for m in _RE_YEARS_AGO.finditer(text):
        n = _num(m.group(1))
        if n:
            take(m, TimeAnchor(m.group(0), now.year - n, None, "relative", "medium"))
    for m in _RE_MONTHS_AGO.finditer(text):
        n = _num(m.group(1))
        if n:
            d = add_months(now, -n)
            take(m, TimeAnchor(m.group(0), d.year, d.month, "relative", "medium"))
    for m in _RE_TWO_YEARS_AGO.finditer(text):
        n = 3 if "재재작년" in m.group(0) else 2
        take(m, TimeAnchor(m.group(0), now.year - n, None, "relative", "medium"))
    for m in _RE_LAST_YEAR.finditer(text):
        take(m, TimeAnchor(m.group(0), now.year - 1, None, "relative", "medium"))
    for m in _RE_THIS_YEAR.finditer(text):
        take(m, TimeAnchor(m.group(0), now.year, None, "relative", "medium"))
    for m in _RE_LAST_MONTH.finditer(text):
        d = add_months(now, -1)
        take(m, TimeAnchor(m.group(0), d.year, d.month, "relative", "medium"))
    for m in _RE_THIS_MONTH.finditer(text):
        take(m, TimeAnchor(m.group(0), now.year, now.month, "relative", "medium"))

    # ── 4. 연도 단독 (위에서 소비되지 않은 것만) ──
    for m in _RE_Y.finditer(text):
        take(m, TimeAnchor(m.group(0), int(m.group(1)), None, "absolute", "high"))
    for m in _RE_YY.finditer(text):
        raw = m.group(1) or m.group(2)
        if raw:
            take(m, TimeAnchor(m.group(0), _expand_yy(int(raw)), None, "absolute", "low"))

    found.sort(key=lambda t: t[0])
    return found, used


def _collect_durations(text: str, used: _SpanSet) -> list[DurationHint]:
    """명시적 기간 표현 수집. 시점을 몰라도 '얼마나' 는 확정할 수 있다."""
    hints: list[DurationHint] = []

    def take(m: re.Match, hint: DurationHint) -> None:
        if used.overlaps(m.start(), m.end()):
            return
        used.add(m.start(), m.end())
        hints.append(hint)

    # N년 M개월 (가장 구체적)
    for m in _RE_DUR_YM.finditer(text):
        y, mo = _num(m.group(1)), _num(m.group(2))
        if y or mo:
            take(m, DurationHint(m.group(0), y * 12 + mo, "explicit"))
    # N년차 → 근속 N년차 (경과는 N-1 ~ N년)
    for m in _RE_NTH_YEAR.finditer(text):
        n = _num(m.group(1))
        if n:
            take(m, DurationHint(m.group(0), max(n - 1, 0) * 12, "nth_year", "medium"))
    # N년간 / N년째
    for m in _RE_DUR_Y.finditer(text):
        n = _num(m.group(1))
        if n:
            take(m, DurationHint(m.group(0), n * 12, "explicit"))
    # N개월(간)
    for m in _RE_DUR_M.finditer(text):
        n = _num(m.group(1))
        if n:
            take(m, DurationHint(m.group(0), n, "explicit"))
    # N주
    for m in _RE_DUR_W.finditer(text):
        n = _num(m.group(1))
        if n:
            take(m, DurationHint(m.group(0), max(round(n / 4.345), 0), "explicit", "medium"))

    return hints


def _collect_ranges(text: str,
                    anchors: list[tuple[int, int, TimeAnchor]],
                    now: datetime) -> tuple[list[DurationHint], bool]:
    """
    'A ~ B' 형태의 구간을 찾아 실제 기간을 계산한다.
    v1.1 은 2021.03 과 2021.08 을 개별 시점으로만 읽고 '5개월' 을 놓쳤다.
    """
    ranges: list[DurationHint] = []
    ongoing_used = False

    ongoing_spans = [(m.start(), m.end()) for m in _RE_ONGOING.finditer(text)]

    # 시점 ↔ 시점
    for i in range(len(anchors) - 1):
        s1, e1, a1 = anchors[i]
        s2, e2, a2 = anchors[i + 1]
        if not _RE_RANGE_SEP.match(text[e1:s2]):
            continue
        both_months = bool(a1.month and a2.month)
        if both_months:
            months = months_between(a1.year, a1.month, a2.year, a2.month)
        else:
            # 월을 모르면 연 단위 차이만 사용한다.
            # (1월~12월로 가정하면 "2019-2021" 이 2년 11개월로 부풀려진다)
            months = (a2.year - a1.year) * 12
        if months > 0:
            ranges.append(DurationHint(
                f"{a1.label()} ~ {a2.label()}", months, "range",
                "high" if both_months else "medium",
            ))

    # 시점 ↔ 진행중(현재)
    for s, e, a in anchors:
        for os_, oe in ongoing_spans:
            if os_ < e or not _RE_RANGE_SEP.match(text[e:os_]):
                continue
            months = (months_between(a.year, a.month, now.year, now.month)
                      if a.month else (now.year - a.year) * 12)
            if months > 0:
                ranges.append(DurationHint(
                    f"{a.label()} ~ 현재", months, "ongoing_range",
                    "high" if a.month else "medium",
                ))
                ongoing_used = True
            break

    return ranges, ongoing_used


def parse_time_expressions(text: str, now: datetime) -> TimeFacts:
    """
    경력 텍스트에서 시점·기간을 전부 추출해 기준 시각(now) 대비 사실로 확정한다.
    반환된 facts 는 그대로 LLM 프롬프트에 주입되며, 모델은 시간 계산을 하지 않는다.
    """
    tf = TimeFacts()
    if not text or not text.strip():
        return tf

    raw_anchors, used = _collect_anchors(text, now)

    # 미래 시점은 사실 판단에서 제외하고 경고로만 남긴다
    valid: list[tuple[int, int, TimeAnchor]] = []
    for s, e, a in raw_anchors:
        if (a.year, a.month or 1) > (now.year, now.month):
            tf.future_mentions.append(a.text.strip())
        else:
            valid.append((s, e, a))

    tf.anchors = [a for _, _, a in valid]
    tf.ongoing = bool(_RE_ONGOING.search(text))

    ranges, _ = _collect_ranges(text, valid, now)
    explicit = _collect_durations(text, used)
    tf.durations = ranges + explicit

    # ── 시점 확정 ──
    if tf.anchors:
        ordered = sorted(tf.anchors, key=lambda a: a.sort_key)
        tf.earliest, tf.latest = ordered[0], ordered[-1]
        tf.resolution = ("absolute"
                         if any(a.kind == "absolute" for a in tf.anchors)
                         else "relative")
        if tf.ongoing:
            tf.months_since_latest = 0
        else:
            tf.months_since_latest = max(months_between(
                tf.latest.year, tf.latest.month or 12, now.year, now.month), 0)
    elif tf.durations:
        tf.resolution = "duration_only"
    else:
        tf.resolution = "none"

    # ── 기간 확정: 구간 계산값을 명시적 표현보다 신뢰 ──
    if ranges:
        tf.total_duration_months = max(d.months for d in ranges)
    elif explicit:
        tf.total_duration_months = max(d.months for d in explicit)

    tf.facts, tf.warnings = _build_facts(tf, now)
    return tf


def _build_facts(tf: TimeFacts, now: datetime) -> tuple[list[str], list[str]]:
    """LLM 에 전달할 사실 문장과 경고를 생성."""
    facts: list[str] = []
    warnings: list[str] = []
    today = now.date().isoformat()

    if tf.latest:
        if tf.ongoing:
            facts.append(
                f"진행 상태: 현재 진행/재직 중 (기준일 {today} 시점에도 유효) — 신선도 문제 없음"
            )
        else:
            elapsed = tf.months_since_latest or 0
            facts.append(
                f"가장 최근 시점: {tf.latest.label()} "
                f"→ 기준일({today}) 대비 {fmt_duration(elapsed)} 경과"
            )
        if tf.earliest and tf.earliest.sort_key != tf.latest.sort_key:
            facts.append(f"가장 이른 시점: {tf.earliest.label()}")

    if tf.total_duration_months is not None:
        src = next((d for d in tf.durations if d.kind in ("range", "ongoing_range")), None)
        if src:
            facts.append(
                f"활동 기간: {src.text} = {fmt_duration(src.months)} "
                f"(시작·종료 시점에서 계산)"
            )
        else:
            src = max(tf.durations, key=lambda d: d.months)
            label = "근속 연차 표현" if src.kind == "nth_year" else "명시된 기간"
            facts.append(
                f"활동 기간: '{src.text.strip()}' → 약 {fmt_duration(src.months)} ({label})"
            )

    # 신선도 판정 — 모델이 직접 계산하지 않도록 결론까지 코드가 낸다
    if tf.ongoing:
        pass
    elif tf.months_since_latest is not None:
        m = tf.months_since_latest
        if m <= 12:
            facts.append("신선도: 최근 1년 이내 — 기간 문제 없음")
        elif m <= 36:
            facts.append(f"신선도: {fmt_duration(m)} 경과 — 통상 유효 범위")
        else:
            facts.append(f"신선도: {fmt_duration(m)} 경과 — 오래된 경험으로 판단 가능")

    # 기간의 충분성 — 인턴/프로젝트 평가 시 근거로 사용
    if tf.total_duration_months is not None:
        d = tf.total_duration_months
        if d < 6:
            facts.append(f"기간 규모: {fmt_duration(d)} — 단기 경험 (6개월 미만)")
        elif d < 24:
            facts.append(f"기간 규모: {fmt_duration(d)} — 중기 경험 (6개월~2년)")
        else:
            facts.append(f"기간 규모: {fmt_duration(d)} — 장기 경험 (2년 이상)")

    if tf.resolution == "duration_only":
        warnings.append(
            "기간은 확인되나 시작·종료 연도가 없습니다. "
            "'언제' 는 추측하지 말고 '기간 미기재' 로 지적하십시오."
        )
    elif tf.resolution == "none":
        if tf.future_mentions:
            warnings.append(
                "확인된 시점이 전부 기준일 이후(미래)입니다. "
                "아직 일어나지 않은 계획이므로 완료된 경력으로 취급하지 마십시오."
            )
        else:
            warnings.append(
                "입력에 연도·기간 표현이 전혀 없습니다. "
                "시점·기간 판단을 지어내지 말고 '기간 미기재' 를 누락 요소로 지적하십시오."
            )

    if tf.future_mentions:
        warnings.append(
            f"기준일 이후(미래) 시점 표현이 있습니다: {', '.join(tf.future_mentions[:5])} "
            "— 이미 완료된 사실처럼 서술하지 마십시오."
        )

    if tf.latest and tf.latest.confidence == "low":
        warnings.append(
            f"'{tf.latest.text.strip()}' 는 두 자리 연도로 추정한 값이라 확실하지 않습니다."
        )

    return facts, warnings
