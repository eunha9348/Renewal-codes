"""
Career Analysis AI - INDIVIDUAL Edition v1.2
=============================================
단일 경력/자격증/활동 심층 분석 전용 모듈

기능:
  - 하나의 자격증, 인턴 경험, 프로젝트, 대외활동 등 단일 항목 심층 분석
  - STAR 방식 이력서 초안 자동 생성
  - 해당 항목과 시너지가 높은 자격증·교육·프로젝트·활동 추천
  - 단기·중기·장기 액션 플랜 제시 (절대 날짜로 고정)
  - 모든 출력은 순수 JSON (stdout), 로그는 전부 stderr

구성 모듈:
  career_individual.py  분석 본체 (이 파일)
  cert_registry.py      자격증 실재성 검증 — 공식 출처 수집 파이프라인
  time_parsing.py       경력 텍스트의 시간 표현 파서

────────────────────────────────────────────────────────────────────────
v1.2 변경점 — 감사 보고서가 지적한 잔여 한계 3건 해결
────────────────────────────────────────────────────────────────────────
[1] 자격증 목록의 수동 유지보수 → 공식 출처 수집 파이프라인
    v1.1 은 화이트리스트를 코드 상수로 고정해, 신설·개명 종목을 반영하려면
    사람이 코드를 고쳐야 했다("목록이 서서히 낡는다").
    v1.2 는 cert_registry 가 자격 시험 주관 기관(한국산업인력공단/Q-Net)의
    공개 API 에서 종목을 수집·캐시한다. 자동화가 새 환각 경로가 되지 않도록
    수집 결과는 앵커·규모·형태 검증을 통과해야만 채택되고, 기존 목록을
    대체하지 않고 합집합으로만 넓힌다. 폐지 후보는 자동 삭제하지 않고
    보고만 한다.  갱신: python cert_registry.py --refresh

[2] 연도 파싱이 절대 표기만 인식 → 상대·기간·구간 표현 전면 지원
    v1.1 은 `YYYY` / `YYYY.MM` 만 인식해 "재직 3년차", "3년 전",
    "작년 하반기" 를 놓쳤고, 그 경우 기간 진단이 통째로 비었다.
    v1.2 의 time_parsing 은 절대·상대·기간·구간·진행중 표현을 모두 해석하고
    구간에서는 실제 개월수까지 계산한다. 모델은 시간 계산을 하지 않는다.

[3] URL 화이트리스트가 실재 URL까지 제거 → 2단 정책 + 선택적 실검증
    등록 심사를 거쳐야 하는 .go.kr/.ac.kr/.re.kr 은 도메인 자체가 기관
    실재성을 보증하므로 허용하고, 자격 발급 기관 도메인을 대폭 보강했다.
    CAREER_VERIFY_URLS=1 이면 실제 접속해 죽은 링크까지 제거한다.

────────────────────────────────────────────────────────────────────────
v1.1 에서 해결한 내용 (유지)
────────────────────────────────────────────────────────────────────────
  - 환각 자격증 차단 ("사회분석사" → 실재는 "사회조사분석사")
  - 시간 미일치 오류: 기준 시각 KST 고정 + 프롬프트 실제 주입
  - 실행 오류: main() TypeError, stdin 미수신, 로그의 stdout 오염,
    import 시점 Client 생성, resp.text None 처리

Hallucination 방지 원칙:
  - 자격증은 검증된 레지스트리에 있는 것만 (코드 레벨 강제)
  - URL 추측/조합 금지 → 검증 실패 시 null
  - 데이터 부족 시 status: insufficient_data (임의 내용 채우기 금지)

사용법:
  export GEMINI_API_KEY="AIza..."            # 또는 코드 상수에 직접 입력
  export DATA_GO_KR_SERVICE_KEY="..."        # 자격증 목록 갱신용 (선택)
  python cert_registry.py --refresh          # 공식 출처에서 목록 갱신
  python career_individual.py
  → URL, 파일 경로, 또는 텍스트 붙여넣기 후 빈 줄에서 END
  → JSON 결과만 stdout 으로 출력
"""

import json
import re
import os
import io
import sys
import time
import difflib
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from collections import deque

from google import genai
from google.genai import types

import cert_registry
import time_parsing
from time_parsing import add_months

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
# 테스트용으로 아래에 API 키를 직접 입력하세요.
# (환경변수 GEMINI_API_KEY 또는 GOOGLE_AI_STUDIO_API_KEY 가 설정되어 있으면
#  그 값이 우선 적용됩니다 — 배포/공유 환경에서는 하드코딩 대신 환경변수 사용을 권장합니다.)
# ⚠ 실제 키를 입력한 채로 커밋/푸시하지 마세요. (.gitignore 에 .env 는 있지만
#    이 파일 자체에 하드코딩하면 그대로 커밋될 수 있습니다.)
GEMINI_API_KEY = ""          # 예: "AIzaSy..."

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_AI_STUDIO_API_KEY")
    or GEMINI_API_KEY
)

__version__ = "1.2.0"

_ANALYSIS_MODEL  = "gemini-2.5-flash"
_EMBEDDING_MODEL = "gemini-embedding-001"

# 크롤러 설정
_MAX_PAGES       = 30
_MAX_PDFS        = 5
_FETCH_TIMEOUT   = 15
_MAX_CONTENT_MB  = 1
_MIN_CRAWL_CHARS = 200

# 재시도 설정
_MAX_RETRIES    = 4
_RETRY_BASE_SEC = 5

# 자격증 검증 모드
#   True  : 화이트리스트에 없는 자격증 추천은 전부 제거 (권장, 환각 차단)
#   False : 명시적 블록리스트만 제거
_STRICT_CERT_WHITELIST = True

# 기준 타임존 — 한국 사용자 기준. UTC 서버에서도 KST 로 고정된다.
_KST = timezone(timedelta(hours=9), name="KST")

_client = None


def _log(msg: str = "") -> None:
    """모든 진단 로그는 stderr 로. stdout 은 순수 JSON 전용."""
    print(msg, file=sys.stderr, flush=True)


def _get_client() -> genai.Client:
    """API 키가 없을 때 import 시점에 죽지 않도록 지연 초기화."""
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY 가 비어 있습니다. "
                "환경변수 GEMINI_API_KEY 또는 GOOGLE_AI_STUDIO_API_KEY 를 설정하세요."
            )
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ──────────────────────────────────────────────
# 응답 모델 (analysis_response 없으면 폴백 사용)
# ──────────────────────────────────────────────
#  응답 모델은 상위 프로젝트의 것을 우선 사용한다.
#    1) src.ai.models      — 실제 서비스에 통합해 실행할 때의 경로
#    2) analysis_response  — 모듈 단독 배치 시의 경로
#    3) 내장 폴백           — 위 둘이 모두 없을 때 (저장소 단독 실행/테스트)
#  1·2 를 찾지 못하면 폴백이 조용히 쓰이므로, 어느 것이 적용됐는지
#  RESPONSE_MODEL_SOURCE 로 확인할 수 있게 남겨둔다.
RESPONSE_MODEL_SOURCE = "builtin_fallback"

try:
    from src.ai.models import VectorSuccessResponse, ErrorResponse  # type: ignore
    RESPONSE_MODEL_SOURCE = "src.ai.models"
