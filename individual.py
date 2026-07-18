"""
Career Analysis AI - INDIVIDUAL Edition v1.0
=============================================
단일 경력/자격증/활동 심층 분석 전용 모듈

기능:
  - 하나의 자격증, 인턴 경험, 프로젝트, 대외활동 등 단일 항목 심층 분석
  - STAR 방식 이력서 초안 자동 생성
  - 해당 항목과 시너지가 높은 자격증·교육·프로젝트·활동 추천
  - 단기·중기·장기 액션 플랜 제시
  - 모든 출력은 순수 JSON

Hallucination 방지 원칙:
  - 실재 확인 불가 추천 항목 생성 금지
  - URL 추측/조합 금지 → null만 허용
  - 데이터 부족 시 status: insufficient_data 반환 (임의 내용 채우기 금지)
  - 추천 자격증·교육은 현재 실제 시행 중인 것만

사용법:
  python career_individual.py
  → URL, 파일, 또는 텍스트 붙여넣기 (단일 항목)
  → JSON 결과 출력 (stdout)
"""

import json
import re
import os
import io
import time
import urllib.request
import urllib.parse
import urllib.error
import sys
from html.parser import HTMLParser
from datetime import date
from collections import deque

from google import genai
from google.genai import types
import pypdf
from analysis_response import VectorSuccessResponse, ErrorResponse

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
GEMINI_API_KEY   = ""
client           = genai.Client(api_key=GEMINI_API_KEY)

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
# 3  HTML 파서
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
# 4  Notion 전용 크롤러
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
# 5  딥 크롤러 (일반 웹사이트)
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
# 6  파일 리더
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
# 7  입력 수집
# ══════════════════════════════════════════════
def _looks_like_url(s: str) -> bool:
    return bool(re.match(r"^https?://\S+", s) or re.match(r"^www\.\S+", s))


def _looks_like_filepath(s: str) -> bool:
    return "\n" not in s and os.path.isfile(s.strip())


def get_user_input(lines: list[str]) -> str:
    print("\n" + "-" * 65)
    print("  단일 경력/활동/자격증 정보를 입력하세요.")
    print("  URL, 파일 경로, 또는 직접 텍스트 모두 가능합니다.")
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
# 8  Embedding
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
# 9  Hallucination 방지 규칙 (공통)
# ══════════════════════════════════════════════
_STRICT_HALLUCINATION_RULES = """
=== STRICT MODE: Hallucination 절대 금지 ===
규칙 1. 실재 확인 불가 추천 항목 생성 금지
  - 자격증·교육: 현재 실제 시행 중인 것만. 이름·주관기관이 불확실하면 제외.
  - 프로젝트·대외활동: 실재가 확인된 것만 추천.
  - 추천 항목 이름을 임의로 만들거나 변형하지 말 것.

규칙 2. URL 생성 금지
  - URL은 기억 속에 확실한 공식 URL만 허용. 추측·조합·변형 절대 금지.
  - 불확실하면 반드시 null (빈 문자열 "" 금지).

규칙 3. 빈 배열 우선 원칙
  - 추천할 항목이 없거나 실재 확인 불가이면 [] 반환.
  - 채우기 위해 임의로 만들어내는 것은 빈 배열보다 훨씬 나쁘다.
  - search_verified 필드가 false인 항목은 출력에서 제외할 것.

규칙 4. 데이터 부족 시 insufficient_data 반환
  - 입력 항목이 너무 짧거나 식별 불가 시, status를 "insufficient_data"로 설정.
  - 절대로 빈 값을 채우거나 임의로 내용을 생성하지 말 것.

규칙 5. 연도·기간 날조 금지
  - 취득 연도, 기간, 점수 등을 추측해서 채우지 말 것.
  - 모르면 null 또는 빈 문자열.

규칙 6. 출력은 순수 JSON만
  - 마크다운 코드블록, 설명 텍스트, 주석 절대 포함 금지.

규칙 7. STAR 분석은 필수가 아님.
  - STAR 분석 시 분석 할 데이터가 부족하면, 절대 임의로 이야기를 지어내지말 것.
  - 만약 STAR 분석을 하기에 데이터가 부족하다면, 데이터가 부족하니 STAR 분석이 이루어지지 않았음을 텍스트로 띄우기.
  - STAR 분석을 생성해서 결과물을 보이는 것보다, 냉철한 판단 기준을 기반으로 데이터가 부족하면, 부족하기에 결과값을 띄울 수 없으며 예시로 어떻게 기록을 하면 좋을지 Comment 달기.
"""


