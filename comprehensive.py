"""
Career Analysis AI - COMPREHENSIVE Edition v2.0
================================================
종합 커리어 분석 전용 모듈 (강점 진단 통합 버전)

v1.0 대비 변경사항:
  - strength_diagnosis 섹션 추가 (critical_diagnosis 보다 먼저 출력)
  - JSON 출력 순서: ... → action_plan → strength_diagnosis → critical_diagnosis → ...
  - 분석 항목: F. 강점 진단 → G. 냉정한 보완점 진단 (기존 F가 G로 이동)
  - main() 에서 강점 요약 로그를 냉정 진단 요약보다 먼저 출력

Hallucination 방지 원칙 (strength_diagnosis 포함):
  - 입력 데이터에 없는 강점 생성 절대 금지
  - 강점이 없으면 strengths: [] + no_strength_diagnosis 에 이유·개선 방향만 기재
  - level 판단마다 evidence(근거 문장) 병기 필수
  - 과장·위로성 서술 금지 (critical_diagnosis 와 동일한 냉정함 유지)

strength_diagnosis 구조 (critical_diagnosis 와 1:1 대응):
  one_line_verdict           ↔  one_line_verdict
  strengths[].category       ↔  weaknesses[].category   (동일 7개 카테고리)
  strengths[].level          ↔  weaknesses[].severity   (outstanding/strong/notable)
  strengths[].diagnosis      ↔  weaknesses[].diagnosis
  strengths[].evidence       ↔  weaknesses[].evidence
  strengths[].impact         ↔  weaknesses[].impact
  strengths[].leverage_action ↔ weaknesses[].priority_action
  no_strength_diagnosis      ↔  missing_experience_types (강점 없음 사유)
  content_quality_highlights ↔  content_quality_issues
  competitor_advantage       ↔  competitor_gap
"""

import json
import re
import os
import io
import time
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
from datetime import datetime, date
from collections import deque

from google import genai
from google.genai import types
import pypdf

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
GEMINI_API_KEY   = ""
client           = genai.Client(api_key=GEMINI_API_KEY)

_ANALYSIS_MODEL  = "gemini-2.5-pro"
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


# ══════════════════════════════════════════════
# 1  LLM 호출 헬퍼
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
                print(f"  [Rate Limit] {wait}초 후 재시도 ({attempt + 1}/{_MAX_RETRIES - 1})...", flush=True)
                time.sleep(wait)
            else:
                raise


def _call_model(system_prompt: str, user_prompt: str,
                use_google_search: bool = False) -> dict:
    raw_text = ""
    try:
        tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None

        def _do():
            return client.models.generate_content(
                model=_ANALYSIS_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt if system_prompt else None,
                    temperature=0.1,
                    tools=tools,
                ),
            )

        resp = _call_with_retry(_do)
        raw_text = resp.text.strip()
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
            return client.models.generate_content(
                model=_ANALYSIS_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt if system_prompt else None,
                    temperature=0.0,
                    tools=tools,
                ),
            )

        resp = _call_with_retry(_do)
        return resp.text.strip()
    except Exception as e:
        return f"ERROR: {e}"


# ══════════════════════════════════════════════
# 2  JSON 정제
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
# 3  학교 정보
# ══════════════════════════════════════════════
_SCHOOL_ALIASES = {
    "서울대": "서울대학교", "연세대": "연세대학교", "고려대": "고려대학교",
    "성균관대": "성균관대학교", "한양대": "한양대학교", "서강대": "서강대학교",
    "중앙대": "중앙대학교", "경희대": "경희대학교", "이화여대": "이화여자대학교",
    "한국외대": "한국외국어대학교", "외대": "한국외국어대학교",
    "시립대": "서울시립대학교", "건국대": "건국대학교", "동국대": "동국대학교",
    "홍익대": "홍익대학교", "숭실대": "숭실대학교", "국민대": "국민대학교",
    "세종대": "세종대학교", "광운대": "광운대학교", "명지대": "명지대학교",
    "인하대": "인하대학교", "아주대": "아주대학교", "부산대": "부산대학교",
    "경북대": "경북대학교", "전남대": "전남대학교", "충남대": "충남대학교",
    "충북대": "충북대학교", "전북대": "전북대학교", "강원대": "강원대학교",
    "제주대": "제주대학교", "울산대": "울산대학교", "한림대": "한림대학교",
    "단국대": "단국대학교", "상명대": "상명대학교", "가톨릭대": "가톨릭대학교",
    "카이스트": "KAIST", "포항공대": "POSTECH",
}


def normalize_school_name(name: str) -> str:
    if not name:
        return ""
    return _SCHOOL_ALIASES.get(name.strip(), name.strip())


def get_user_profile(raw_school: str, raw_dept: str) -> tuple[str, str]:
    print("\n[학교 / 학과 정보 입력]")
    print("─" * 45)
    school = normalize_school_name(raw_school) if raw_school else ""
    print(f"  -> 학교: {school}" if school else "  -> 학교 정보 없이 진행합니다.")
    department = raw_dept if raw_dept else ""
    print(f"  -> 학과: {department}" if department else "  -> 학과 정보 없이 진행합니다.")
    print("─" * 45)
    return school, department


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
    print(f"    __NEXT_DATA__ 파싱 성공: {len(blocks)}개 블록, {len(text)} chars", flush=True)
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
            val = val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
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
    print(f"  [Notion 크롤러] 시작: {url}", flush=True)
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

    if html:
        parser = _TextExtractor(base_url=url)
        parser.feed(html)
        plain_text = parser.text.strip()
        if plain_text and len(plain_text) > 100:
            collected_parts.append(f"[Notion HTML 텍스트: {url}]\n{plain_text}")

    combined = "\n\n".join(collected_parts).strip()
    print(f"    추출 결과: {len(combined)} chars", flush=True)

    if len(combined) < _MIN_CRAWL_CHARS:
        print(f"    WARNING: 결과 빈약 → URL 힌트 보강", flush=True)
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

    print(f"  [Notion 크롤러 완료] 총 {len(combined)} chars", flush=True)
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
        print(f"    WARNING fetch failed [{url}]: {e}", flush=True)
        return None