except Exception:
    try:
        from analysis_response import VectorSuccessResponse, ErrorResponse  # type: ignore
        RESPONSE_MODEL_SOURCE = "analysis_response"
    except Exception:  # pragma: no cover - 단독 실행 폴백
        class _BaseResp:
            def _data(self) -> dict:
                raise NotImplementedError

            def model_dump(self, exclude_none: bool = False) -> dict:
                d = self._data()
                if exclude_none:
                    d = {k: v for k, v in d.items() if v is not None}
                return d

            def model_dump_json(self, indent: int = 2, exclude_none: bool = False) -> str:
                return json.dumps(
                    self.model_dump(exclude_none=exclude_none),
                    ensure_ascii=False,
                    indent=indent,
                )

        class VectorSuccessResponse(_BaseResp):
            def __init__(self, result: dict, vector=None):
                self.status = "success"
                self.result = result
                self.vector = vector

            def _data(self) -> dict:
                return {"status": self.status, "vector": self.vector, "result": self.result}

        class ErrorResponse(_BaseResp):
            def __init__(self, message: str):
                self.status = "error"
                self.message = message

            def _data(self) -> dict:
                return {"status": self.status, "message": self.message}


# ══════════════════════════════════════════════
# 1  시간 기준(TimeContext) — 시간 미일치 오류의 단일 원천
# ══════════════════════════════════════════════
#  [문제] 기존 코드는 main() 에서 date.today() 로 기준일을 만들어 출력에만
#         찍고, 정작 LLM 프롬프트에는 넣지 않았다. 모델은 자기 학습 시점을
#         '현재'로 가정하므로 아래가 전부 어긋났다.
#           - "현재 시행 중인 자격증" 판단
#           - "현재 취업시장" 수요 서술
#           - 단기/중기/장기 액션플랜의 실제 시점
#           - 입력 경력의 '몇 년 전' 계산 (기간_문제 진단)
#         게다가 date.today() 는 서버 로컬(UTC) 기준이라 KST 와 하루 어긋났다.
#
#  [해결] 기준 시각을 KST 로 고정하고, 파이썬이 모든 시간 값을 미리 계산해
#         프롬프트에 주입한다. 모델은 시간을 '추론'하지 않고 '전달받는다'.
# ══════════════════════════════════════════════



class TimeContext:
    """분석 전 구간에서 공유되는 단 하나의 시간 기준."""

    # 액션플랜 구간 정의 (개월)
    SHORT_MONTHS = 3     # 단기: 오늘 ~ +3개월
    MID_MONTHS   = 12    # 중기: +3개월 ~ +12개월
    LONG_MONTHS  = 36    # 장기: +12개월 ~ +36개월

    def __init__(self, now: datetime | None = None):
        self.now = (now or datetime.now(_KST)).astimezone(_KST)
        self.today = self.now.date()
        self.iso_date = self.today.isoformat()
        self.iso_datetime = self.now.isoformat(timespec="seconds")
        self.year = self.now.year
        self.month = self.now.month
        self.quarter = (self.month - 1) // 3 + 1
        self.half = 1 if self.month <= 6 else 2

        s_end = add_months(self.now, self.SHORT_MONTHS)
        m_end = add_months(self.now, self.MID_MONTHS)
        l_end = add_months(self.now, self.LONG_MONTHS)

        self.windows = {
            "단기": (self.now, s_end, self.SHORT_MONTHS),
            "중기": (s_end, m_end, self.MID_MONTHS),
            "장기": (m_end, l_end, self.LONG_MONTHS),
        }

    # ── 표시용 ──
    def window_label(self, key: str) -> str:
        start, end, _ = self.windows[key]
        return f"{start.date().isoformat()} ~ {end.date().isoformat()}"

    def window_deadline(self, key: str) -> str:
        return self.windows[key][1].date().isoformat()

    def as_dict(self) -> dict:
        return {
            "기준시각_KST": self.iso_datetime,
            "기준일": self.iso_date,
            "기준연도": self.year,
            "기준분기": f"{self.year}년 {self.quarter}분기",
            "단기_구간": self.window_label("단기"),
            "중기_구간": self.window_label("중기"),
            "장기_구간": self.window_label("장기"),
            "타임존": "Asia/Seoul (UTC+09:00)",
        }

    def as_prompt_block(self, time_facts=None) -> str:
        tf = time_facts
        lines = [
            "=== [시간 기준 — 반드시 이 값만 사용] ===",
            f"오늘 날짜(KST): {self.iso_date}  ({self.year}년 {self.month}월, {self.year}년 {self.quarter}분기)",
            "당신의 학습 시점이 아니라 위 '오늘 날짜'가 현재입니다.",
            "'현재', '요즘', '최근', '올해', '작년' 은 전부 위 날짜를 기준으로 해석하십시오.",
            "시간과 관련된 수치는 아래 계산된 값만 사용하고, 직접 계산하거나 추측하지 마십시오.",
            "",
            "[액션플랜 구간 — 이 절대 기간을 그대로 전제로 작성]",
            f"  단기: {self.window_label('단기')} (마감 {self.window_deadline('단기')}, {self.SHORT_MONTHS}개월)",
            f"  중기: {self.window_label('중기')} (마감 {self.window_deadline('중기')})",
            f"  장기: {self.window_label('장기')} (마감 {self.window_deadline('장기')})",
        ]
        if tf is not None and tf.facts:
            lines += ["", "[입력 데이터에서 확정된 시간 사실 — 재계산 금지]"]
            lines += [f"  - {f}" for f in tf.facts]
        if tf is not None and tf.warnings:
            lines += ["", "[시간 관련 주의]"]
            lines += [f"  - {w}" for w in tf.warnings]
        lines += [
            "",
            "[시간 서술 금지 규칙]",
            f"  - {self.year}년 이후의 미래 연도를 이미 일어난 사실처럼 쓰지 말 것",
            "  - 입력에 없는 연도·기간·취득시점을 채워 넣지 말 것 (모르면 null)",
            "  - '3년 전', '최근 몇 년' 같은 상대 표현 대신 위에 주어진 값을 쓸 것",
            "=" * 44,
        ]
        return "\n".join(lines)


def extract_time_facts(text: str, tc: "TimeContext") -> time_parsing.TimeFacts:
    """
    입력 텍스트의 시간 표현을 전부 해석해 기준일 대비 사실로 확정한다.

    [v1.1 한계] 정규식 두 개로 `YYYY` / `YYYY.MM` 만 인식했다. "재직 3년차",
                "3년 전", "작년 하반기" 같은 표현은 전부 인식하지 못했고,
                인식 실패 시 모델에게 기간 판단을 하지 말라고 지시했기 때문에
                기간_문제 진단이 통째로 비는 문제가 있었다.

    [v1.2]     time_parsing 모듈이 절대·상대·기간·구간·진행중 표현을 모두
                해석한다. 구간("2021.03~2021.08")에서는 실제 근무 개월수까지
                계산해 전달하므로, 모델은 시간 계산을 전혀 하지 않는다.
    """
    return time_parsing.parse_time_expressions(text, tc.now)



# ══════════════════════════════════════════════
# 2  LLM 호출 헬퍼
# ══════════════════════════════════════════════
def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("429", "quota", "rate_limit", "rate limit", "resource_exhausted"))