# ══════════════════════════════════════════════
# 10  시스템 프롬프트
# ══════════════════════════════════════════════
def build_system_prompt_individual() -> str:
    """
    단일 항목 심층 분석용 시스템 프롬프트.
    분석 항목:
      A. 항목 메타 (이름, 타입, 한 줄 요약)
      B. 심층 분석 (커리어 가치, 강점, 한계, 어필 직무, 시장 수요)
      C. STAR 이력서 초안
      D. 시너지 추천 (연계 자격증·교육·프로젝트·활동, 우선순위 포함)
      E. 단기·중기·장기 액션 플랜
    """
    return (
        "당신은 대한민국 최고의 전문 커리어 컨설턴트입니다.\n"
        "사용자가 제공한 단일 경력/자격증/활동 하나를 심층 분석하는 것이 임무입니다.\n\n"
        "[분석 원칙]\n"
        "1. 단일 항목에 집중 — 여러 항목이 보이더라도 가장 대표적인 하나를 선정\n"
        "2. 근거 중심 서술 — 입력 데이터에서 실제 확인된 내용만 서술\n"
        "3. 실질적 가치 발굴 — 표면적 사실 너머의 커리어 가치를 찾아낼 것\n"
        "4. 시너지 발굴 — 이 항목과 조합 시 가장 효과적인 추천 제시\n"
        "5. 데이터 부족 시 status를 'insufficient_data'로 설정하고 분석 중단\n"
        "6. 냉정한 진단 — 사용자가 듣기 싫더라도 진짜 약점을 직시하게 할 것. 하지만, 그래도 조금은 따뜻한 언어로 설명할 것.\n"
        f"{_STRICT_HALLUCINATION_RULES}\n"
        "=== 냉정한 보완점 진단(item_diagnosis) 작성 지침 ===\n"
        "목적: 이 단일 항목이 이력서/커리어에서 실제로 얼마나 강한지 냉정하게 평가.\n"
        "절대 금지: 칭찬·위로·긍정적 포장 — item_diagnosis 섹션에는 좋은 말 하지 말 것.\n"
        "판단 기준 (해당하는 항목만 포함):\n"
        "  [서술_완성도] 기간/수치/역할/성과 중 빠진 것이 있는가?\n"
        "    → 구체적으로 무엇이 빠졌는지 열거\n"
        "  [차별성_부족] 동일 스펙 보유자가 많아 희소성이 없는가?\n"
        "    → 시장 내 포화 정도 설명\n"
        "  [직무_연결_약함] 이 항목이 어필하려는 직무와 연결고리가 약한가?\n"
        "    → 왜 연결이 약한지, 무엇이 보강되어야 하는지\n"
        "  [성과_불명확] 숫자·결과·임팩트가 없어 설득력이 떨어지는가?\n"
        "    → 어떤 수치/성과를 추가해야 하는지 구체적으로 제시\n"
        "  [기간_문제] 너무 짧거나 너무 오래되어 신선도가 떨어지는가?\n"
        "    → 기간·시점 문제를 명시\n"
        "  [단독_활용_한계] 이 항목 하나만으로는 지원 시 경쟁력이 불충분한가?\n"
        "    → 반드시 함께 갖춰야 할 것을 명시\n"
        "severity 기준:\n"
        "  critical: 이 상태로 이력서에 넣으면 오히려 역효과 가능\n"
        "  major   : 설득력을 크게 낮추는 문제\n"
        "  minor   : 있으면 좋지만 없어도 당장 치명적이지 않은 부족함\n"
        "improvement_example: 실제로 어떻게 바꿔 써야 하는지 예시 문장 제시 (가능하면 Before/After 형식)\n\n"
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
        '  "deep_analysis": {\n'
        '    "career_value": "이 항목이 커리어에서 갖는 실질적 가치와 의미",\n'
        '    "market_value": "현재 한국 취업시장에서 이 항목의 수요·희소성·경쟁력 평가"\n'
        '  },\n'
        '    "applicable_roles": [\n'
        '      "이 항목으로 어필 가능한 직무/포지션 1",\n'
        '      "직무 2"\n'
        '    ],\n'
        '  "item_strengths": {\n'
        '    "has_genuine_strengths": true,\n'
        '    "one_line_strength_verdict": "이 항목의 핵심 강점 한 문장 — 강점 없으면 null",\n'
        '    "no_strength_reason": null,\n'
        '    "summarized_strengths": [\n'
        '      "이 항목이 갖는 구체적 강점 1",\n'
        '      "강점 2"\n'
        '    ],\n'
        '    "strengths": [\n'
        '      {\n'
        '        "id": 1,\n'
        '        "category": "전문성_희소성|성과_입증|역량_다양성|경험_깊이|도메인_전문성|차별화_포인트",\n'
        '        "strength_level": "outstanding|notable|moderate",\n'
        '        "title": "강점 제목 (10자 이내)",\n'
        '        "analysis": "왜 강점인지 냉정하고 구체적인 근거 — 입력 데이터 기반",\n'
        '        "evidence": "입력 텍스트에서 이 강점을 직접 확인할 수 있는 구체적 내용",\n'
        '        "career_impact": "이 강점이 취업·커리어에서 발휘하는 실질적 영향",\n'
        '        "leverage_action": "이 강점을 극대화하기 위해 지금 해야 할 한 가지 행동 (동사 시작)",\n'
        '        "showcase_example": "Before: 현재 서술 → After: 강점이 잘 드러나는 개선된 서술 (없으면 null)"\n'
        '      }\n'
        '    ],\n'
        '    "strongest_asset": "가장 강력한 단일 강점 한 줄 요약 — 강점 없으면 null",\n'
        '    "positioning_tip": "면접·이력서에서 가장 효과적으로 포지셔닝하는 전략 — 강점 없으면 null"\n'
        '  },\n'
        '  "item_diagnosis": {\n'
        '    "one_line_verdict": "이 항목의 현재 상태를 냉정하게 한 문장으로",\n'
        '    "limitations": [\n'
        '      "이 항목만으로는 부족한 점 또는 보완이 필요한 점 1",\n'
        '      "한계 2"\n'
        '    ],\n'
        '    "weaknesses": [\n'
        '      {\n'
        '        "id": 1,\n'
        '        "category": "서술_완성도|차별성_부족|직무_연결_약함|성과_불명확|기간_문제|단독_활용_한계",\n'
        '        "severity": "critical|major|minor",\n'
        '        "title": "약점 제목 (10자 이내)",\n'
        '        "diagnosis": "왜 약점인지 냉정하고 구체적인 근거",\n'
        '        "evidence": "입력에서 이 약점을 판단한 구체적 근거",\n'
        '        "impact": "이 약점이 취업/커리어에 미치는 실질적 영향",\n'
        '        "priority_action": "지금 당장 해야 할 한 가지 구체적 행동 (동사로 시작)",\n'
        '        "improvement_example": "Before: 현재 서술 → After: 개선된 서술 예시 (해당 없으면 null)"\n'
        '      }\n'
        '    ],\n'
        '    "missing_elements": [\n'
        '      "이 항목 서술에서 반드시 추가해야 할 누락 요소 (예: 수치, 기간, 팀 규모 등)"\n'
        '    ],\n'
        '    "rewrite_suggestion": "이 항목 전체를 이력서에 가장 효과적으로 쓰는 방법 — 구체적 표현 전략 제시"\n'
        '  },\n'
        '  "synergy_recommendations": [\n'
        '    {\n'
        '      "priority": 1,\n'
        '      "category": "자격증|교육강의|프로젝트|대외활동|경험",\n'
        '      "name": "추천 항목명 (실재하는 것만, 불확실하면 제외)",\n'
        '      "reason": "이 항목과 조합했을 때 시너지가 나는 구체적 이유",\n'
        '      "expected_effect": "조합 후 기대되는 커리어 효과",\n'
        '      "estimated_duration": "취득/이수에 필요한 예상 기간 (모르면 null)"\n'
        '    }\n'
        '  ],\n'
        '  "action_plan": {\n'
        '    "단기": "3개월 이내 — 지금 당장 해야 할 구체적 행동",\n'
        '    "중기": "6개월~1년 — 이 시기에 달성해야 할 목표",\n'
        '    "장기": "1년 이상 — 이 항목을 기반으로 향후 커리어 방향"\n'
        '  },\n'
        '  "missing_info_warning": null\n'
        "}"
    )


