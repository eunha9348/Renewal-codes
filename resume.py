"""
resume_generator.py
====================
유저 데이터 → Resume JSON 출력 (Colab 완전 독립 실행)

Colab 설치:
  !pip install -q google-genai pypdf requests beautifulsoup4 playwright
  !playwright install chromium

language 파라미터:
  "ko"   → 한국어 Resume (국문 이력서 형식)
  "en"   → 영문 Resume (서구권 CV/Resume 형식)
  "both" → 한국어 + 영어 동시 생성 (파일 2개 출력)

원칙:
  - 유저가 직접 입력한 데이터만 사용
  - Hallucination 절대 금지 (없는 내용 → null / 빈 배열)
  - AI 추천·분석·진단 내용 완전 제외
  - 활동 간 연계는 유저 데이터 내에서만 추출
"""

import io, json, os, re, sys, time, urllib.parse
from collections import deque
from datetime import date

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("WARNING: pip install requests beautifulsoup4")

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False
    print("WARNING: pip install pypdf")

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("WARNING: pip install playwright && playwright install chromium")

from google import genai
from google.genai import types


# ══════════════════════════════════════════════
# ★ 설정 — 여기만 수정
# ══════════════════════════════════════════════
GEMINI_API_KEY = ""   # ← API 키 입력
MODEL          = "gemini-2.5-pro"
# 대안: "gemini-2.0-flash" (빠름) / "gemini-1.5-pro" (안정)

_TIMEOUT        = 20
_MAX_CHARS      = 30_000
_MAX_PAGES      = 20
_JS_WAIT_MS     = 2500
_SPA_THRESHOLD  = 500
_MAX_RETRIES    = 4
_RETRY_BASE_SEC = 5

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

client = genai.Client(api_key=GEMINI_API_KEY)


# ══════════════════════════════════════════════
# 크롤러 (내장)
# ══════════════════════════════════════════════
def _html_to_text(html: str, base_url: str = "") -> tuple[str, list[str]]:
    if not HAS_REQUESTS:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL|re.IGNORECASE)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip(), []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","noscript","meta","link","head","svg"]): tag.decompose()
    links = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h and not h.startswith(("javascript:","mailto:","tel:","#")):
            links.append(urllib.parse.urljoin(base_url, h) if base_url else h)
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n", strip=True)).strip()
    return text, links

def _same_origin(a, b): return urllib.parse.urlparse(a).netloc == urllib.parse.urlparse(b).netloc
def _is_pdf_url(u): return u.lower().split("?")[0].endswith(".pdf")

def _fetch_req(url):
    if not HAS_REQUESTS: return None
    try:
        s = requests.Session(); s.headers.update(_HEADERS)
        r = s.get(url, timeout=_TIMEOUT, allow_redirects=True)
        r.raise_for_status(); r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  [requests] 실패: {e}", flush=True); return None

def _fetch_pw(url):
    if not HAS_PLAYWRIGHT: return None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
            ctx = br.new_context(user_agent=_HEADERS["User-Agent"], viewport={"width":1280,"height":900})
            pg = ctx.new_page()
            pg.goto(url, wait_until="networkidle", timeout=_TIMEOUT*1000)
            pg.wait_for_timeout(_JS_WAIT_MS)
            pg.evaluate("window.scrollTo(0,document.body.scrollHeight)")
            pg.wait_for_timeout(800)
            html = pg.content(); br.close(); return html
    except Exception as e:
        print(f"  [playwright] 실패: {e}", flush=True); return None

def _parse_pdf(data):
    if not HAS_PYPDF: return ""
    try:
        r = pypdf.PdfReader(io.BytesIO(data))
        t = "\n".join(p.extract_text() or "" for p in r.pages)
        print(f"  [PDF] {len(r.pages)}p, {len(t)}자", flush=True); return t
    except Exception as e:
        print(f"  [PDF] 실패: {e}", flush=True); return ""