def _call_with_retry(fn, *args, **kwargs):
    for attempt in range(_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if _is_rate_limit(e) and attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BASE_SEC * (2 ** attempt)
                _log(f"  [Rate Limit] {wait}초 후 재시도 ({attempt + 1}/{_MAX_RETRIES - 1})...")
                time.sleep(wait)
            else:
                raise


def _call_model(system_prompt: str, user_prompt: str,
                use_google_search: bool = False) -> dict:
    raw_text = ""
    try:
        tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None

        def _do():
            return _get_client().models.generate_content(
                model=_ANALYSIS_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt if system_prompt else None,
                    temperature=0.1,
                    tools=tools,
                ),
            )

        resp = _call_with_retry(_do)
        raw_text = (getattr(resp, "text", None) or "").strip()
        if not raw_text:
            return {"status": "error", "message": "모델이 빈 응답을 반환했습니다."}
        return json.loads(clean_json_response(raw_text))

    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "message": f"JSON parse failed: {e.msg}",
            "raw_response": raw_text[:1000],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _call_model_raw(user_prompt: str, system_prompt: str = "",
                    use_google_search: bool = False) -> str:
    try:
        tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None

        def _do():
            return _get_client().models.generate_content(
                model=_ANALYSIS_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt if system_prompt else None,
                    temperature=0.0,
                    tools=tools,
                ),
            )

        resp = _call_with_retry(_do)
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:
        return f"ERROR: {e}"