# ══════════════════════════════════════════════
# 11  핵심 분석 함수
# ══════════════════════════════════════════════
def analyze_career_individual(item_text: str) -> dict:
    """강점 + 약점 통합 심층 분석. URL 크롤링 입력 처리 포함."""
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
        "━━ [강점 분석 필수 지침] ━━\n"
        "item_strengths: 입력 데이터에서 실제 확인된 사실에만 근거하여 강점을 구체적으로 작성.\n"
        "강점이 없거나 데이터가 빈약한 경우:\n"
        "  → has_genuine_strengths: false / strengths: []\n"
        "  → no_strength_reason 명시 / 나머지 필드: null\n"
        "evidence: 반드시 입력 텍스트에서 직접 인용 가능한 내용만 기재.\n"
        "showcase_example: Before/After 형식으로 구체적인 문장 예시 제시.\n"
        "strength_level 인플레이션 금지 — outstanding은 근거가 확실할 때만 부여.\n\n"
        "━━ [약점 분석 필수 지침] ━━\n"
        "item_diagnosis: 입력 데이터에서 실제 확인된 사실에만 근거하여 냉정하게 작성.\n"
        "존재하지 않는 약점을 만들어내지 말고, 명백한 약점을 완화하거나 숨기지도 말 것.\n"
        "improvement_example: Before/After 형식으로 구체적인 문장 예시 제시.\n"
        f"{source_hint}\n"
        f"[분석 대상]\n{item_text}"
    )
    return _call_model(
        system_prompt=build_system_prompt_individual(),
        user_prompt=user_prompt,
        use_google_search=False,
    )