def _crawl(url, deep=True):
    if not deep: return _single(url)
    print(f"\n  [딥크롤] {url}", flush=True)
    visited, queue, col, cnt = set(), deque([url]), [], 0
    skip = (".jpg",".jpeg",".png",".gif",".svg",".ico",".css",".js",".woff",".woff2")
    while queue and cnt < _MAX_PAGES:
        cur = queue.popleft(); norm = cur.split("#")[0].rstrip("/")
        if norm in visited: continue
        visited.add(norm)
        if _is_pdf_url(cur):
            if HAS_REQUESTS:
                try:
                    t = _parse_pdf(requests.get(cur, headers=_HEADERS, timeout=_TIMEOUT).content)
                    if t: col.append(f"[PDF: {cur}]\n{t}")
                except: pass
            continue
        html = _fetch_req(cur); text, links = "", []
        if html:
            text, links = _html_to_text(html, cur)
            if len(text.strip()) < _SPA_THRESHOLD:
                print(f"    SPA → Playwright: {cur}", flush=True)
                pw = _fetch_pw(cur)
                if pw: text, links = _html_to_text(pw, cur)
        else:
            pw = _fetch_pw(cur)
            if pw: text, links = _html_to_text(pw, cur)
        if text.strip():
            cnt += 1; col.append(f"[Page {cnt}: {cur}]\n{text}")
            print(f"    p{cnt}: {len(text)}자 — {cur}", flush=True)
        for lnk in links:
            n = lnk.split("#")[0].rstrip("/")
            if n and n not in visited and _same_origin(url, lnk) and not any(n.endswith(e) for e in skip):
                queue.append(lnk)
    res = "\n\n".join(col)
    print(f"  [딥크롤 완료] {cnt}p, {len(res)}자", flush=True)
    return res[:_MAX_CHARS]

def _single(url):
    print(f"  [크롤] {url}", flush=True)
    if _is_pdf_url(url):
        try: return _parse_pdf(requests.get(url, headers=_HEADERS, timeout=_TIMEOUT).content)
        except: return ""
    html = _fetch_req(url)
    if html:
        text, _ = _html_to_text(html, url)
        if len(text.strip()) >= _SPA_THRESHOLD:
            print(f"  [req] OK {len(text)}자", flush=True); return text[:_MAX_CHARS]
        print("  SPA → Playwright", flush=True)
    pw = _fetch_pw(url)
    if pw:
        text, _ = _html_to_text(pw, url)
        print(f"  [pw] OK {len(text)}자", flush=True); return text[:_MAX_CHARS]
    if html:
        text, _ = _html_to_text(html, url); return text[:_MAX_CHARS]
    return ""

def _read_file(path):
    if not os.path.isfile(path): print(f"  [파일없음] {path}"); return ""
    if path.lower().endswith(".pdf"):
        with open(path,"rb") as f: return _parse_pdf(f.read())
    with open(path,"r",encoding="utf-8",errors="replace") as f: t = f.read()
    print(f"  [파일] {len(t)}자: {path}"); return t

def resolve(source, deep=True):
    s = source.strip()
    if re.match(r"^https?://", s): return _crawl(s, deep)
    if os.path.isfile(s): return _read_file(s)
    print(f"  [텍스트] {len(s)}자"); return s


# ══════════════════════════════════════════════
# Gemini 호출
# ══════════════════════════════════════════════
def _is_rl(e): return any(k in str(e).lower() for k in ("429","quota","rate_limit","resource_exhausted"))

def _retry(fn):
    for i in range(_MAX_RETRIES):
        try: return fn()
        except Exception as e:
            if _is_rl(e) and i < _MAX_RETRIES-1:
                w = _RETRY_BASE_SEC*(2**i); print(f"  [RL] {w}s 재시도...", flush=True); time.sleep(w)
            else: raise

def _call(sys_p, usr_p):
    return _retry(lambda: client.models.generate_content(
        model=MODEL, contents=usr_p,
        config=types.GenerateContentConfig(system_instruction=sys_p, temperature=0.05)
    )).text.strip()

def _clean(raw):
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL|re.IGNORECASE)
    if m: return m.group(1).strip()
    d, s = 0, -1
    for i, c in enumerate(raw):
        if c=="{":
            if d==0: s=i
            d+=1
        elif c=="}":
            d-=1
            if d==0 and s!=-1: return raw[s:i+1].strip()
    return raw