# ══════════════════════════════════════════════
# 3  JSON 정제
# ══════════════════════════════════════════════
def clean_json_response(raw_text: str) -> str:
    raw_text = raw_text.strip()

    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    depth = 0
    start = -1
    for i, ch in enumerate(raw_text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return raw_text[start:i + 1].strip()

    depth = 0
    start = -1
    for i, ch in enumerate(raw_text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start != -1:
                return raw_text[start:i + 1].strip()

    return raw_text


# ══════════════════════════════════════════════
# 4  HTML 파서
# ══════════════════════════════════════════════
class _TextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "meta", "link", "head"}

    def __init__(self, base_url: str = ""):
        super().__init__()
        self._skip = 0
        self._parts: list[str] = []
        self._links: list[str] = []
        self._base = base_url

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        if tl in self.SKIP_TAGS:
            self._skip += 1
        if tl == "a":
            href = dict(attrs).get("href", "")
            if href and not href.startswith(("javascript:", "mailto:", "tel:")):
                self._links.append(urllib.parse.urljoin(self._base, href))

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            s = data.strip()
            if s:
                self._parts.append(s)

    @property
    def text(self) -> str:
        return "\n".join(self._parts)

    @property
    def links(self) -> list[str]:
        return self._links


def _is_spa(html: str) -> bool:
    visible = len(re.sub(r"<[^>]+>", " ", html).strip())
    return visible < 2000 and html.lower().count("<script") >= 2


def _extract_links_from_raw_html(raw_html: str, base_url: str) -> list[str]:
    found = []
    for m in re.finditer(
        r'(?:href|src|data-href|data-src)\s*=\s*["\']([^"\']+)["\']',
        raw_html, re.IGNORECASE
    ):
        href = m.group(1).strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        found.append(urllib.parse.urljoin(base_url, href))
    return list(dict.fromkeys(found))


# ══════════════════════════════════════════════
# 5  Notion 전용 크롤러
# ══════════════════════════════════════════════
def _is_notion_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return "notion.site" in host or "notion.so" in host


def _parse_notion_blocks(blocks: dict) -> str:
    lines: list[str] = []

    def _rich_text_to_str(rt) -> str:
        if not isinstance(rt, list):
            return ""
        parts = []
        for chunk in rt:
            if isinstance(chunk, list) and chunk:
                parts.append(str(chunk[0]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts).strip()

    TEXT_PROPS = ["title", "caption", "description"]
    SKIP_TYPES = {"image", "video", "file", "audio", "pdf", "embed", "bookmark",
                  "divider", "table_of_contents", "breadcrumb", "unsupported"}

    for block_id, block_wrapper in blocks.items():
        val = block_wrapper.get("value", {})
        if not isinstance(val, dict):
            continue
        btype = val.get("type", "")
        if btype in SKIP_TYPES:
            continue
        props = val.get("properties", {})
        if not isinstance(props, dict):
            continue

        for prop_key in TEXT_PROPS:
            if prop_key in props:
                text = _rich_text_to_str(props[prop_key])
                if text:
                    lines.append(text)
                break

    return "\n".join(lines)


def _extract_from_next_data(html: str) -> str:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(.*?)\s*</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""

    record_map = (
        data.get("props", {})
            .get("pageProps", {})
            .get("recordMap", {})
    )
    if not record_map:
        record_map = data.get("props", {}).get("pageProps", {})

    blocks = record_map.get("block", {})
    if not blocks:
        return ""

    text = _parse_notion_blocks(blocks)
    _log(f"    __NEXT_DATA__ 파싱 성공: {len(blocks)}개 블록, {len(text)} chars")
    return text


def _extract_og_meta(html: str, url: str) -> str:
    parts = []
    patterns = [
        (r'<title[^>]*>(.*?)</title>', "페이지 제목"),
        (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', "OG 제목"),
        (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', "OG 설명"),
        (r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', "메타 설명"),
    ]
    seen = set()
    for pattern, label in patterns:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            val = m.group(1).strip()
            val = (val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                      .replace("&#39;", "'").replace("&quot;", '"'))
            if val and val not in seen:
                parts.append(f"{label}: {val}")
                seen.add(val)

    path = urllib.parse.urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1]
    slug_words = re.sub(r"-[0-9a-f]{20,}$", "", slug)
    slug_clean = re.sub(r"[-_]", " ", slug_words).strip()
    if slug_clean:
        parts.append(f"URL 슬러그(페이지명 힌트): {slug_clean}")

    return "\n".join(parts)


def crawl_notion_page(url: str) -> str:
    _log(f"  [Notion 크롤러] 시작: {url}")
    collected_parts: list[str] = []

    raw_bytes = _fetch_bytes(url)
    html = ""
    if raw_bytes:
        try:
            html = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html = raw_bytes.decode("euc-kr", errors="replace")

    if html:
        next_data_text = _extract_from_next_data(html)
        if next_data_text.strip():
            collected_parts.append(f"[Notion 블록 콘텐츠: {url}]\n{next_data_text}")

        og_text = _extract_og_meta(html, url)
        if og_text.strip():
            collected_parts.append(f"[Notion 메타정보: {url}]\n{og_text}")

        parser = _TextExtractor(base_url=url)
        parser.feed(html)
        plain_text = parser.text.strip()
        if plain_text and len(plain_text) > 100:
            collected_parts.append(f"[Notion HTML 텍스트: {url}]\n{plain_text}")

    combined = "\n\n".join(collected_parts).strip()
    _log(f"    추출 결과: {len(combined)} chars")

    if len(combined) < _MIN_CRAWL_CHARS:
        _log("    WARNING: 결과 빈약 → URL 힌트 보강")
        path = urllib.parse.urlparse(url).path.rstrip("/")
        slug_raw = path.split("/")[-1]
        slug_clean = re.sub(r"[-_]", " ", re.sub(r"-[0-9a-f]{20,}$", "", slug_raw)).strip()
        hint = (
            f"[Notion 크롤링 제한 안내]\n"
            f"Notion 페이지 URL: {url}\n"
            f"페이지 제목/경로명: {slug_clean}\n"
            f"Notion은 JavaScript 렌더링 기반이라 완전한 크롤링이 제한될 수 있습니다.\n"
            f"위 페이지명과 수집된 정보를 바탕으로 최대한 추론하되, "
            f"추론 불가 항목은 반드시 빈 배열로 반환하십시오.\n"
        )
        combined = hint + "\n\n" + combined

    _log(f"  [Notion 크롤러 완료] 총 {len(combined)} chars")
    return combined


# ══════════════════════════════════════════════
# 6  딥 크롤러 (일반 웹사이트)
# ══════════════════════════════════════════════
def _fetch_bytes(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read(int(_MAX_CONTENT_MB * 1024 * 1024))
    except Exception as e:
        _log(f"    WARNING fetch failed [{url}]: {e}")
        return None


def _parse_pdf_bytes(data: bytes) -> str:
    try:
        import pypdf
    except ImportError:
        _log("    WARNING: pypdf 미설치 → PDF 파싱 건너뜀 (pip install pypdf)")
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        _log(f"    OK PDF parsed ({len(reader.pages)} pages, {len(text)} chars)")
        return text
    except Exception as e:
        _log(f"    WARNING PDF parse error: {e}")
        return ""


def _parse_html_bytes(data: bytes, url: str) -> tuple[str, list[str], str]:
    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError:
        html = data.decode("euc-kr", errors="replace")
    parser = _TextExtractor(base_url=url)
    parser.feed(html)
    return parser.text, parser.links, html


def _same_origin(a: str, b: str) -> bool:
    return urllib.parse.urlparse(a).netloc == urllib.parse.urlparse(b).netloc


def _is_pdf_url(url: str, data: bytes | None = None) -> bool:
    if url.lower().split("?")[0].endswith(".pdf"):
        return True
    return bool(data and data[:4] == b"%PDF")


def deep_crawl_site(start_url: str) -> str:
    _log(f"  [딥 크롤러] 시작: {start_url}")
    collected: list[str] = []
    visited: set[str] = set()
    pdf_count = 0

    raw = _fetch_bytes(start_url)
    if raw is None:
        return ""
    if _is_pdf_url(start_url, raw):
        text = _parse_pdf_bytes(raw)
        return f"[PDF: {start_url}]\n{text}" if text else ""

    main_text, main_links, main_html = _parse_html_bytes(raw, start_url)
    visited.add(start_url)
    if main_text.strip():
        collected.append(f"[Main: {start_url}]\n{main_text}")
        _log(f"    Main page: {len(main_text)} chars")

    if _is_spa(main_html):
        _log("    SPA detected — using raw HTML link extraction")

    raw_links = _extract_links_from_raw_html(main_html, start_url)
    all_start_links = list(dict.fromkeys(main_links + raw_links))

    queue: deque[str] = deque()
    seen: set[str] = set()
    for lnk in all_start_links:
        clean = lnk.split("#")[0].rstrip("/")
        if not clean or clean in seen or clean in visited:
            continue
        if clean.startswith(("mailto:", "tel:", "javascript:")):
            continue
        seen.add(clean)
        if _is_pdf_url(clean):
            queue.appendleft(clean)
        elif _same_origin(start_url, clean):
            queue.append(clean)

    _log(f"    큐 초기 크기: {len(queue)}개 링크")
    page_count = 0
    while queue and (page_count < _MAX_PAGES or pdf_count < _MAX_PDFS):
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        sub_raw = _fetch_bytes(url)
        if sub_raw is None:
            continue

        if _is_pdf_url(url, sub_raw):
            if pdf_count >= _MAX_PDFS:
                _log(f"    PDF 한도 도달, 건너뜀: {url}")
                continue
            text = _parse_pdf_bytes(sub_raw)
            if text.strip():
                collected.append(f"[PDF: {url}]\n{text}")
                pdf_count += 1
                _log(f"    PDF collected ({pdf_count}/{_MAX_PDFS}): {url}")
            continue

        if page_count >= _MAX_PAGES:
            continue
        sub_text, sub_links, sub_html = _parse_html_bytes(sub_raw, url)
        page_count += 1
        if sub_text.strip():
            collected.append(f"[Page: {url}]\n{sub_text}")
            _log(f"    Page {page_count}: {url} ({len(sub_text)} chars)")

        sub_raw_links = _extract_links_from_raw_html(sub_html, url)
        for lnk in list(dict.fromkeys(sub_links + sub_raw_links)):
            clean = lnk.split("#")[0].rstrip("/")
            if not clean or clean in seen or clean in visited:
                continue
            if clean.startswith(("mailto:", "tel:", "javascript:")):
                continue
            seen.add(clean)
            if _is_pdf_url(clean):
                queue.appendleft(clean)
            elif _same_origin(start_url, clean):
                queue.append(clean)

    result = "\n\n".join(collected)
    _log(f"  [딥 크롤러 완료] {len(collected)}개 소스, {len(result)} chars")
    return result


# ══════════════════════════════════════════════
# 7  파일 리더
# ══════════════════════════════════════════════
def read_file(path: str) -> str:
    if not os.path.isfile(path):
        _log(f"  ERROR file not found: {path}")
        return ""
    if path.lower().endswith(".pdf"):
        with open(path, "rb") as f:
            return _parse_pdf_bytes(f.read())
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        _log(f"  OK file read ({len(content)} chars)")
        return content
    except Exception as e:
        _log(f"  ERROR reading file: {e}")
        return ""


# ══════════════════════════════════════════════
# 8  입력 수집
# ══════════════════════════════════════════════
def _looks_like_url(s: str) -> bool:
    return bool(re.match(r"^https?://\S+", s) or re.match(r"^www\.\S+", s))


def _looks_like_filepath(s: str) -> bool:
    return "\n" not in s and os.path.isfile(s.strip())


def _read_stdin_lines() -> list[str]:
    """빈 줄에서 END 입력 시 종료. (기존: 안내문만 출력하고 입력을 안 받던 버그)"""
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
    except EOFError:
        pass
    return lines


def get_user_input(lines=None) -> str:
    _log("\n" + "-" * 65)
    _log("  단일 경력/활동/자격증 정보를 입력하세요.")
    _log("  URL, 파일 경로, 또는 직접 텍스트 모두 가능합니다.")
    _log("  입력 완료 후 빈 줄에서 END 를 입력하세요.")
    _log("-" * 65 + "\n")

    if lines is None:
        lines = _read_stdin_lines()
    elif isinstance(lines, str):
        lines = lines.splitlines()
    else:
        lines = list(lines)

    raw = "\n".join(lines).strip()
    if not raw:
        return ""

    first_line = lines[0].strip() if lines else ""

    if len(lines) == 1 and _looks_like_url(first_line):
        url = first_line if first_line.startswith("http") else "https://" + first_line

        if _is_notion_url(url):
            _log("\n  [자동 감지] Notion URL → Notion 전용 크롤러 시작")
            content = crawl_notion_page(url)
        else:
            _log("\n  [자동 감지] URL → 딥 크롤링 시작")
            content = deep_crawl_site(url)

        if content.strip():
            return f"[SOURCE_URL: {url}]\n\n{content}"
        _log("  WARNING: 크롤링 실패 → URL 힌트 모드로 진행")
        return (
            f"[SOURCE_URL: {url}]\n"
            f"[크롤링 실패]\n"
            f"URL: {url}\n"
            f"URL 경로명과 도메인을 바탕으로 내용을 최대한 추론하십시오.\n"
            f"추론 불가 항목은 빈 배열로 반환하십시오."
        )

    if len(lines) == 1 and _looks_like_filepath(first_line):
        _log(f"\n  [자동 감지] 파일 경로 → 읽는 중: {first_line}")
        content = read_file(first_line.strip())
        return content if content.strip() else raw

    _log(f"\n  [자동 감지] 텍스트 직접 입력 ({len(raw)}자)")
    return raw


# ══════════════════════════════════════════════
# 9  Embedding
# ══════════════════════════════════════════════
def get_embedding(text: str):
    if not text:
        return None
    truncated = text[:10000]
    for kwargs in [
        {"contents": truncated, "config": types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")},
        {"contents": truncated},
        {"content": truncated, "config": types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")},
        {"content": truncated},
    ]:
        try:
            result = _get_client().models.embed_content(model=_EMBEDDING_MODEL, **kwargs)
            if getattr(result, "embeddings", None):
                return result.embeddings[0].values
            if getattr(result, "embedding", None):
                return result.embedding.values
        except Exception:
            continue
    _log("  WARNING embedding all fallbacks failed")
    return None


# ══════════════════════════════════════════════
# 10  자격증 실재성 검증 (환각 자격증 차단)
# ══════════════════════════════════════════════
#  [문제] "사회분석사" 처럼 존재하지 않는 자격증이 반복 추천되었다.
#         (실재하는 것은 "사회조사분석사" — Q-Net 국가기술자격.
#          모델이 이름을 뭉개어 만들어낸 전형적인 환각 케이스)
#
#  [v1.1] 코드에 화이트리스트를 상수로 박아 차단. 환각은 막았지만 목록이
#         고정이라 신설·개명 종목을 반영하려면 사람이 코드를 고쳐야 했다.
#
#  [v1.2] cert_registry 모듈이 자격 시험 주관 기관(한국산업인력공단/Q-Net)의
#         공개 API 에서 종목 목록을 수집·캐시한다. 수집 실패·키 없음·응답
#         이상 시에는 내장 시드로 자동 폴백하므로 검증 기능은 항상 동작한다.
#         갱신:  python cert_registry.py --refresh
# ══════════════════════════════════════════════
_CERT_REGISTRY = None


def get_cert_registry() -> cert_registry.CertRegistry:
    """레지스트리 지연 로드 (프로세스당 1회)."""
    global _CERT_REGISTRY
    if _CERT_REGISTRY is None:
        _CERT_REGISTRY = cert_registry.load_certs(allow_refresh=True)
        _log(f"  [자격증 레지스트리] {len(_CERT_REGISTRY.names)}종 "
             f"(출처: {_CERT_REGISTRY.origin})")
    return _CERT_REGISTRY


def verify_certification(name: str) -> tuple[bool, str]:
    """(통과여부, 사유). 레지스트리에 위임."""
    return get_cert_registry().verify(name, strict=_STRICT_CERT_WHITELIST)


def _norm_cert(name: str) -> str:
    return cert_registry.norm_cert(name)


# ══════════════════════════════════════════════
# 11  Hallucination 방지 규칙 (공통)
# ══════════════════════════════════════════════
_STRICT_HALLUCINATION_RULES = """
=== STRICT MODE: Hallucination 절대 금지 ===
규칙 1. 실재 확인 불가 추천 항목 생성 금지
  - 자격증: 아래 화이트리스트에 있는 것만. 목록 밖은 자동 삭제된다.
  - 교육·프로젝트·대외활동: 실재가 확인된 것만 추천.
    확신이 없으면 특정 브랜드명 대신 '유형'으로 서술하라
    (예: "OO아카데미 데이터분석 부트캠프" X → "실데이터 기반 분석 포트폴리오 과정" O).
  - 추천 항목 이름을 임의로 만들거나 변형하지 말 것.

규칙 2. URL 생성 금지
  - URL은 기억 속에 확실한 공식 URL만 허용. 추측·조합·변형 절대 금지.
  - 불확실하면 반드시 null (빈 문자열 "" 금지).

규칙 3. 빈 배열 우선 원칙
  - 추천할 항목이 없거나 실재 확인 불가이면 [] 반환.
  - 채우기 위해 임의로 만들어내는 것은 빈 배열보다 훨씬 나쁘다.

규칙 4. 데이터 부족 시 insufficient_data 반환
  - 입력 항목이 너무 짧거나 식별 불가 시, status를 "insufficient_data"로 설정.
  - 절대로 빈 값을 채우거나 임의로 내용을 생성하지 말 것.

규칙 5. 연도·기간 날조 금지
  - 취득 연도, 기간, 점수 등을 추측해서 채우지 말 것.
  - 모르면 null 또는 빈 문자열.
  - 시간 관련 값은 [시간 기준] 블록에 주어진 값만 사용한다. 직접 계산 금지.

규칙 6. 출력은 순수 JSON만
  - 마크다운 코드블록, 설명 텍스트, 주석 절대 포함 금지.

규칙 7. STAR 분석은 필수가 아님.
  - 데이터가 부족하면 절대 임의로 이야기를 지어내지 말 것.
  - 부족하면 star_format 각 필드를 null 로 두고,
    star_note 에 '데이터 부족으로 STAR 분석 불가' 사유와
    어떤 기록을 남기면 되는지 예시를 적을 것.
"""


# ══════════════════════════════════════════════
# 12  시스템 프롬프트
# ══════════════════════════════════════════════
def build_system_prompt_individual(tc: TimeContext, time_facts=None) -> str:
    """
    단일 항목 심층 분석용 시스템 프롬프트.
    ★ v1.2: 시간 기준(TimeContext)과 자격증 화이트리스트를 프롬프트에 실제로 주입.
             화이트리스트는 cert_registry 가 공식 출처에서 수집·캐시한 목록을 쓴다
             (수집 실패 시 내장 시드로 폴백).
    """
    return (
        "당신은 대한민국 최고의 전문 커리어 컨설턴트입니다.\n"
        "사용자가 제공한 단일 경력/자격증/활동 하나를 심층 분석하는 것이 임무입니다.\n\n"
        f"{tc.as_prompt_block(time_facts)}\n\n"
        f"{get_cert_registry().prompt_block()}\n\n"
        "[분석 원칙]\n"
        "1. 단일 항목에 집중 — 여러 항목이 보이더라도 가장 대표적인 하나를 선정\n"
        "2. 근거 중심 서술 — 입력 데이터에서 실제 확인된 내용만 서술\n"
        "3. 실질적 가치 발굴 — 표면적 사실 너머의 커리어 가치를 찾아낼 것\n"
        "4. 시너지 발굴 — 이 항목과 조합 시 가장 효과적인 추천 제시\n"
        "5. 데이터 부족 시 status를 'insufficient_data'로 설정하고 분석 중단\n"
        "6. 냉정한 진단 — 사용자가 듣기 싫더라도 진짜 약점을 직시하게 할 것. "
        "하지만, 그래도 조금은 따뜻한 언어로 설명할 것.\n"
        f"{_STRICT_HALLUCINATION_RULES}\n"
        "=== 냉정한 보완점 진단(item_diagnosis) 작성 지침 ===\n"
        "목적: 이 단일 항목이 이력서/커리어에서 실제로 얼마나 강한지 냉정하게 평가.\n"
        "절대 금지: 칭찬·위로·긍정적 포장 — item_diagnosis 섹션에는 좋은 말 하지 말 것.\n"
        "판단 기준 (해당하는 항목만 포함):\n"
        "  [서술_완성도] 기간/수치/역할/성과 중 빠진 것이 있는가?\n"
        "  [차별성_부족] 동일 스펙 보유자가 많아 희소성이 없는가?\n"
        "  [직무_연결_약함] 어필하려는 직무와 연결고리가 약한가?\n"
        "  [성과_불명확] 숫자·결과·임팩트가 없어 설득력이 떨어지는가?\n"
        "  [기간_문제] 너무 짧거나 오래되어 신선도가 떨어지는가?\n"
        "     → 반드시 위 [시간 기준] 블록의 경과 기간 값만 사용할 것.\n"
        "       입력에 연도가 없으면 '기간 미기재'만 지적하고 시점을 추측하지 말 것.\n"
        "  [단독_활용_한계] 이 항목 하나만으로 경쟁력이 불충분한가?\n"
        "severity 기준:\n"
        "  critical: 이 상태로 이력서에 넣으면 오히려 역효과 가능\n"
        "  major   : 설득력을 크게 낮추는 문제\n"
        "  minor   : 있으면 좋지만 당장 치명적이지 않은 부족함\n"
        "improvement_example: Before/After 형식의 실제 예시 문장\n\n"
        "[출력 형식] 순수 JSON만 — 마크다운 코드블록·설명·주석 절대 금지\n\n"
        "{\n"
        '  "status": "success",\n'
        '  "item_name": "분석 대상 항목명 (입력에서 추출)",\n'
        '  "item_type": "자격증|직무경력|인턴십|프로젝트|교육|봉사|대외활동|수상|기타",\n'
        '  "brief_summary": "항목의 핵심을 한 문장으로 요약",\n'
        '  "star_format": {\n'
        '    "title": "이력서에 쓸 경험 제목",\n'
        '    "S": "Situation — 어떤 상황·배경에서 이 경험을 하게 되었는가",\n'
        '    "T": "Task — 어떤 과제·목표·역할이 주어졌는가",\n'
        '    "A": "Action — 구체적으로 어떤 행동·노력을 했는가",\n'
        '    "R": "Result — 어떤 결과·성과·배움을 얻었는가"\n'
        '  },\n'
        '  "star_note": "데이터 부족으로 STAR 작성이 불가한 경우 사유와 기록 예시 (충분하면 null)",\n'
        '  "deep_analysis": {\n'
        '    "career_value": "이 항목이 커리어에서 갖는 실질적 가치와 의미",\n'
        f'    "market_value": "{tc.iso_date} 기준 한국 취업시장에서의 수요·희소성·경쟁력 평가",\n'
        '    "applicable_roles": ["어필 가능한 직무 1", "직무 2"]\n'
        '  },\n'
        '  "item_strengths": {\n'
        '    "has_genuine_strengths": true,\n'
        '    "one_line_strength_verdict": "핵심 강점 한 문장 — 없으면 null",\n'
        '    "no_strength_reason": null,\n'
        '    "summarized_strengths": ["구체적 강점 1", "강점 2"],\n'
        '    "strengths": [\n'
        '      {\n'
        '        "id": 1,\n'
        '        "category": "전문성_희소성|성과_입증|역량_다양성|경험_깊이|도메인_전문성|차별화_포인트",\n'
        '        "strength_level": "outstanding|notable|moderate",\n'
        '        "title": "강점 제목 (10자 이내)",\n'
        '        "analysis": "왜 강점인지 냉정하고 구체적인 근거 — 입력 데이터 기반",\n'
        '        "evidence": "입력 텍스트에서 직접 확인 가능한 내용",\n'
        '        "career_impact": "취업·커리어에서 발휘하는 실질적 영향",\n'
        '        "leverage_action": "지금 해야 할 한 가지 행동 (동사 시작)",\n'
        '        "showcase_example": "Before: ... → After: ... (없으면 null)"\n'
        '      }\n'
        '    ],\n'
        '    "strongest_asset": "가장 강력한 단일 강점 한 줄 — 없으면 null",\n'
        '    "positioning_tip": "면접·이력서 포지셔닝 전략 — 없으면 null"\n'
        '  },\n'
        '  "item_diagnosis": {\n'
        '    "one_line_verdict": "현재 상태를 냉정하게 한 문장으로",\n'
        '    "limitations": ["보완이 필요한 점 1", "한계 2"],\n'
        '    "weaknesses": [\n'
        '      {\n'
        '        "id": 1,\n'
        '        "category": "서술_완성도|차별성_부족|직무_연결_약함|성과_불명확|기간_문제|단독_활용_한계",\n'
        '        "severity": "critical|major|minor",\n'
        '        "title": "약점 제목 (10자 이내)",\n'
        '        "diagnosis": "왜 약점인지 냉정하고 구체적인 근거",\n'
        '        "evidence": "입력에서 이 약점을 판단한 구체적 근거",\n'
        '        "impact": "취업/커리어에 미치는 실질적 영향",\n'
        '        "priority_action": "지금 당장 해야 할 한 가지 행동 (동사로 시작)",\n'
        '        "improvement_example": "Before: ... → After: ... (해당 없으면 null)"\n'
        '      }\n'
        '    ],\n'
        '    "missing_elements": ["반드시 추가해야 할 누락 요소 (수치, 기간, 팀 규모 등)"],\n'
        '    "rewrite_suggestion": "이력서에 가장 효과적으로 쓰는 방법 — 구체적 표현 전략"\n'
        '  },\n'
        '  "synergy_recommendations": [\n'
        '    {\n'
        '      "priority": 1,\n'
        '      "category": "자격증|교육강의|프로젝트|대외활동|경험",\n'
        '      "name": "추천 항목명 (자격증은 화이트리스트 표기 그대로)",\n'
        '      "reason": "이 항목과 조합했을 때 시너지가 나는 구체적 이유",\n'
        '      "expected_effect": "조합 후 기대되는 커리어 효과",\n'
        '      "estimated_duration": "취득/이수 예상 소요 기간 (모르면 null)"\n'
        '    }\n'
        '  ],\n'
        '  "action_plan": {\n'
        f'    "단기": "{tc.window_label("단기")} 안에 끝낼 구체적 행동",\n'
        f'    "중기": "{tc.window_label("중기")} 안에 달성할 목표",\n'
        f'    "장기": "{tc.window_label("장기")} 커리어 방향"\n'
        '  },\n'
        '  "missing_info_warning": null\n'
        "}"
    )


# ══════════════════════════════════════════════
# 13  핵심 분석 함수
# ══════════════════════════════════════════════
def analyze_career_individual(item_text: str, tc: TimeContext | None = None) -> dict:
    """강점 + 약점 통합 심층 분석. URL 크롤링 입력 처리 포함."""
    tc = tc or TimeContext()
    time_facts = extract_time_facts(item_text, tc)

    source_hint = ""
    if item_text.startswith("[SOURCE_URL:"):
        first_line_end = item_text.find("\n")
        source_url_line = item_text[:first_line_end] if first_line_end > 0 else item_text[:100]
        source_hint = (
            f"\n[입력 소스 안내]\n"
            f"사용자가 URL을 제출했습니다: {source_url_line}\n"
            f"아래 데이터는 해당 URL에서 크롤링한 결과입니다.\n"
            f"크롤링 제한으로 일부 정보가 누락될 수 있습니다.\n"
            f"확인 불가 항목은 null 또는 빈 배열로 두고, 절대 임의로 채우지 마십시오.\n"
        )

    user_prompt = (
        "다음 단일 항목을 심층 분석하고 시너지 추천을 제시하세요.\n\n"
        f"{tc.as_prompt_block(time_facts)}\n\n"
        "━━ [강점 분석 필수 지침] ━━\n"
        "item_strengths: 입력 데이터에서 실제 확인된 사실에만 근거하여 작성.\n"
        "강점이 없거나 데이터가 빈약한 경우:\n"
        "  → has_genuine_strengths: false / strengths: []\n"
        "  → no_strength_reason 명시 / 나머지 필드: null\n"
        "evidence: 반드시 입력 텍스트에서 직접 인용 가능한 내용만 기재.\n"
        "showcase_example: Before/After 형식으로 구체적인 문장 예시 제시.\n"
        "strength_level 인플레이션 금지 — outstanding은 근거가 확실할 때만 부여.\n\n"
        "━━ [약점 분석 필수 지침] ━━\n"
        "item_diagnosis: 입력 데이터에서 실제 확인된 사실에만 근거하여 냉정하게 작성.\n"
        "존재하지 않는 약점을 만들어내지 말고, 명백한 약점을 완화하거나 숨기지도 말 것.\n"
        "improvement_example: Before/After 형식으로 구체적인 문장 예시 제시.\n\n"
        "━━ [자격증 추천 필수 지침] ━━\n"
        "category 가 '자격증'인 추천은 화이트리스트 표기를 그대로 사용.\n"
        "목록에 없으면 자격증 추천을 생략하고 다른 category 로 대체할 것.\n"
        f"{source_hint}\n"
        f"[분석 대상]\n{item_text}"
    )

    result = _call_model(
        system_prompt=build_system_prompt_individual(tc, time_facts),
        user_prompt=user_prompt,
        use_google_search=False,
    )
    if isinstance(result, dict) and result.get("status") != "error":
        result = postprocess_result(result, tc, time_facts)
    return result


# ══════════════════════════════════════════════
# 14  후처리 — 자격증 검증 + 시간 정합성 보정
# ══════════════════════════════════════════════
# ── URL 검증 ────────────────────────────────────────────────────────
#  [v1.1 한계] 소수 공식 도메인만 허용해, 환각 URL은 확실히 막았지만
#              화이트리스트에 없는 실재 기관 URL까지 함께 지워졌다.
#
#  [v1.2] 두 층으로 나눠 정확도를 올렸다.
#    (1) 등록 제한 도메인 — .go.kr(정부기관) / .ac.kr(인가 대학) / .re.kr(연구기관)
#        은 한국인터넷진흥원이 자격을 심사해야 등록되므로, 아무나 만들 수 없다.
#        도메인 자체가 기관 실재성을 보증하므로 통째로 허용한다.
#    (2) 명시 허용 도메인 — 그 밖의 자격 주관·발급 기관.
#
#  다만 호스트 검증은 "그 기관이 실재하는가"만 보증하고 "그 경로가 실재하는가"
#  는 보증하지 못한다. 경로까지 확인하려면 CAREER_VERIFY_URLS=1 을 켜면 되고,
#  이때 실제로 접속해 응답하지 않는 URL은 제거된다.

# 등록 자격 심사를 거쳐야만 취득 가능한 한국 도메인 (기관 실재성 보증)
_RESTRICTED_KR_SUFFIXES = (".go.kr", ".ac.kr", ".re.kr")

# 자격 주관·발급 기관 도메인
_ALLOWED_URL_HOSTS = (
    # 국내 자격 주관 기관
    "q-net.or.kr", "hrdkorea.or.kr", "dataq.or.kr", "kpc.or.kr",
    "kcci.or.kr", "license.kcci.or.kr", "pqi.or.kr", "kmooc.kr",
    "koreatech.ac.kr", "youthcenter.go.kr", "work.go.kr", "hrd.go.kr",
    "kirs.or.kr", "kifin.or.kr", "kofia.or.kr", "iif.or.kr",
    "kacpta.or.kr", "kicpa.or.kr", "kaa.or.kr", "kar.or.kr",
    "cbt.or.kr", "ybmnet.co.kr", "toeic.co.kr", "opic.or.kr",
    "kbs.co.kr", "historyexam.go.kr", "epis.or.kr", "kpc-cert.or.kr",
    # 국제 자격 발급 기관
    "aws.amazon.com", "learn.microsoft.com", "cloud.google.com",
    "pmi.org", "isaca.org", "isc2.org", "comptia.org", "cisco.com",
    "redhat.com", "oracle.com", "cncf.io", "linuxfoundation.org",
    "tableau.com", "databricks.com", "snowflake.com", "cfainstitute.org",
    "garp.org", "ets.org", "ielts.org", "chinesetest.cn", "jlpt.jp",
)

_VERIFY_URLS_LIVE = os.getenv("CAREER_VERIFY_URLS", "") == "1"
_URL_CHECK_CACHE: dict[str, bool] = {}


def _host_allowed(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    if not host:
        return False
    if any(host == h or host.endswith("." + h) for h in _ALLOWED_URL_HOSTS):
        return True
    return any(host.endswith(suf) for suf in _RESTRICTED_KR_SUFFIXES)


def _url_is_live(url: str) -> bool:
    """CAREER_VERIFY_URLS=1 일 때만 호출. 실제 응답하지 않는 경로를 걸러낸다."""
    if url in _URL_CHECK_CACHE:
        return _URL_CHECK_CACHE[url]
    ok = False
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "career-analysis-ai/1.2 (link-check)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            ok = resp.status < 400
    except urllib.error.HTTPError as e:
        ok = e.code < 400          # 405 등은 서버가 HEAD를 안 받는 경우
    except Exception:
        ok = False
    _URL_CHECK_CACHE[url] = ok
    if not ok:
        _log(f"  [URL 검증] 응답 없음 → 제거: {url}")
    return ok


def _check_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    if not _host_allowed(url):
        return None
    if _VERIFY_URLS_LIVE and not _url_is_live(url):
        return None
    return url


def _scrub_urls(node):
    """URL 추측 방지: 기관 실재성이 확인되지 않는 URL 필드는 전부 null 로."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if isinstance(k, str) and re.search(r"(url|link|링크|주소)", k, re.I):
                out[k] = _check_url(v)
            else:
                out[k] = _scrub_urls(v)
        return out
    if isinstance(node, list):
        return [_scrub_urls(x) for x in node]
    return node


def _scrub_terms(node, terms: list[str]) -> tuple:
    """본문 텍스트에 섞여 들어온 미검증 자격증명을 제거한다."""
    hits: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            return {k: walk(v) for k, v in n.items()}
        if isinstance(n, list):
            return [walk(x) for x in n]
        if isinstance(n, str):
            s = n
            for t in terms:
                if t and t in s:
                    hits.append(t)
                    s = s.replace(t, "[검증되지 않은 자격증명 삭제됨]")
            return s
        return n

    return walk(node), sorted(set(hits))


def filter_certifications(result: dict) -> tuple[dict, list[dict]]:
    """
    synergy_recommendations 중 category='자격증' 항목의 실재성을 검증.
    화이트리스트 미포함 항목은 제거하고 사유를 함께 반환한다.
    ("사회분석사" 같은 환각 자격증이 사용자에게 도달하지 않도록 하는 최종 관문)
    """
    recs = result.get("synergy_recommendations")
    if not isinstance(recs, list):
        return result, []

    kept: list[dict] = []
    removed: list[dict] = []
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        category = str(rec.get("category") or "")
        name = str(rec.get("name") or "")

        if "자격" in category or "certif" in category.lower():
            ok, reason = verify_certification(name)
            if not ok:
                removed.append({
                    "name": name,
                    "category": category,
                    "removed_reason": reason,
                })
                _log(f"  [자격증 필터] 제거: {name} — {reason}")
                continue
        else:
            # 다른 카테고리라도 알려진 환각 자격증명은 차단
            if not get_cert_registry().verify(name, strict=False)[0]:
                removed.append({
                    "name": name,
                    "category": category,
                    "removed_reason": "실재하지 않는 자격증명",
                })
                _log(f"  [자격증 필터] 제거: {name} — 실재하지 않는 자격증명")
                continue
        kept.append(rec)

    # priority 재부여 (구멍 방지)
    for i, rec in enumerate(sorted(kept, key=lambda r: r.get("priority") or 99), start=1):
        rec["priority"] = i

    result["synergy_recommendations"] = kept
    return result, removed


def normalize_time_fields(result: dict, tc: TimeContext, time_facts) -> tuple[dict, list[str]]:
    """
    시간 정합성 보정:
      - action_plan 을 절대 기간 + 마감일이 붙은 객체로 고정
      - 결과 전체에서 기준연도를 넘는 미래 연도 서술을 탐지해 경고
      - 기준 시각/구간을 time_context 로 동봉 (analysis_date 단독 표기의 모호함 제거)
    """
    warnings: list[str] = []

    plan = result.get("action_plan")
    if isinstance(plan, dict):
        normalized = {}
        for key in ("단기", "중기", "장기"):
            raw = plan.get(key)
            content = raw.get("내용") if isinstance(raw, dict) else raw
            normalized[key] = {
                "기간": tc.window_label(key),
                "마감일": tc.window_deadline(key),
                "내용": content if isinstance(content, str) and content.strip() else None,
            }
        # 모델이 임의 키를 추가한 경우 보존
        for key, val in plan.items():
            if key not in normalized:
                normalized[key] = val
        result["action_plan"] = normalized

    # 미래 연도 탐지 (기준연도 + 1 이상을 '이미 일어난 일'처럼 쓴 경우)
    future_re = re.compile(r"(?<!\d)(20[3-9]\d|2[1-9]\d\d)(?!\d)")

    def scan(n, path="$"):
        if isinstance(n, dict):
            for k, v in n.items():
                scan(v, f"{path}.{k}")
        elif isinstance(n, list):
            for i, v in enumerate(n):
                scan(v, f"{path}[{i}]")
        elif isinstance(n, str):
            for y in future_re.findall(n):
                if int(y) > tc.year:
                    warnings.append(f"{path}: 기준일({tc.iso_date}) 이후 연도 {y} 언급")

    scan(result)

    warnings.extend(time_facts.warnings)

    result["time_context"] = tc.as_dict()
    result["analysis_date"] = tc.iso_date          # 하위 호환 유지
    result["input_time_facts"] = time_facts.facts
    result["input_time_resolution"] = time_facts.to_dict()
    result["time_warnings"] = sorted(set(warnings)) or None
    return result, warnings


def postprocess_result(result: dict, tc: TimeContext, time_facts) -> dict:
    """LLM 출력 → 검증·정합성 보정된 최종 payload."""
    result, removed = filter_certifications(result)

    blocked_terms = [r["name"] for r in removed if r.get("name")]
    if blocked_terms:
        result, hits = _scrub_terms(result, blocked_terms)
        if hits:
            _log(f"  [본문 세정] 미검증 자격증명 {len(hits)}건 제거: {', '.join(hits)}")

    result = _scrub_urls(result)
    result, _ = normalize_time_fields(result, tc, time_facts)

    result["removed_recommendations"] = removed or None
    reg = get_cert_registry()
    result["validation"] = {
        "cert_whitelist_mode": "strict" if _STRICT_CERT_WHITELIST else "blocklist_only",
        "cert_registry_origin": reg.origin,          # live | cache | seed
        "cert_registry_fetched_at": reg.fetched_at,  # 공식 출처 마지막 수집 시각
        "verified_cert_count": len(reg.names),
        "removed_recommendation_count": len(removed),
        "time_resolution": time_facts.resolution,
        "time_basis": f"{tc.iso_datetime} (Asia/Seoul)",
    }
    return result


# ══════════════════════════════════════════════
# 15  Main
# ══════════════════════════════════════════════
# 반환 모델은 analysis_response.py 에서 import (성공/실패 분리):
#   성공 → VectorSuccessResponse(result, vector)  /  실패 → ErrorResponse(message)
def main(user_input=None):
    tc = TimeContext()          # ★ 기준 시각 단일 원천 (KST 고정)

    _log("=" * 65)
    _log("  Career Analysis AI - INDIVIDUAL Edition v1.2")
    _log(f"  모델: {_ANALYSIS_MODEL}  |  임베딩: {_EMBEDDING_MODEL}")
    _log(f"  기준시각: {tc.iso_datetime} (Asia/Seoul, 자동)")
    _log(f"  액션플랜: 단기 {tc.window_label('단기')} / "
         f"중기 {tc.window_label('중기')} / 장기 {tc.window_label('장기')}")
    _log("  [단일 경력/자격증/활동 심층 분석 전용]")
    _log("=" * 65)

    raw_content = get_user_input(user_input)

    if len(raw_content.strip()) < 5:
        resp = ErrorResponse(
            message="입력 데이터가 너무 짧습니다. 분석할 경력/자격증/활동을 입력해주세요.",
        )
        print(resp.model_dump_json(indent=2, exclude_none=True))
        return resp

    time_facts = extract_time_facts(raw_content, tc)
    _log(f"\n[0] 입력 시간 표현 해석 (resolution={time_facts.resolution})")
    for f in time_facts.facts:
        _log(f"  - {f}")
    for w in time_facts.warnings:
        _log(f"  ! {w}")

    _log("\n[1] 임베딩 벡터 생성 중...")
    try:
        vector = get_embedding(raw_content)
    except Exception as e:
        _log(f"  WARNING embedding failed: {e}")
        vector = None
    if vector:
        _log(f"  OK (dim: {len(vector)})")
    else:
        _log("  WARNING: embedding skipped (분석은 계속 진행)")

    _log(f"\n[2] 단일 항목 심층 분석 + 냉정 보완점 진단 중 ({_ANALYSIS_MODEL})...")
    result = analyze_career_individual(raw_content, tc)

    if not isinstance(result, dict) or result.get("status") == "error":
        msg = (result.get("message", "분석에 실패했습니다.")
               if isinstance(result, dict) else "분석에 실패했습니다.")
        _log(f"\n[3] 분석 실패: {msg}")
        resp = ErrorResponse(message=msg)
        print(resp.model_dump_json(indent=2, exclude_none=True))
        return resp

    result["embedding_dim"] = len(vector) if vector else None

    _log("\n[3] 분석 완료!\n")

    # ── 강점 요약 로그 ──
    sd = result.get("item_strengths") or {}
    if sd.get("has_genuine_strengths"):
        sl = sd.get("strengths") or []
        o = sum(1 for s in sl if s.get("strength_level") == "outstanding")
        n = sum(1 for s in sl if s.get("strength_level") == "notable")
        m = sum(1 for s in sl if s.get("strength_level") == "moderate")
        _log(f"  강점 {len(sl)}건 (outstanding {o} / notable {n} / moderate {m})")
    elif sd:
        _log(f"  강점 없음: {sd.get('no_strength_reason')}")

    # ── 냉정 진단 요약 로그 ──
    diag = result.get("item_diagnosis") or {}
    if diag:
        weaknesses = diag.get("weaknesses") or []
        c = sum(1 for w in weaknesses if w.get("severity") == "critical")
        j = sum(1 for w in weaknesses if w.get("severity") == "major")
        i = sum(1 for w in weaknesses if w.get("severity") == "minor")
        _log(f"  진단: {diag.get('one_line_verdict', '')}")
        _log(f"  약점 {len(weaknesses)}건 (critical {c} / major {j} / minor {i})")

    # ── 검증 결과 로그 ──
    if result.get("removed_recommendations"):
        _log(f"  자격증 필터로 제거된 추천 {len(result['removed_recommendations'])}건")
    if result.get("time_warnings"):
        for w in result["time_warnings"]:
            _log(f"  [시간 경고] {w}")

    # ── 최종 출력: 공통 envelope 형식 ──
    #   { "status": "success", "vector": [...], "result": { ...payload... } }
    #   payload 안에는 status·vector 를 넣지 않는다.
    payload = {k: v for k, v in result.items() if k not in ("status", "vector")}
    resp = VectorSuccessResponse(result=payload, vector=vector)
    print(resp.model_dump_json(indent=2, exclude_none=True))
    return resp


if __name__ == "__main__":
    # 기존 버그: main() 을 인자 없이 호출해 TypeError.
    # 인자를 주지 않으면 stdin 에서 직접 입력을 받는다.
    main()