# ══════════════════════════════════════════════
# 12  입력 유효성 검사
# ══════════════════════════════════════════════
def _is_likely_single_item(text: str) -> bool:
    """
    입력이 단일 항목처럼 보이는지 간단히 판별.
    SOURCE_URL 입력은 크롤링 결과이므로 단일 항목으로 간주하지 않는다.
    실제 분류는 LLM이 수행하며, 이 함수는 경고 출력용.
    """
    if text.startswith("[SOURCE_URL:"):
        return True  # URL 입력은 개별 분석 허용
    # 줄 수가 너무 많으면 복합 입력일 가능성 높음 (경고만, 강제 중단 안 함)
    return True  # 판별 책임은 LLM에 위임


# ══════════════════════════════════════════════
# 13  Main
# ══════════════════════════════════════════════
# 반환 모델은 analysis_response.py 에서 import (성공/실패 분리):
#   성공 → VectorSuccessResponse(result, vector)  /  실패 → ErrorResponse(message)
def main(user_input):
    ref_date = date.today()
    rd = ref_date.strftime("%Y-%m-%d")

    print("=" * 65)
    print("  Career Analysis AI - INDIVIDUAL Edition v1.0")
    print(f"  모델: {_ANALYSIS_MODEL}  |  임베딩: {_EMBEDDING_MODEL}")
    print(f"  기준일: {rd} (자동)")
    print("  [단일 경력/자격증/활동 심층 분석 전용]")
    print("=" * 65)

    raw_content = get_user_input(user_input)

    if len(raw_content.strip()) < 5:
        resp = ErrorResponse(
            message="입력 데이터가 너무 짧습니다. 분석할 경력/자격증/활동을 입력해주세요.",
        )
        print(resp.model_dump_json(indent=2, exclude_none=True))
        return resp

    print("\n[1] 임베딩 벡터 생성 중...", flush=True)
    vector = get_embedding(raw_content)
    if vector:
        print(f"  OK (dim: {len(vector)})", flush=True)
    else:
        print("  WARNING: embedding skipped (분석은 계속 진행)", flush=True)

    print("\n[2] 단일 항목 심층 분석 + 냉정 보완점 진단 중 (Gemini 2.5 Pro)...", flush=True)
    result = analyze_career_individual(raw_content)

    # embedding 벡터는 JSON 출력에서 제외 (가독성)
    result["embedding_dim"] = len(vector) if vector else None
    result["analysis_date"] = rd

    print("\n[3] 분석 완료!\n", flush=True)

    # 강점 로그
    sd = result.get("item_strengths", {})
    if sd:
        if sd.get("has_genuine_strengths"):
            sl = sd.get("strengths", [])
            o = sum(1 for s in sl if s.get("strength_level") == "outstanding")
            n = sum(1 for s in sl if s.get("strength_level") == "notable")
            m = sum(1 for s in sl if s.get("strength_level") == "moderate")


    # ── 냉정 진단 요약 로그 (stderr로 출력해 JSON stdout과 분리) ──
    diag = result.get("item_diagnosis", {})
    if diag:
        verdict = diag.get("one_line_verdict", "")
        weaknesses = diag.get("weaknesses", [])
        critical_cnt = sum(1 for w in weaknesses if w.get("severity") == "critical")
        major_cnt    = sum(1 for w in weaknesses if w.get("severity") == "major")
        minor_cnt    = sum(1 for w in weaknesses if w.get("severity") == "minor")


    # ── 최종 출력: 공통 envelope 형식으로 통일 ──
    # API Endpoint 계약(§3.3 [1]): analyzer main() 은 envelope 를 반환한다.
    #   { "status": "success", "vector": [...], "result": { ...payload... } }
    # 규약: result(payload) 안에는 status·vector 를 넣지 않는다 (§3.6 #25·#26).
    #       status 는 envelope 최상위로만, vector 는 별도 필드로만 나간다.
    if not isinstance(result, dict) or result.get("status") == "error":
        resp = ErrorResponse(
            message=(result.get("message", "분석에 실패했습니다.")
                     if isinstance(result, dict) else "분석에 실패했습니다."),
        )
    else:
        payload = {k: v for k, v in result.items() if k != "status"}
        resp = VectorSuccessResponse(result=payload, vector=vector)

    print(resp.model_dump_json(indent=2, exclude_none=True))
    return resp


if __name__ == "__main__":
    main()