# ══════════════════════════════════════════════
# Resume 스키마 — 한국어 (국문 이력서)
# ══════════════════════════════════════════════
_SYS_KO = """당신은 유저 원본 데이터에서 한국어 이력서(국문 Resume) JSON을 추출하는 파서입니다.

[절대 원칙]
1. 유저가 직접 작성·입력한 내용만 추출합니다. 단 하나의 단어도 추가·수정·강화하지 않습니다.
2. 없는 정보는 반드시 null 또는 빈 배열입니다. 추론·가정·상상 금지.
3. AI 분석·추천·진단 내용은 입력에 포함되어도 완전 무시합니다.
4. connections는 유저가 직접 기술한 내용 안에서만 도출합니다. 없으면 빈 배열.
5. 출력은 순수 JSON만. 마크다운·설명 텍스트 금지.

[한국 국문 이력서 형식 기준]
- 인적사항: 이름, 생년월일(있으면), 연락처, 이메일, 주소(있으면), 사진여부(있으면)
- 학력: 최신순, 입학~졸업(예정), 학교명, 학과, 학점(있으면)
- 경력: 최신순, 회사명, 부서, 직위, 재직기간, 담당업무
- 자격증/어학: 취득일, 발급기관, 점수(있으면)
- 대외활동/프로젝트: 활동명, 기관, 기간, 역할, 성과
- 수상: 수상명, 수여기관, 날짜
- 자기소개서 요약: 유저가 직접 쓴 내용이 있을 때만

[Resume JSON 스키마 — 한국어]
{
  "meta": {
    "language": "ko",
    "format": "korean_resume",
    "generated_at": "YYYY-MM-DD",
    "source_chars": 0
  },
  "인적사항": {
    "이름": null,
    "영문명": null,
    "생년월일": null,
    "이메일": null,
    "전화번호": null,
    "주소": null,
    "링크": []
  },
  "학력": [
    {
      "id": 1,
      "학교명": null,
      "학과": null,
      "전공구분": "주전공|복수전공|부전공|연계전공",
      "학위": "학사|석사|박사|수료",
      "입학년월": null,
      "졸업년월": null,
      "졸업구분": "졸업|재학중|졸업예정|수료|중퇴",
      "학점": null,
      "만점": null,
      "비고": null
    }
  ],
  "경력": [
    {
      "id": 1,
      "회사명": null,
      "부서": null,
      "직위": null,
      "고용형태": "정규직|계약직|인턴|파트타임|프리랜서",
      "입사년월": null,
      "퇴사년월": null,
      "재직중": false,
      "담당업무": [],
      "성과": []
    }
  ],
  "자격증": [
    {
      "id": 1,
      "자격증명": null,
      "발급기관": null,
      "취득년월": null,
      "자격구분": "국가자격|민간자격|어학|기타"
    }
  ],
  "어학": [
    {
      "id": 1,
      "언어": null,
      "시험명": null,
      "점수등급": null,
      "취득년월": null
    }
  ],
  "대외활동": [
    {
      "id": 1,
      "활동명": null,
      "기관": null,
      "기간_시작": null,
      "기간_종료": null,
      "기간_원문": null,
      "진행중": false,
      "역할": null,
      "활동내용": [],
      "성과": []
    }
  ],
  "프로젝트": [
    {
      "id": 1,
      "프로젝트명": null,
      "소속기관": null,
      "기간_시작": null,
      "기간_종료": null,
      "기간_원문": null,
      "역할": null,
      "사용기술": [],
      "내용": [],
      "성과": []
    }
  ],
  "수상": [
    {
      "id": 1,
      "수상명": null,
      "수여기관": null,
      "수상년월": null,
      "내용": null
    }
  ],
  "기술및역량": {
    "기술스택": [],
    "툴": [],
    "소프트스킬": []
  },
  "동아리_학회": [
    {
      "id": 1,
      "단체명": null,
      "구분": "교내동아리|교내학회|연합동아리|외부학회|기타",
      "기간_원문": null,
      "역할": null,
      "활동내용": []
    }
  ],
  "연계성": [
    {
      "항목ids": [1, 2],
      "연결점": "유저 데이터 안에서 확인되는 연결점만 (원문 근거 필수)"
    }
  ],
  "자기소개_요약": null,
  "파싱경고": []
}"""