def _parse_pdf_bytes(data: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        print(f"    OK PDF parsed ({len(reader.pages)} pages, {len(text)} chars)", flush=True)
        return text
    except Exception as e:
        print(f"    WARNING PDF parse error: {e}", flush=True)
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
    print(f"  [딥 크롤러] 시작: {start_url}", flush=True)
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
        print(f"    Main page: {len(main_text)} chars", flush=True)

    if _is_spa(main_html):
        print("    SPA detected — using raw HTML link extraction", flush=True)

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

    print(f"    큐 초기 크기: {len(queue)}개 링크", flush=True)
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
                print(f"    PDF 한도 도달, 건너뜀: {url}", flush=True)
                continue
            text = _parse_pdf_bytes(sub_raw)
            if text.strip():
                collected.append(f"[PDF: {url}]\n{text}")
                pdf_count += 1
                print(f"    PDF collected ({pdf_count}/{_MAX_PDFS}): {url}", flush=True)
            continue

        if page_count >= _MAX_PAGES:
            continue
        sub_text, sub_links, sub_html = _parse_html_bytes(sub_raw, url)
        page_count += 1
        if sub_text.strip():
            collected.append(f"[Page: {url}]\n{sub_text}")
            print(f"    Page {page_count}: {url} ({len(sub_text)} chars)", flush=True)

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
    print(f"  [딥 크롤러 완료] {len(collected)}개 소스, {len(result)} chars", flush=True)
    return result


# ══════════════════════════════════════════════
# 7  파일 리더
# ══════════════════════════════════════════════
def read_file(path: str) -> str:
    if not os.path.isfile(path):
        print(f"  ERROR file not found: {path}", flush=True)
        return ""
    if path.lower().endswith(".pdf"):
        with open(path, "rb") as f:
            return _parse_pdf_bytes(f.read())
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        print(f"  OK file read ({len(content)} chars)", flush=True)
        return content
    except Exception as e:
        print(f"  ERROR reading file: {e}", flush=True)
        return ""


# ══════════════════════════════════════════════
# 8  입력 수집
# ══════════════════════════════════════════════
def _looks_like_url(s: str) -> bool:
    return bool(re.match(r"^https?://\S+", s) or re.match(r"^www\.\S+", s))


def _looks_like_filepath(s: str) -> bool:
    return "\n" not in s and os.path.isfile(s.strip())


def get_user_input(lines: list[str]) -> str:
    print("\n" + "-" * 65)
    print("  경력/활동/자격증 정보를 입력하세요.")
    print("  URL(노션 포함), 파일 경로, 또는 직접 텍스트 모두 가능합니다.")
    print("  입력 완료 후 빈 줄에서 END 를 입력하세요.")
    print("-" * 65 + "\n")

    raw = "\n".join(lines).strip()
    if not raw:
        return ""

    first_line = lines[0].strip() if lines else ""

    if len(lines) == 1 and _looks_like_url(first_line):
        url = first_line if first_line.startswith("http") else "https://" + first_line

        if _is_notion_url(url):
            print(f"\n  [자동 감지] Notion URL → Notion 전용 크롤러 시작", flush=True)
            content = crawl_notion_page(url)
        else:
            print(f"\n  [자동 감지] URL → 딥 크롤링 시작", flush=True)
            content = deep_crawl_site(url)

        if content.strip():
            return f"[SOURCE_URL: {url}]\n\n{content}"
        else:
            print(f"  WARNING: 크롤링 실패 → URL 힌트 모드로 진행", flush=True)
            return (
                f"[SOURCE_URL: {url}]\n"
                f"[크롤링 실패]\n"
                f"URL: {url}\n"
                f"URL 경로명과 도메인을 바탕으로 내용을 최대한 추론하십시오.\n"
                f"추론 불가 항목은 빈 배열로 반환하십시오."
            )

    if len(lines) == 1 and _looks_like_filepath(first_line):
        print(f"\n  [자동 감지] 파일 경로 → 읽는 중: {first_line}", flush=True)
        content = read_file(first_line.strip())
        return content if content.strip() else raw

    print(f"\n  [자동 감지] 텍스트 직접 입력 ({len(raw)}자)", flush=True)
    return raw


# ══════════════════════════════════════════════
# 9  Embedding
# ══════════════════════════════════════════════
def get_embedding(text: str):
    if not text:
        return None
    truncated = text[:10000]
    for kwargs in [
        {"content": truncated, "config": types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")},
        {"content": truncated},
        {"contents": truncated, "config": types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")},
    ]:
        try:
            result = client.models.embed_content(model=_EMBEDDING_MODEL, **kwargs)
            if hasattr(result, "embeddings") and result.embeddings:
                return result.embeddings[0].values
            if hasattr(result, "embedding") and result.embedding:
                return result.embedding.values
        except Exception:
            continue
    print("  WARNING embedding all fallbacks failed", flush=True)
    return None


# ══════════════════════════════════════════════
# 10  Hallucination 방지 규칙 (공통)
# ══════════════════════════════════════════════
_STRICT_HALLUCINATION_RULES = """
=== STRICT MODE: Hallucination 절대 금지 ===
규칙 1. 실재 확인 불가 항목은 생성 금지
  - 공모전·프로젝트: 코드 실행 시점(오늘) 기준으로 현재 접수 중이거나
    정기 개최(매년/반기)가 확인된 것만. 이름·주관기관이 불확실하면 배열에서 제외.
    과거에 종료된 공모전을 현재형으로 서술하지 말 것.
  - 자격증: 국가공인 또는 민간자격증으로 현재 실제 시행 중인 것만.
  - 동아리·학회: 지정 학교에 현재 실재하는 것만.
  - 채용공고: URL이 확인된 공고만.

규칙 2. URL 생성 금지
  - URL은 기억 속에 확실한 공식 URL만 허용. 추측·조합·변형 절대 금지.
  - 불확실하면 반드시 null (빈 문자열 "" 금지).

규칙 3. 동아리·학회 최소 추천 수
  - 학교 정보가 있을 때: 교내·연합·외부를 합산하여 최소 3개 이상 추천할 것.
  - 교내동아리/교내학회, 연합동아리/연합학회, 외부학회 중 가능한 타입을 균형 있게 포함.
  - 실재 여부가 확실하지 않으면 해당 항목은 제외하되, 제외로 인해 3개 미만이 되면
    실재가 확인 가능한 다른 항목으로 보충하여 3개를 채울 것.
  - 학교 정보가 없을 때: 연합동아리·외부학회 위주로 최소 2개 이상 추천.

규칙 4. 빈 배열 우선 (동아리 제외)
  - 동아리 이외 항목(자격증, 공모전, 채용공고): 추천할 항목이 없거나 확인 불가이면 [] 반환.
  - 채우기 위해 임의로 만들어내는 것은 빈 배열보다 훨씬 나쁘다.

규칙 5. 연도·마감일 날조 금지
  - 공모전 마감일·채용 마감일을 모르면 null 또는 "미정".
  - 연도를 추측해서 채우지 말 것.

규칙 6. 출력은 순수 JSON만
  - 마크다운 코드블록, 설명 텍스트, 주석 절대 포함 금지.

규칙 7. STAR 분석은 필수가 아님.
  - STAR 분석 시 분석 할 데이터가 부족하면, 절대 임의로 이야기를 지어내지말 것.
  - 만약 STAR 분석을 하기에 데이터가 부족하다면, 데이터가 부족하니 STAR 분석이 이루어지지 않았음을 텍스트로 띄우기.
  - STAR 분석을 생성해서 결과물을 보이는 것보다, 냉철한 판단 기준을 기반으로 데이터가 부족하면, 부족하기에 결과값을 띄울 수 없으며 예시로 어떻게 기록을 하면 좋을지 Comment 달기.

규칙 8. strength_diagnosis Hallucination 금지 (v2.0 추가)
  - 입력 데이터에 없는 강점 생성 절대 금지
  - 강점이 없으면 strengths: [] 반환 후 no_strength_diagnosis 에 이유·개선 방향만 기재
  - 모든 경험에 의례적으로 강점을 부여하는 행위 금지 (객관성 필수)
  - 없는 강점을 억지로 생성하는 것은 빈 배열 반환보다 훨씬 나쁜 결과물이다.

규칙 9. critical_diagnosis 약점 보존 의무 (v2.0 추가)
  - strength_diagnosis 를 먼저 출력했다는 이유로 critical_diagnosis 의 약점을 축소하거나 은폐하는 행위 절대 금지
  - 강점이 많거나 뛰어나더라도 약점 섹션의 완성도와 냉정함은 독립적으로 유지
  - 강점의 존재가 약점의 severity 를 낮출 근거가 되지 않는다
  - 두 섹션은 서로 영향을 주지 않으며, critical_diagnosis 는 강점과 무관하게 입력 데이터만 기반으로 판단
  - 약점을 긍정적으로 포장하거나 "~이지만 괜찮다" 식의 완화 표현 사용 금지
"""


# ══════════════════════════════════════════════
# 11  시스템 프롬프트 빌더 (v2.0 — strength 먼저, critical 뒤)
# ══════════════════════════════════════════════
def build_system_prompt_comprehensive(ref_date: date, school: str, department: str = "") -> str:
    rd = ref_date.strftime("%Y-%m-%d")
    school_str = school if school else "정보 없음"
    dept_str   = department if department else "정보 없음"

    club_count_rule = (
        f"- 동아리·학회는 반드시 '{school}'에 실재하는 것만 추천\n"
        f"- 다른 학교 동아리·학회 추천 절대 금지\n"
        f"- 연합동아리는 '{school}' 지부 존재가 확인된 것만\n"
        f"- 교내·연합·외부 타입을 균형 있게 섞어 최소 3개 이상 추천\n"
        if school else
        "- 학교 불명: 교내 동아리·학회 추천 금지, 연합·외부 위주로 최소 2개 이상 추천\n"
    )
    dept_rules = (
        f"- 자격증·공모전·동아리 추천 시 '{dept_str}' 전공·커리어 경로 최우선 반영\n"
        if department else ""
    )

    return (
        "당신은 대한민국 최고의 커리어 컨설턴트이자 데이터 분석가입니다.\n"
        f"코드 실행(분석) 기준일: {rd} — 이 날짜를 '오늘'로 간주하십시오.\n"
        f"\n=== 사용자 프로필 ===\n학교: {school_str}\n학과: {dept_str}\n"
        f"{club_count_rule}"
        f"{dept_rules}"
        f"{_STRICT_HALLUCINATION_RULES}\n"
        # ── 분석 항목 목록 (F=강점, G=약점 순서) ────────────────────
        "=== 분석 항목 ===\n"
        "A. 역량 클러스터링 + STAR 이력서 초안\n"
        "B. 보유 항목 간 시너지 조합 (2~5개)\n"
        "C. 추가 추천: 자격증, 동아리·학회(최소 3개), 공모전(현재 시점 기준)\n"
        f"D. 채용공고 추천 ({rd} 이후 마감, URL 확인된 것만)\n"
        "E. 단기·중기·장기 액션 플랜\n"
        "F. 강점 진단 (strength_diagnosis) — 아래 지침 엄수\n"
        "G. 냉정한 보완점 진단 (critical_diagnosis) — 아래 지침 엄수\n\n"
        # ── 강점 진단(F) 작성 지침 ──────────────────────────────────
        "=== 강점 진단(F) 작성 지침 ===\n"
        "목적: 입력 데이터에서 실제로 확인되는 진짜 강점만을 객관적으로 도출한다.\n"
        "절대 금지:\n"
        "  - 위로·칭찬 목적의 과장, 없는 내용으로 강점 창작\n"
        "  - 모든 경험에 의례적으로 강점을 부여하는 행위 (객관성 필수)\n"
        "  - strengths 배열을 채우기 위해 근거 없는 항목을 생성하는 행위\n"
        "판단 기준 (모두 독립적으로 평가, 해당하는 항목만 포함):\n"
        "  [활동_수량] 직군 평균 대비 활동 총 개수가 풍부한가?\n"
        "    → 구체적 수치로 표현 (예: '인턴 경험 2회 — 해당 직군 지원자 평균 1.5회 상회')\n"
        "  [활동_깊이] 각 항목의 서술이 구체적이고 수치·성과가 명확한가?\n"
        "    → 어떤 항목이, 왜 강한지 입력 근거와 함께 명시\n"
        "  [직무_연관성] 지원 직군과 연관된 핵심 활동이 있는가?\n"
        "    → 어떤 직무 카테고리가 잘 갖춰져 있는지 명시\n"
        "  [스킬_보유] 해당 직군에서 요구하는 기술·자격을 보유하고 있는가?\n"
        "    → 보유 스킬이 직무 요구와 얼마나 일치하는지 설명\n"
        "  [기간_연속성] 경력이 일관되고 성장 흐름이 있는가?\n"
        "    → 일관된 방향성이나 시간적 심화 흐름이 확인될 경우 명시\n"
        "  [서류_품질] 입력된 내용이 이력서로서 설득력 있게 작성되어 있는가?\n"
        "    → 구체적으로 어떤 표현·항목이 강점인지 명시\n"
        "  [경쟁력_우위] 동일 직군·학교 수준 경쟁자 대비 차별화 포인트가 있는가?\n"
        "    → 경쟁자들이 보통 갖지 못한 것 중 보유한 것을 명시\n"
        "level 기준 (critical_diagnosis 의 severity 와 1:1 대응):\n"
        "  outstanding : 이 강점 하나만으로도 서류 합격 가능성을 크게 높임   ↔ critical\n"
        "  strong      : 합격 가능성을 뚜렷이 높이는 실질적 강점              ↔ major\n"
        "  notable     : 긍정 인상을 주지만 당장 결정적이지는 않은 장점         ↔ minor\n"
        "leverage_action: 이 강점을 더욱 극대화하기 위해 지금 당장 해야 할 한 가지 행동 (동사로 시작)\n"
        "[강점이 없거나 식별이 어려운 경우] — 솔직하게 기재, 절대 창작 금지:\n"
        "  - strengths 배열을 반드시 빈 배열 [] 로 반환\n"
        "  - no_strength_diagnosis.has_issue = true 로 설정\n"
        "  - reason: 강점 식별 불가 이유를 입력 데이터 기반으로 냉정하게 기재\n"
        "      예) '경험 수 1개로 역량 일관성 및 강점 패턴 식별 불가'\n"
        "          '활동 내용이 모두 1~2줄로 짧아 강점 판단 근거 부족'\n"
        "          '성과 수치가 전혀 없어 강점의 크기를 판단할 근거 없음'\n"
        "  - improvement_direction: 어떻게 입력을 보완하거나 활동을 추가하면 강점이 생기는지\n"
        "      1~2가지 구체적 방향 제시\n"
        "  - 없는 강점을 억지로 생성하는 것은 빈 배열 반환보다 훨씬 나쁜 결과물이다.\n\n"
        # ── 냉정한 보완점 진단(G) 작성 지침 (v2.1 — 강점 섹션과 동등한 깊이로 확충) ──
        "=== 냉정한 보완점 진단(G) 작성 지침 ===\n"
        "목적: 사용자가 듣기 불편하더라도 반드시 알아야 할 진짜 약점을 직시하게 하는 것.\n"
        "절대 금지:\n"
        "  - 칭찬·위로·긍정적 포장 — G 섹션에는 단 한 줄의 좋은 말도 하지 말 것\n"
        "  - strength_diagnosis 를 먼저 작성했다는 이유로 약점을 축소하거나 생략하는 행위\n"
        "  - 강점이 있다는 이유로 약점 severity 를 낮추거나 완화하는 행위\n"
        "  - 입력 데이터에 없는 약점을 창작하는 행위 (실제 확인된 사실만 기재)\n"
        "  - '~이지만 괜찮습니다', '~에도 불구하고' 등의 완화 표현 사용\n"
        "판단 기준 (모두 독립적으로 평가하고, 해당하는 항목만 포함):\n"
        "  [활동_수량] 직군 평균 대비 활동 총 개수가 부족한가?\n"
        "    → 구체적 수치로 표현 (예: '인턴 경험 0회 — 해당 직군 지원자 평균 1.5회')\n"
        "  [활동_깊이] 각 항목의 서술 내용이 너무 짧거나 수치/성과가 없는가?\n"
        "    → 어떤 항목이, 왜 얕은지 구체적으로 지적\n"
        "  [직무_연관성] 지원 직군과 무관한 활동만 있거나, 핵심 직무 경험이 비어있는가?\n"
        "    → 빈 직무 카테고리를 명시 (예: '데이터 분석 직군인데 SQL 경험 전무')\n"
        "  [스킬_공백] 해당 직군에서 필수로 요구되는 기술/자격이 보이지 않는가?\n"
        "    → 없는 스킬을 열거, 왜 치명적인지 설명\n"
        "  [기간_연속성] 경력 공백이 있거나 활동들 사이 단절이 심각한가?\n"
        "    → 공백 기간을 명시\n"
        "  [서류_품질] 입력된 내용이 이력서로서 설득력 없게 작성되어 있는가?\n"
        "    → 구체적으로 어떤 표현/항목이 문제인지 지적\n"
        "  [경쟁력_격차] 동일 직군·학교 수준 경쟁자 대비 눈에 띄는 차별점이 없는가?\n"
        "    → 경쟁자들이 보통 갖고 있는 것 중 없는 것을 열거\n"
        "severity 기준 (strength_diagnosis 의 level 과 1:1 대응):\n"
        "  critical   : 이 상태로 지원하면 서류 탈락 가능성 높음     ↔ outstanding\n"
        "  major      : 합격 가능성을 뚜렷이 낮추는 약점              ↔ strong\n"
        "  minor      : 있으면 좋지만 없어도 당장 치명적이지 않은 부족함 ↔ notable\n"
        "priority_action: 해당 약점을 개선하기 위해 지금 당장 해야 할 한 가지 행동 (동사로 시작)\n"
        "[앵커링 저항 원칙] — strength_diagnosis 먼저 출력 후 이 섹션을 작성할 때:\n"
        "  - 앞서 작성한 강점 내용은 이 섹션 판단에 영향을 주지 않는다\n"
        "  - 강점이 많다고 느껴질수록 약점을 더 의식적으로 냉정하게 기재할 것\n"
        "  - 두 섹션의 결과물은 서로 독립적 — 강점이 약점을 상쇄하지 않는다\n"
        "[약점이 없거나 경미한 경우]:\n"
        "  - 진짜 약점이 전혀 없는 경우에만 weaknesses: [] (매우 드문 경우, 신중히 판단)\n"
        "  - minor 수준이라도 발견되면 반드시 포함 — 생략은 사용자에게 불이익\n"
        "  - weaknesses 배열을 비우는 것 자체가 '약점 없음'을 의미하므로 극도로 신중히 결정\n\n"
        # ── [1순위 수정] 약점 보존 원칙 — Anchoring Effect 방어 ────────
        "[약점 보존 원칙]\n"
        "strength_diagnosis 를 먼저 출력했더라도 critical_diagnosis 는 강점과 완전히 독립적으로 판단할 것.\n"
        "강점이 많다고 약점을 축소하거나 생략하지 말 것.\n"
        "두 섹션은 서로 영향을 주지 않는다 — 강점은 약점을 상쇄하지 않는다.\n\n"
        "[출력] 순수 JSON만 (코드블록·설명 금지)\n\n"
        "{\n"
        '  "status": "success",\n'
        f'  "user_school": "{school_str}",\n'
        f'  "user_department": "{dept_str}",\n'
        '  "brief_summary": "핵심 한 줄 요약",\n'
        '  "detailed_summary": "심층 요약",\n'
        '  "keyword_clustering": {\n'
        '    "personality_tendency": ["성향"],\n'
        '    "core_competency": ["역량"],\n'
        '    "job_industry": ["직군"]\n'
        "  },\n"
        '  "experience_insights": {"motivation": "동기", "learning_points": "배움"},\n'
        '  "synergy_combinations": [\n'
        '    {"combination_title":"조합명","items":["항목1","항목2"],'
        '"synergy_reason":"이유","expected_effect":"효과","applicable_roles":["직무"]}\n'
        "  ],\n"
        '  "additional_recommendations": {\n'
        '    "certifications": [\n'
        '      {"name":"자격증명","reason":"연계 이유","expected_effect":"기대 효과",'
        '"estimated_duration":"취득 소요 기간"}\n'
        "    ],\n"
        '    "clubs_and_societies": [\n'
        "      {\n"
        '        "name": "동아리/학회명",\n'
        '        "type": "교내동아리/교내학회/연합동아리/연합학회/외부학회",\n'
        f'        "school_affiliation": "{school_str}",\n'
        '        "description": "실제 알고 있는 활동 내용 (불확실하면 빈 문자열)",\n'
        '        "reason": "추천 이유",\n'
        '        "expected_effect": "기대 효과",\n'
        '        "url": null,\n'
        '        "search_query": "검증용 검색어",\n'
        '        "search_verified": false\n'
        "      }\n"
        "    ],\n"
        '    "projects_and_contests": [\n'
        "      {\n"
        '        "name": "공모전명 (주관기관이 확인된 것만)",\n'
        '        "organizer": "주관기관명",\n'
        '        "reason": "추천 이유",\n'
        '        "expected_effect": "기대 효과",\n'
        '        "url": null,\n'
        '        "deadline": null,\n'
        '        "is_regular": true\n'
        "      }\n"
        "    ]\n"
        "  },\n"
        '  "resume_star_format": [\n'
        '    {"title":"경험명","S":"상황","T":"과제","A":"행동","R":"결과"}\n'
        "  ],\n"
        '  "action_plan": {"단기":"3개월 이내","중기":"6개월~1년","장기":"1년 이상"},\n'
        # ── [v2.0] strength_diagnosis — critical_diagnosis 보다 먼저 ──
        '  "strength_diagnosis": {\n'
        '    "one_line_verdict": "현재 이력 강점의 전반적 상태를 한 문장으로 (강점이 없으면 솔직하게 기재)",\n'
        '    "strengths": [\n'
        "      {\n"
        '        "id": 1,\n'
        '        "category": "활동_수량|활동_깊이|직무_연관성|스킬_보유|기간_연속성|서류_품질|경쟁력_우위",\n'
        '        "level": "outstanding|strong|notable",\n'
        '        "title": "강점 제목 (10자 이내, 핵심만)",\n'
        '        "diagnosis": "왜 강점인지 냉정하고 구체적인 근거 (입력 데이터에서 확인된 사실 기반, 창작 금지)",\n'
        '        "evidence": "이 강점을 판단한 구체적 근거 (입력 텍스트 인용 또는 확인된 사실)",\n'
        '        "impact": "이 강점이 취업·커리어에 미치는 실질적 긍정 영향",\n'
        '        "leverage_action": "이 강점을 극대화하기 위해 지금 당장 해야 할 한 가지 구체적 행동 (동사로 시작)"\n'
        "      }\n"
        "    ],\n"
        '    "no_strength_diagnosis": {\n'
        '      "has_issue": false,\n'
        '      "reason": "강점 없음·식별 불가 이유 (강점이 있으면 빈 문자열 반환)",\n'
        '      "improvement_direction": "강점을 만들기 위한 구체적 개선 방향 (강점이 있으면 빈 문자열 반환)"\n'
        "    },\n"
        '    "standout_experience_types": [\n'
        '      "현재 이력에서 특히 돋보이는 경험 유형 (없으면 빈 배열)"\n'
        "    ],\n"
        '    "content_quality_highlights": [\n'
        "      {\n"
        '        "item": "잘 작성된 특정 이력·서술 항목명",\n'
        '        "highlight": "구체적으로 무엇이 강한가 (수치 있음·역할 명확·성과 구체적 등)",\n'
        '        "why_effective": "이 표현이 채용담당자에게 설득력 있는 이유"\n'
        "      }\n"
        "    ],\n"
        '    "competitor_advantage": "동일 직군·학교 수준 경쟁자 대비 현재 포트폴리오의 결정적 차별점 (없으면 \'현재 데이터 기준 명확한 차별점 식별 불가\'로 솔직하게 기재)"\n'
        "  },\n"
        # ── critical_diagnosis (기존 동일) ──────────────────────────
        '  "critical_diagnosis": {\n'
        '    "one_line_verdict": "현재 이력 전반의 상태를 냉정하게 한 문장으로 (예: 활동은 있으나 직무 연관성과 깊이가 모두 부족한 상태)",\n'
        '    "weaknesses": [\n'
        "      {\n"
        '        "id": 1,\n'
        '        "category": "활동_수량|활동_깊이|직무_연관성|스킬_공백|기간_연속성|서류_품질|경쟁력_격차",\n'
        '        "severity": "critical|major|minor",\n'
        '        "title": "약점 제목 (10자 이내, 핵심만)",\n'
        '        "diagnosis": "왜 약점인지 냉정하고 구체적인 근거 (입력 데이터에서 확인된 사실 기반)",\n'
        '        "evidence": "입력에서 이 약점을 판단한 구체적 근거 (인용 또는 없음)",\n'
        '        "impact": "이 약점이 취업/커리어에 미치는 실질적 영향",\n'
        '        "priority_action": "지금 당장 해야 할 한 가지 구체적 행동 (동사로 시작)"\n'
        "      }\n"
        "    ],\n"
        '    "missing_experience_types": [\n'
        '      "현재 이력에서 완전히 빠진 경험 유형 (예: 인턴십, 팀 프로젝트, 수상 이력 등)"\n'
        "    ],\n"
        '    "content_quality_issues": [\n'
        "      {\n"
        '        "item": "문제가 있는 특정 이력/서술 항목명",\n'
        '        "issue": "구체적으로 무엇이 부실한가 (수치 없음/기간 불명/역할 불명 등)",\n'
        '        "improvement_hint": "이렇게 바꾸면 설득력이 생긴다 — 구체적 표현 예시 제시"\n'
        "      }\n"
        "    ],\n"
        '    "competitor_gap": "동일 직군·학교 수준 경쟁자 대비 현재 포트폴리오가 갖지 못한 결정적 차이점"\n'
        "  },\n"
        '  "valid_job_recommendations": [\n'
        "    {\n"
        '      "company": "회사명",\n'
        '      "role": "직무",\n'
        '      "deadline": "YYYY-MM-DD 또는 상시채용",\n'
        '      "why_match": "매칭 근거",\n'
        '      "url": null\n'
        "    }\n"
        "  ],\n"
        '  "missing_info_warning": null\n'
        "}"
    )


# ══════════════════════════════════════════════
# 12  검증 프롬프트 빌더
# ══════════════════════════════════════════════
def build_prompt_verify_clubs(school: str, clubs: list) -> str:
    club_list = "\n".join(
        f"  {i+1}. {c.get('name','?')} (type={c.get('type','?')})"
        for i, c in enumerate(clubs)
    )
    return (
        f"사용자 학교: '{school}'\n\n"
        f"[검증 대상]\n{club_list}\n\n"
        "Google Search로 각 항목을 검색하여:\n"
        "1. 실제 존재하는지 (verified: true/false)\n"
        "2. 다른 학교에만 있는지 (wrong_school: true/false)\n"
        "3. 공식 URL (확인되면 기입, 아니면 null)\n"
        "4. description: 확인한 실제 활동 내용 (미확인이면 빈 문자열)\n\n"
        "순수 JSON 배열만 출력:\n"
        '[\n  {"name":"동아리명","verified":true,"wrong_school":false,'
        '"actual_school":"확인된 소속 학교","official_url":null,'
        '"description":"","evidence":"근거"}\n]'
    )


def build_prompt_verify_contests(contests: list, ref_date: date) -> str:
    rd = ref_date.strftime("%Y-%m-%d")
    c_list = "\n".join(
        f"  {i+1}. [{c.get('organizer','?')}] {c.get('name','?')}"
        for i, c in enumerate(contests)
    )
    return (
        f"오늘 날짜(기준일): {rd}\n\n"
        f"[검증 대상 공모전/프로젝트]\n{c_list}\n\n"
        f"Google Search로 각 공모전을 '{rd}' 기준으로 실제 검색하여:\n"
        f"1. 실제 존재하고 정기 개최되는지 (verified: true/false)\n"
        f"2. 주관기관이 실제 해당 기관인지 (organizer_confirmed: true/false)\n"
        f"3. {rd} 기준으로 현재 접수 중이거나 {rd} 이후 개최 예정인지\n"
        f"   (upcoming: true/false) — 이미 종료된 공모전은 반드시 false\n"
        f"4. 마감일이 {rd} 이후이거나 상시 운영인지 (deadline_ok: true/false)\n"
        f"5. 공식 URL (확인되면 기입, 아니면 null)\n"
        f"6. 마감일 (확인되면 YYYY-MM-DD, 모르면 null)\n\n"
        "순수 JSON 배열만 출력:\n"
        '[\n  {"name":"공모전명","verified":true,"organizer_confirmed":true,'
        '"upcoming":true,"deadline_ok":true,"official_url":null,'
        '"deadline":null,"evidence":"근거"}\n]'
    )


def build_prompt_verify_certifications(certs: list) -> str:
    c_list = "\n".join(
        f"  {i+1}. {c.get('name','?')}"
        for i, c in enumerate(certs)
    )
    return (
        f"[검증 대상 자격증]\n{c_list}\n\n"
        "각 자격증에 대해:\n"
        "1. 실제 시행 중인 국가공인 또는 민간자격증인지 (verified: true/false)\n"
        "2. 주관기관 (issuer)\n"
        "3. 공식 URL (확인되면 기입, 아니면 null)\n\n"
        "순수 JSON 배열만 출력:\n"
        '[\n  {"name":"자격증명","verified":true,"issuer":"주관기관","official_url":null}\n]'
    )


def build_prompt_verify_jobs(jobs: list, ref_date: date) -> str:
    rd = ref_date.strftime("%Y-%m-%d")
    job_list = "\n".join(
        f"  {i+1}. {j.get('company','?')} | {j.get('role','?')} | "
        f"마감: {j.get('deadline','?')} | URL: {j.get('url','?')}"
        for i, j in enumerate(jobs)
    )
    return (
        f"기준일: {rd}\n\n"
        f"[검증 대상]\n{job_list}\n\n"
        "Google Search로 각 채용공고를 실제 검색하여:\n"
        "1. 실제 존재하는지 (verified)\n"
        f"2. 마감일이 {rd} 이후인지 (deadline_confirmed)\n"
        "3. 실제 채용공고 URL (확인되면 기입, 아니면 null)\n\n"
        "순수 JSON 배열만 출력:\n"
        '[\n  {"company":"회사명","role":"직무","verified":true,'
        '"deadline_confirmed":true,"correct_url":null,"evidence":"근거"}\n]'
    )


# ══════════════════════════════════════════════
# 13  채용공고 URL 접근성 검증
# ══════════════════════════════════════════════
def _verify_url_content(url: str, company: str, role: str, timeout: int = 10) -> dict:
    res = {"accessible": False, "content_match": False, "reason": ""}
    if not url or not url.startswith("http"):
        res["reason"] = "유효하지 않은 URL"
        return res
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; CareerBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                res["reason"] = f"HTTP {resp.status}"
                return res
            res["accessible"] = True
            page = resp.read(80_000).decode("utf-8", errors="replace").lower()

        co = company.lower().strip()
        co_clean = re.sub(r"\(주\)|주식회사|\s+", "", co)
        co_found = co in page or co_clean in page

        role_kws = [k for k in re.split(r"[/\s·,]+", role.lower()) if len(k) >= 2]
        role_found = sum(1 for k in role_kws if k in page) >= max(1, len(role_kws) // 2)

        job_kws = ["채용", "지원", "모집", "recruit", "career", "apply", "job", "hiring"]
        is_job = any(k in page for k in job_kws)

        if co_found and (role_found or is_job):
            res["content_match"] = True
        else:
            reasons = []
            if not co_found:
                reasons.append(f"회사명 '{company}' 미발견")
            if not role_found and not is_job:
                reasons.append("채용 관련 내용 미발견")
            res["reason"] = ", ".join(reasons)
    except urllib.error.HTTPError as e:
        res["reason"] = f"HTTP {e.code}"
    except Exception as e:
        res["reason"] = str(e)[:80]
    return res


def verify_job_urls(jobs: list) -> tuple[list, list]:
    verified, failed = [], []
    for i, job in enumerate(jobs, 1):
        url = job.get("url") or ""
        company = job.get("company", "")
        role = job.get("role", "")
        label = f"{company} | {role}"
        print(f"    [{i}/{len(jobs)}] {label}", flush=True)

        if not url or url == "null":
            print(f"           -> FAIL: URL 미확보 -> 제외", flush=True)
            job["_fail_reason"] = "URL 미확보"
            failed.append(job)
            continue

        print(f"           {url}", flush=True)
        check = _verify_url_content(url, company, role)
        if check["accessible"] and check["content_match"]:
            print(f"           -> OK", flush=True)
            verified.append(job)
        else:
            reason = check["reason"] if check["accessible"] else f"접근 실패: {check['reason']}"
            print(f"           -> FAIL: {reason} -> 제외", flush=True)
            job["_fail_reason"] = reason
            failed.append(job)
    return verified, failed


# ══════════════════════════════════════════════
# 14  동아리·학회 검증
# ══════════════════════════════════════════════
def _club_school_ok(club: dict, user_school: str) -> bool:
    ctype = club.get("type", "").lower()
    if "외부" in ctype:
        return True
    if not user_school:
        return "연합" in ctype
    affil = normalize_school_name(club.get("school_affiliation", ""))
    return affil == normalize_school_name(user_school)


def _request_broader_clubs(school: str, department: str, exclude_names: list) -> list:
    exclude_str = ", ".join(exclude_names) if exclude_names else "없음"
    dept_hint = f" (전공: {department})" if department else ""
    prompt = (
        f"학교: {school}{dept_hint}\n"
        f"이미 시도했지만 검증 실패한 항목: {exclude_str}\n\n"
        f"'{school}' 학생에게 추천할 수 있는 동아리·학회를 다시 찾아주세요.\n"
        "조건:\n"
        f"- '{school}'에 실재하거나 '{school}' 지부가 확인된 연합동아리\n"
        "- 또는 전국 단위 외부학회/협회\n"
        "- 교내·연합·외부 타입 균형 있게 최소 3개\n"
        "- 실재가 확실하지 않으면 포함 금지\n\n"
        "순수 JSON 배열만 출력:\n"
        '[\n  {\n'
        '    "name": "동아리명",\n'
        '    "type": "교내동아리/교내학회/연합동아리/연합학회/외부학회",\n'
        f'    "school_affiliation": "{school}",\n'
        '    "description": "",\n'
        '    "reason": "추천 이유",\n'
        '    "expected_effect": "기대 효과",\n'
        '    "url": null,\n'
        '    "search_query": "검증용 검색어",\n'
        '    "search_verified": false\n'
        '  }\n]'
    )
    system = (
        "당신은 한국 대학교 동아리 및 학회 정보 전문가입니다.\n"
        "Google Search를 활용해 실재하는 동아리·학회만 추천하십시오."
    )
    raw = _call_model_raw(prompt, system_prompt=system, use_google_search=True)
    try:
        return [r for r in json.loads(clean_json_response(raw)) if isinstance(r, dict)]
    except Exception:
        return []


def verify_clubs_with_search(clubs: list, school: str, department: str = "") -> list:
    if not clubs:
        if school:
            print(f"\n  [동아리] 초기 추천 없음 → 재추천 요청", flush=True)
            clubs = _request_broader_clubs(school, department, [])
        if not clubs:
            return []

    print(f"\n  [동아리 검증 1단계: 학교 매칭 - {len(clubs)}건]", flush=True)
    stage1 = [c for c in clubs if _club_school_ok(c, school)]
    for c in clubs:
        print(f"    [{c.get('name','?')}] {'OK' if c in stage1 else '학교 불일치 -> 제외'}", flush=True)

    if not stage1:
        if school:
            print(f"  [동아리] 1단계 전원 탈락 → 재추천 요청", flush=True)
            tried = [c.get("name", "") for c in clubs]
            stage1 = _request_broader_clubs(school, department, tried)
            stage1 = [c for c in stage1 if _club_school_ok(c, school)]
        if not stage1:
            return []

    print(f"\n  [동아리 검증 2단계: Google Search - {len(stage1)}건]", flush=True)
    system = (
        "당신은 한국 대학교 동아리 및 학회 정보 전문가입니다.\n"
        "Google Search를 활용해 동아리/학회를 검색·검증하십시오."
    )
    raw = _call_model_raw(
        build_prompt_verify_clubs(school, stage1),
        system_prompt=system,
        use_google_search=True,
    )

    result = []
    tried_names = [c.get("name", "") for c in stage1]
    try:
        vmap = {r.get("name", ""): r for r in json.loads(clean_json_response(raw)) if isinstance(r, dict)}
        for club in stage1:
            name = club.get("name", "")
            vr = vmap.get(name, {})
            if vr.get("wrong_school"):
                print(f"    [{name}] -> FAIL: 다른 학교 소속 -> 제외", flush=True)
                continue
            if not vr.get("verified"):
                print(f"    [{name}] -> FAIL: 검색 미확인 -> 제외", flush=True)
                continue
            actual = normalize_school_name(vr.get("actual_school", ""))
            ctype = club.get("type", "").lower()
            if actual and school and "외부" not in ctype and actual != normalize_school_name(school):
                print(f"    [{name}] -> FAIL: 교차 확인 실패 -> 제외", flush=True)
                continue

            print(f"    [{name}] -> OK", flush=True)
            club["search_verified"] = True
            official_url = vr.get("official_url")
            club["url"] = official_url if official_url and official_url != "null" else None
            club["description"] = vr.get("description", "").strip()
            if vr.get("evidence"):
                club["verification_evidence"] = vr["evidence"]
            result.append(club)
    except Exception as e:
        print(f"  WARNING: 동아리 검증 파싱 실패 ({e}) -> 전체 제외", flush=True)

    min_clubs = 3 if school else 2
    if len(result) < min_clubs and school:
        shortage = min_clubs - len(result)
        print(f"\n  [동아리] 검증 통과 {len(result)}건 (목표 {min_clubs}건) → {shortage}건 보충 재요청", flush=True)
        existing_names = tried_names + [c.get("name", "") for c in result]
        extra_raw = _request_broader_clubs(school, department, existing_names)
        extra_stage = [c for c in extra_raw if _club_school_ok(c, school)]

        if extra_stage:
            raw2 = _call_model_raw(
                build_prompt_verify_clubs(school, extra_stage),
                system_prompt=system,
                use_google_search=True,
            )
            try:
                vmap2 = {r.get("name", ""): r for r in json.loads(clean_json_response(raw2)) if isinstance(r, dict)}
                for club in extra_stage:
                    if len(result) >= min_clubs:
                        break
                    name = club.get("name", "")
                    vr = vmap2.get(name, {})
                    if vr.get("wrong_school") or not vr.get("verified"):
                        print(f"    [{name}] -> FAIL (보충)", flush=True)
                        continue
                    print(f"    [{name}] -> OK (보충)", flush=True)
                    club["search_verified"] = True
                    official_url = vr.get("official_url")
                    club["url"] = official_url if official_url and official_url != "null" else None
                    club["description"] = vr.get("description", "").strip()
                    result.append(club)
            except Exception as e:
                print(f"  WARNING: 보충 검증 파싱 실패 ({e})", flush=True)

    return result


# ══════════════════════════════════════════════
# 15  공모전·프로젝트 검증
# ══════════════════════════════════════════════
def verify_contests_with_search(contests: list, ref_date: date) -> list:
    if not contests:
        return []

    print(f"\n  [공모전 검증: Google Search - {len(contests)}건]", flush=True)
    system = (
        "당신은 한국 공모전·대회 정보 전문가입니다.\n"
        "Google Search로 각 공모전을 실제 검색하여 존재 여부와 현황을 검증하십시오.\n"
        "확인되지 않는 공모전은 verified: false로 표시하십시오."
    )
    raw = _call_model_raw(
        build_prompt_verify_contests(contests, ref_date),
        system_prompt=system,
        use_google_search=True,
    )

    verified_list = []
    try:
        parsed = json.loads(clean_json_response(raw))
        vmap = {r.get("name", ""): r for r in parsed if isinstance(r, dict)}

        for contest in contests:
            name = contest.get("name", "")
            vr = vmap.get(name, {})
            label = f"{contest.get('organizer','?')} | {name}"

            if not vr.get("verified"):
                print(f"    [{label}] -> FAIL: 실재 미확인 -> 제외", flush=True)
                continue
            if not vr.get("organizer_confirmed"):
                print(f"    [{label}] -> FAIL: 주관기관 불일치 -> 제외", flush=True)
                continue
            if not vr.get("deadline_ok", True):
                print(f"    [{label}] -> FAIL: 마감 지남 -> 제외", flush=True)
                continue

            print(f"    [{label}] -> OK", flush=True)
            official_url = vr.get("official_url")
            contest["url"] = official_url if official_url and official_url != "null" else None
            deadline = vr.get("deadline")
            contest["deadline"] = deadline if deadline and deadline != "null" else None
            contest["search_verified"] = True
            verified_list.append(contest)

    except Exception as e:
        print(f"  WARNING: 공모전 검증 파싱 실패 ({e}) -> 전체 제외", flush=True)

    return verified_list


# ══════════════════════════════════════════════
# 16  자격증 검증
# ══════════════════════════════════════════════
def verify_certifications_with_search(certs: list) -> list:
    if not certs:
        return []

    print(f"\n  [자격증 검증: Google Search - {len(certs)}건]", flush=True)
    system = (
        "당신은 한국 자격증 정보 전문가입니다.\n"
        "Google Search로 각 자격증의 실제 시행 여부를 검증하십시오."
    )
    raw = _call_model_raw(
        build_prompt_verify_certifications(certs),
        system_prompt=system,
        use_google_search=True,
    )

    verified_list = []
    try:
        parsed = json.loads(clean_json_response(raw))
        vmap = {r.get("name", ""): r for r in parsed if isinstance(r, dict)}

        for cert in certs:
            name = cert.get("name", "")
            vr = vmap.get(name, {})

            if not vr.get("verified"):
                print(f"    [{name}] -> FAIL: 실재 미확인 -> 제외", flush=True)
                continue

            print(f"    [{name}] -> OK ({vr.get('issuer', '주관기관 미확인')})", flush=True)
            official_url = vr.get("official_url")
            cert["url"] = official_url if official_url and official_url != "null" else None
            cert["issuer"] = vr.get("issuer", "")
            verified_list.append(cert)

    except Exception as e:
        print(f"  WARNING: 자격증 검증 파싱 실패 ({e}) -> 전체 제외", flush=True)

    return verified_list


# ══════════════════════════════════════════════
# 17  채용공고 AI 검증
# ══════════════════════════════════════════════
def verify_jobs_with_search(jobs: list, ref_date: date) -> list:
    if not jobs:
        return []

    jobs_with_url = [j for j in jobs if j.get("url") and j.get("url") != "null"]
    dropped = len(jobs) - len(jobs_with_url)
    if dropped:
        print(f"    URL 미확보 {dropped}건 사전 제외", flush=True)
    if not jobs_with_url:
        return []

    print(f"\n  [채용공고 Google Search 검증 - {len(jobs_with_url)}건]", flush=True)
    system = (
        "당신은 한국 채용 시장 전문가입니다.\n"
        "Google Search로 각 채용공고를 실제 검색·검증하십시오."
    )
    raw = _call_model_raw(
        build_prompt_verify_jobs(jobs_with_url, ref_date),
        system_prompt=system,
        use_google_search=True,
    )

    try:
        vmap = {}
        for r in json.loads(clean_json_response(raw)):
            if isinstance(r, dict):
                vmap[f"{r.get('company','')}|{r.get('role','')}"] = r

        verified = []
        for job in jobs_with_url:
            key = f"{job.get('company','')}|{job.get('role','')}"
            vr = vmap.get(key, {})
            label = f"{job.get('company','?')} | {job.get('role','?')}"
            if not vr.get("verified"):
                print(f"    [{label}] -> FAIL: 공고 미확인", flush=True)
                continue
            if not vr.get("deadline_confirmed"):
                print(f"    [{label}] -> FAIL: 마감일 미확인", flush=True)
                continue
            correct_url = vr.get("correct_url")
            if correct_url and correct_url != "null" and correct_url != job.get("url"):
                job["url"] = correct_url
                print(f"    [{label}] -> OK (URL 교정)", flush=True)
            else:
                print(f"    [{label}] -> OK", flush=True)
            verified.append(job)
        return verified
    except Exception as e:
        print(f"  WARNING: 채용공고 검증 실패 ({e})", flush=True)
        return []


# ══════════════════════════════════════════════
# 18  날짜 필터
# ══════════════════════════════════════════════
def filter_valid_jobs(jobs: list, ref_date: date) -> tuple[list, list]:
    valid, expired = [], []
    for job in jobs:
        dl = job.get("deadline", "")
        if not dl or dl in ("상시채용", "없음", "미정", "-", "null", None):
            job["is_valid"] = True
            valid.append(job)
            continue
        try:
            d = datetime.strptime(dl, "%Y-%m-%d").date()
            (valid if d >= ref_date else expired).append(job)
        except ValueError:
            valid.append(job)
    return valid, expired


# ══════════════════════════════════════════════
# 19  핵심 분석 함수 (v2.0 — strength_diagnosis 지시 포함)
# ══════════════════════════════════════════════
def analyze_career_comprehensive(user_text: str, ref_date: date,
                                  school: str, department: str = "") -> dict:
    system_prompt = build_system_prompt_comprehensive(ref_date, school, department)
    rd = ref_date.strftime("%Y-%m-%d")

    profile_parts = []
    if school:
        profile_parts.append(f"학교: {school}")
        profile_parts.append(f"동아리·학회는 반드시 '{school}' 소속만 추천하십시오.")
    if department:
        profile_parts.append(f"학과: {department}")
        profile_parts.append(f"'{department}' 전공에 맞는 추천을 우선하십시오.")

    profile_ctx = ("\n[사용자 프로필]\n" + "\n".join(profile_parts) + "\n") if profile_parts else ""

    source_hint = ""
    if user_text.startswith("[SOURCE_URL:"):
        first_line_end = user_text.find("\n")
        source_url_line = user_text[:first_line_end] if first_line_end > 0 else user_text[:100]
        source_hint = (
            f"\n[입력 소스 안내]\n"
            f"사용자가 URL을 제출했습니다: {source_url_line}\n"
            f"아래 데이터는 해당 URL에서 크롤링한 결과입니다.\n"
            f"크롤링 제한으로 일부 정보가 누락될 수 있으며, 이 경우 URL 경로명(슬러그)을 "
            f"추가 맥락으로 활용하십시오. 단, 추론 불가 항목은 반드시 빈 배열로 반환하십시오.\n"
        )

    user_prompt = (
        f"아래 사용자 데이터를 분석하십시오.\n"
        f"오늘 날짜: {rd} — 모든 기준은 이 날짜 기준.\n"
        f"채용공고: {rd} 이후 마감 + URL 확인된 것만.\n"
        f"공모전: {rd} 기준 현재 접수 중이거나 {rd} 이후 개최 예정인 것만."
        f" 주관기관이 명확하게 확인된 것만 포함. 불확실하거나 이미 종료된 것은 빈 배열.\n"
        f"자격증: 현재 실제 시행 중인 것만. 불확실하면 빈 배열.\n"
        f"동아리·학회: 최소 3개 이상 추천. 교내·연합·외부 균형 있게.\n"
        f"critical_diagnosis: 입력 데이터에서 실제 확인된 사실에만 근거하여 냉정하게 작성."
        f" 존재하지 않는 약점을 만들어내지 말고, 반대로 명백한 약점을 완화하거나 숨기지도 말 것.\n"
        # ── [v2.0 추가] strength_diagnosis 전용 지시 ──────────────
        f"strength_diagnosis: 입력 데이터에서 실제 확인된 강점만 기재할 것."
        f" 없는 강점을 만들어내거나 과장하는 것은 절대 금지."
        f" 강점이 없으면 strengths 배열을 [] 로 반환하고,"
        f" no_strength_diagnosis 에 이유(reason)와 개선 방향(improvement_direction)을"
        f" 솔직하고 구체적으로 기재할 것.\n"
        f"{source_hint}"
        f"{profile_ctx}\n"
        f"[사용자 데이터]\n{user_text}"
    )
    return _call_model(system_prompt, user_prompt, use_google_search=False)


# ══════════════════════════════════════════════
# 20  강점 진단 stderr 로그 헬퍼 (v2.0 신규)
# ══════════════════════════════════════════════
def _log_strength_summary(result: dict) -> None:
    """
    strength_diagnosis 요약을 stderr 에 출력합니다.
    JSON stdout 과 혼합되지 않도록 stderr 사용.
    critical_diagnosis 로그와 동일한 포맷 적용.
    """
    import sys
    sd = result.get("strength_diagnosis", {})
    if not sd:
        print("  [강점 진단] strength_diagnosis 키 없음", file=sys.stderr)
        return

    verdict = sd.get("one_line_verdict", "")
    if verdict:
        print(f"  [강점 진단] {verdict}", file=sys.stderr)

    strengths = sd.get("strengths", [])
    if strengths:
        outstanding = sum(1 for s in strengths if s.get("level") == "outstanding")
        strong      = sum(1 for s in strengths if s.get("level") == "strong")
        notable     = sum(1 for s in strengths if s.get("level") == "notable")
        print(
            f"  [강점 진단] 강점 — outstanding:{outstanding}  strong:{strong}  notable:{notable}",
            file=sys.stderr,
        )
    else:
        no_str    = sd.get("no_strength_diagnosis", {})
        reason    = no_str.get("reason", "")
        direction = no_str.get("improvement_direction", "")
        print("  [강점 진단] 식별 가능한 강점 없음", file=sys.stderr)
        if reason:
            print(f"             이유: {reason}", file=sys.stderr)
        if direction:
            print(f"             개선 방향: {direction}", file=sys.stderr)


# ══════════════════════════════════════════════
# 21  Main (v2.0 — 강점 로그 먼저, 냉정 진단 뒤)
# ══════════════════════════════════════════════
def main(user_input: list[str], school: str, department: str):
    import sys
    ref_date = date.today()
    rd = ref_date.strftime("%Y-%m-%d")

    print("=" * 65)
    print("  Career Analysis AI - COMPREHENSIVE Edition v2.0")
    print(f"  모델: {_ANALYSIS_MODEL}  |  임베딩: {_EMBEDDING_MODEL}")
    print(f"  기준일: {rd} (자동)  |  Google Search 검증 활성화")
    print("  strength_diagnosis: 활성화 (강점 → 약점 순서 출력)")
    print("=" * 65)

    school, department = get_user_profile(school, department)
    raw_content = get_user_input(user_input)

    if len(raw_content.strip()) < 10:
        error_result = {
            "status": "error",
            "message": "입력 데이터가 너무 짧습니다. 최소 10자 이상 입력하세요."
        }
        print(json.dumps(error_result, ensure_ascii=False, indent=2))
        return error_result

    print("\n[1] 임베딩 벡터 생성 중...", flush=True)
    vector = get_embedding(raw_content)
    if vector:
        print(f"  OK (dim: {len(vector)})", flush=True)
    else:
        print("  WARNING: embedding skipped (분석은 계속 진행)", flush=True)

    print("\n[2] 종합 커리어 분석 + 강점 진단 + 냉정 보완점 진단 중 (Gemini 2.5 Pro)...", flush=True)
    result = analyze_career_comprehensive(raw_content, ref_date, school, department)

    verified_clubs: list    = []
    verified_contests: list = []
    verified_certs: list    = []

    if result.get("status") not in ("error", "insufficient_data"):
        additional = result.get("additional_recommendations", {})

        # 자격증 검증
        raw_certs = additional.get("certifications", [])
        verified_certs = verify_certifications_with_search(raw_certs)

        # 동아리 검증
        raw_clubs = additional.get("clubs_and_societies", [])
        verified_clubs = verify_clubs_with_search(raw_clubs, school, department)

        # 공모전 검증
        raw_contests = additional.get("projects_and_contests", [])
        verified_contests = verify_contests_with_search(raw_contests, date.today())

        # 검증 결과를 result에 반영
        if "additional_recommendations" in result:
            result["additional_recommendations"]["certifications"]       = verified_certs
            result["additional_recommendations"]["clubs_and_societies"]  = verified_clubs
            result["additional_recommendations"]["projects_and_contests"] = verified_contests

        # 채용공고 검증
        all_jobs = result.get("valid_job_recommendations", [])
        ai_ok = verify_jobs_with_search(all_jobs, ref_date) if all_jobs else []

        if ai_ok:
            print("\n  [채용공고 URL + 내용 검증 중...]", flush=True)
            url_ok, url_fail = verify_job_urls(ai_ok)
            if url_fail:
                print(f"  {len(url_fail)}건 URL 검증 실패 → 제외", flush=True)
        else:
            url_ok = []

        valid_jobs, expired_jobs = filter_valid_jobs(url_ok, ref_date)
        result["verified_jobs"] = valid_jobs
        result["expired_jobs"]  = expired_jobs
        result.pop("valid_job_recommendations", None)
    else:
        result["verified_jobs"] = []
        result["expired_jobs"]  = []

    result["embedding_dim"] = len(vector) if vector else None

    print("\n[3] 분석 완료!\n", flush=True)

    # ── stderr: 강점 진단 요약 먼저 ──────────────────────────────────
    _log_strength_summary(result)

    # ── stderr: 냉정 진단 요약 뒤 ─────────────────────────────────────
    diag = result.get("critical_diagnosis", {})
    if diag:
        verdict = diag.get("one_line_verdict", "")
        if verdict:
            print(f"  [냉정 진단] {verdict}", file=sys.stderr)
        weaknesses = diag.get("weaknesses", [])
        critical_cnt = sum(1 for w in weaknesses if w.get("severity") == "critical")
        major_cnt    = sum(1 for w in weaknesses if w.get("severity") == "major")
        minor_cnt    = sum(1 for w in weaknesses if w.get("severity") == "minor")
        print(
            f"  [냉정 진단] 약점 — critical:{critical_cnt}  major:{major_cnt}  minor:{minor_cnt}",
            file=sys.stderr,
        )

    # ── stdout: 최종 출력을 공통 envelope 형식으로 통일 ──────────────────
    # API Endpoint 계약(§3.3 [1]): analyzer main() 은 envelope 를 반환한다.
    #   { "status": "success", "vector": [...], "result": { ...payload... } }
    # 규약: result(payload) 안에는 status·vector 를 넣지 않는다 (§3.6 #25·#26).
    #       vector 는 별도 필드로만, status 는 envelope 최상위로만 나간다.
    if not isinstance(result, dict) or result.get("status") == "error":
        envelope = {
            "status": "error",
            "message": result.get("message", "분석에 실패했습니다.")
                       if isinstance(result, dict) else "분석에 실패했습니다.",
        }
    else:
        payload = {k: v for k, v in result.items() if k != "status"}
        # ── 분석 이름 기본값 (FRT-119 대응, 부분) ──────────────────────
        # 정식 이름("{첫 경험명} 등 {n}개 분석")은 계약 §4에 따라 서버가
        # POST 시점에 experience_ids 로 확정한다 — analyzer 는 경험 목록을
        # 모르므로 §4.1 폴백 형식("{생성일} 종합 분석")을 코드로(LLM 아님)
        # 결정적으로 주입한다. 서버가 저장한 title 이 있으면 그쪽이 우선이며,
        # 이 값은 title 부재 시 프론트가 빈칸 대신 쓸 수 있는 기본값이다.
        payload.setdefault("analysis_title", f"{rd} 종합 분석")
        envelope = {"status": "success", "result": payload, "vector": vector}

    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    return envelope


if __name__ == "__main__":
    main()