# ══════════════════════════════════════════════
# Resume 스키마 — 영어 (서구권 CV/Resume)
# ══════════════════════════════════════════════
_SYS_EN = """You are a parser that extracts English Resume / CV JSON from user-provided raw data.

[Absolute Rules]
1. Extract ONLY what the user directly wrote. Do not add, modify, or embellish a single word.
2. Missing information must be null or empty array. No inference, assumption, or fabrication.
3. Ignore any AI analysis, recommendations, or diagnostic content in the input.
4. connections: only from explicit cross-references in user data. Empty array if none.
5. Output pure JSON only. No markdown code blocks, no explanatory text.

[Standard Western Resume / CV Format]
- Contact Header: name, email, phone, location (city/country), LinkedIn, GitHub, portfolio
- Summary/Objective: only if user wrote one directly
- Education: reverse chronological, institution, degree, major, GPA (if stated), dates
- Work Experience: reverse chronological, company, title, dates, bullet-point responsibilities & achievements
- Projects: name, tech stack, description, outcomes (only what user stated)
- Skills: technical skills, tools, languages — categorized
- Certifications & Licenses: name, issuer, date
- Awards & Honors: name, issuer, date
- Activities & Leadership: org, role, dates, description
- Publications / Research: only if user mentioned

[Resume JSON Schema — English]
{
  "meta": {
    "language": "en",
    "format": "western_resume",
    "generated_at": "YYYY-MM-DD",
    "source_chars": 0
  },
  "contact": {
    "name": null,
    "name_ko": null,
    "email": null,
    "phone": null,
    "location": null,
    "linkedin": null,
    "github": null,
    "portfolio": null,
    "other_links": []
  },
  "summary": null,
  "education": [
    {
      "id": 1,
      "institution": null,
      "degree": "Bachelor|Master|PhD|Associate|Diploma|Certificate|Other",
      "field_of_study": null,
      "minor": null,
      "start_date": null,
      "end_date": null,
      "status": "Graduated|In Progress|Expected|Withdrew",
      "gpa": null,
      "gpa_scale": null,
      "honors": null,
      "relevant_coursework": [],
      "notes": null
    }
  ],
  "work_experience": [
    {
      "id": 1,
      "company": null,
      "title": null,
      "employment_type": "Full-time|Part-time|Internship|Contract|Freelance",
      "start_date": null,
      "end_date": null,
      "is_current": false,
      "location": null,
      "responsibilities": [],
      "achievements": []
    }
  ],
  "projects": [
    {
      "id": 1,
      "name": null,
      "organization": null,
      "start_date": null,
      "end_date": null,
      "role": null,
      "tech_stack": [],
      "description": [],
      "outcomes": []
    }
  ],
  "skills": {
    "technical": [],
    "tools": [],
    "languages": [],
    "soft_skills": []
  },
  "certifications": [
    {
      "id": 1,
      "name": null,
      "issuer": null,
      "date": null,
      "type": "National|Professional|Language|Other"
    }
  ],
  "awards": [
    {
      "id": 1,
      "title": null,
      "issuer": null,
      "date": null,
      "description": null
    }
  ],
  "activities": [
    {
      "id": 1,
      "organization": null,
      "type": "Club|Society|Volunteer|Competition|Leadership|Other",
      "role": null,
      "start_date": null,
      "end_date": null,
      "date_raw": null,
      "is_ongoing": false,
      "description": [],
      "achievements": []
    }
  ],
  "publications": [
    {
      "id": 1,
      "title": null,
      "venue": null,
      "date": null,
      "description": null
    }
  ],
  "connections": [
    {
      "item_ids": [1, 2],
      "note": "Cross-reference found within user data only (cite evidence)"
    }
  ],
  "parse_warnings": []
}"""


# ══════════════════════════════════════════════
# 프롬프트 빌더
# ══════════════════════════════════════════════
def _build_prompt(raw_content, personal, lang):
    today = date.today().strftime("%Y-%m-%d")
    ko = lang == "ko"

    fields_ko = [
        ("이름(한국어)", personal.get("name_ko","")),
        ("이름(영문)",   personal.get("name_en","")),
        ("이메일",       personal.get("email","")),
        ("전화번호",     personal.get("phone","")),
        ("학교",         personal.get("school","")),
        ("학과",         personal.get("department","")),
        ("링크",         personal.get("links","")),
    ]
    fields_en = [
        ("Name (Korean)", personal.get("name_ko","")),
        ("Name (English)",personal.get("name_en","")),
        ("Email",         personal.get("email","")),
        ("Phone",         personal.get("phone","")),
        ("School",        personal.get("school","")),
        ("Department",    personal.get("department","")),
        ("Links",         personal.get("links","")),
    ]
    fields = fields_ko if ko else fields_en
    hint_lines = [f"{k}: {v}" for k, v in fields if v]

    if ko:
        hint_block = (
            "\n[별도 제공된 인적사항 — 인적사항 필드에 그대로 사용]\n"
            + "\n".join(hint_lines) + "\n"
        ) if hint_lines else ""
        return (
            f"아래 유저 원본 데이터에서 한국어 이력서 JSON을 추출하세요.\n"
            f"오늘 날짜: {today}\n"
            f"{hint_block}\n"
            f"AI 분석·추천 항목이 포함된 경우 무조건 무시하고, "
            f"유저가 직접 작성한 내용만 추출합니다.\n\n"
            f"[유저 원본 데이터]\n{raw_content}"
        )
    else:
        hint_block = (
            "\n[Separately provided personal info — use as-is in contact/education fields]\n"
            + "\n".join(hint_lines) + "\n"
        ) if hint_lines else ""
        return (
            f"Extract English Resume JSON from the user's raw data below.\n"
            f"Today: {today}\n"
            f"{hint_block}\n"
            f"If AI analysis or recommendations appear in the input, ignore them entirely.\n"
            f"Extract only what the user directly wrote.\n\n"
            f"[User Raw Data]\n{raw_content}"
        )


# ══════════════════════════════════════════════
# 핵심 생성 함수
# ══════════════════════════════════════════════
def generate(raw_content, personal, lang="ko"):
    """lang: 'ko' | 'en'"""
    sys_p = _SYS_KO if lang == "ko" else _SYS_EN
    prompt = _build_prompt(raw_content, personal, lang)

    print(f"  Gemini 호출 중 (language={lang})...", flush=True)
    raw = _call(sys_p, prompt)
    result = json.loads(_clean(raw))

    today = date.today().strftime("%Y-%m-%d")
    result.setdefault("meta", {})
    result["meta"].update({
        "language": lang,
        "generated_at": today,
        "source_chars": len(raw_content),
    })
    return result


# ══════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════
def main(
    sources: list[str],
    name_ko: str = "",
    name_en: str = "",
    email: str = "",
    phone: str = "",
    school: str = "",
    department: str = "",
    links: str = "",
    language: str = "both",
    output_path: str = "resume.json",
    deep_crawl: bool = True,
):
    """
    Args:
        sources:     URL / 파일 경로 / 텍스트 목록 (여러 개 조합 가능)
        name_ko:     이름 (한국어)
        name_en:     이름 (영문)
        email:       이메일
        phone:       전화번호
        school:      학교
        department:  학과
        links:       GitHub / LinkedIn / 포트폴리오 등
        language:    "ko" | "en" | "both"
        output_path: 결과 저장 경로 (both면 _ko.json / _en.json 자동 분리)
        deep_crawl:  True = 같은 도메인 링크까지 순회
    """
    assert language in ("ko", "en", "both"), "language는 'ko', 'en', 'both' 중 하나"

    print("=" * 55)
    print("  Resume JSON Generator")
    print(f"  모델: {MODEL}  |  언어: {language}")
    print("=" * 55)

    # 데이터 수집
    parts = [p for s in sources if (p := resolve(s, deep=deep_crawl))]
    if not parts:
        # API Endpoint 계약(§2.4·§3.3): 입력이 없어도 sys.exit 로 죽지 않는다.
        # sys.exit(1) 은 콜백조차 못 보내 result=null 로 영구 방치되는 원인이었다.
        # 대신 실패 envelope 를 반환해 tasks.py 가 /internal/resume/failure 로
        # status=failed 를 기록할 수 있게 한다.
        print("ERROR: 입력 데이터 없음", flush=True)
        return {"status": "error", "message": "입력 데이터가 없습니다."}

    raw_content = "\n\n---\n\n".join(parts)
    print(f"\n총 입력: {len(raw_content)}자\n", flush=True)

    personal = dict(name_ko=name_ko, name_en=name_en, email=email,
                    phone=phone, school=school, department=department, links=links)

    # 언어별 생성
    langs = ["ko", "en"] if language == "both" else [language]
    base, ext = os.path.splitext(output_path)

    results = {}
    for lang in langs:
        result = generate(raw_content, personal, lang)
        results[lang] = result

        # 저장 경로 결정
        if language == "both":
            save_path = f"{base}_{lang}{ext or '.json'}"
        else:
            save_path = output_path if ext else f"{output_path}.json"

        out = json.dumps(result, ensure_ascii=False, indent=2)
        print(f"\n{'='*55}\n[{lang.upper()}] Resume JSON\n{'='*55}")
        print(out)

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"\n저장 완료: {save_path}", flush=True)

    # 공통 envelope 형식으로 통일 (API Endpoint 계약 §3.3 [1]).
    #   - language 가 "ko"/"en"(API 경계) 이면 result = 단일 레쥬메 payload.
    #   - "both" 는 API 경계 밖(§2.4)이라 CLI 편의용으로만 유지: {ko, en} 묶음.
    #   - resume payload 에는 status/vector 가 없으므로 그대로 담는다 (#25·#26).
    final_result = results if language == "both" else results[language]
    return {"status": "success", "result": final_result}


# ══════════════════════════════════════════════
# 실행 예시
# ══════════════════════════════════════════════
if __name__ == "__main__":
    main(
        sources=[
            "https://jimin-byun.com/index.html",
            # "/path/to/career.txt",
            # "직접 입력 텍스트...",
        ],
        name_ko="변지민",
        name_en="Jimin Byun",
        email="",
        phone="",
        school="",
        department="",
        links="https://jimin-byun.com",
        language="both",        # "ko" | "en" | "both"
        output_path="resume.json",
        deep_crawl=True,
    )
