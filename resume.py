"""
resume_generator.py  (2.0 Ver. — 직무/템플릿/경험선택 지원)
====================
유저 데이터 → Resume JSON 출력 (Colab 완전 독립 실행)

★ v1 대비 추가된 Input 파라미터는 딱 3개 (나머지 형식·함수 구조는 v1과 동일):
  - target_role         : 지원 직무 (예: "백엔드 개발자"). 빈 값이면 직무 필터 없음.
  - template            : 이력서 템플릿/양식 키. "auto"면 target_role로 자동 결정.
  - selected_experiences: 유저가 "선택"한 경험(제목/키워드 목록). 선택 경험은 반드시
                          포함하고 요약·분류. 나머지 경험은 LLM이 직무 관련성에 따라
                          요약·분류·포함 여부를 판단. (선택 경험도 요약·분류는 LLM 담당)

★ rev.2 버그픽스 (선택 검증 강화 + 파싱경고 최소화):
  - 선택 경험 검증(_verify_selected)이 meta를 제외한 "경험 섹션"만 검사하도록 수정
    (meta.selected_experiences와의 자기매칭으로 검증이 무력화되던 버그 해결)
  - 각 경험 항목에 "선택매칭"(en: selected_match) 에코 필드 추가 — 매칭된 유저 선택
    문자열을 번역 없이 그대로 복사 → 영문(번역) 출력에서도 검증이 동작
  - 정규화 6자 미만의 짧은 선택어는 본문 부분일치 매칭을 신뢰하지 않음 (우연 매칭 오탐 방지)
  - 파싱경고는 "심각한 파싱 실패"만 기록하도록 프롬프트에 기준 명시
    (선택적 필드 부재는 null 처리만 하고 경고 금지 → 경고 남발 차단)

Colab 설치:
  !pip install -q google-genai pypdf requests beautifulsoup4 playwright
  !playwright install chromium

language 파라미터:
  "ko"   → 한국어 Resume (국문 이력서 형식)
  "en"   → 영문 Resume (서구권 CV/Resume 형식)
  "both" → 한국어 + 영어 동시 생성 (파일 2개 출력)

원칙 (v1 계승, "요약" 관련만 완화):
  - 유저가 직접 입력한 데이터만 사용
  - Hallucination 절대 금지 (없는 사실·수치·기관·기간·성과 창작 금지 / 없는 내용 → null·빈 배열)
  - 단, "요약"은 허용: 원문 내용의 압축·재구성은 OK, 새 사실 추가는 금지
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
# 템플릿 — 직무별 섹션 순서 (출력 JSON 키 순서에 반영)
#   template="auto" 이면 target_role 키워드로 자동 선택.
# ══════════════════════════════════════════════
#   ※ finance/business/general 순서는 실제 컨설팅·금융 트랙 레쥬메 4종 분석 반영
#     (EDUCATION → WORK/EXPERIENCE → EXTRACURRICULAR/ACTIVITIES → 기술 → 어학 → OTHER INFO).
#   ※ 하단 3종 고정 순서: 기술및역량 → 어학 → 기타정보 (어학은 기술및역량 바로 아래, 기타정보 위).
#   ※ "기타정보"(병역·관심사, 어학 제외)는 모든 템플릿에서 최하단.
_TPL_KO = {
    "software":  ["자기소개_요약","프로젝트","경력","학력","수상","자격증","대외활동","동아리_학회","기술및역량","어학","기타정보"],
    "data_ai":   ["자기소개_요약","프로젝트","경력","학력","수상","자격증","대외활동","동아리_학회","기술및역량","어학","기타정보"],
    "design":    ["자기소개_요약","프로젝트","경력","학력","수상","대외활동","자격증","동아리_학회","기술및역량","어학","기타정보"],
    "marketing": ["자기소개_요약","경력","프로젝트","대외활동","학력","수상","자격증","동아리_학회","기술및역량","어학","기타정보"],
    "business":  ["자기소개_요약","학력","경력","프로젝트","대외활동","동아리_학회","수상","자격증","기술및역량","어학","기타정보"],
    "finance":   ["자기소개_요약","학력","경력","대외활동","동아리_학회","수상","자격증","프로젝트","기술및역량","어학","기타정보"],
    "research":  ["자기소개_요약","학력","프로젝트","경력","수상","자격증","대외활동","동아리_학회","기술및역량","어학","기타정보"],
    "general":   ["자기소개_요약","학력","경력","프로젝트","대외활동","동아리_학회","수상","자격증","기술및역량","어학","기타정보"],
}
_TPL_EN = {
    "software":  ["summary","projects","work_experience","education","awards","certifications","activities","publications","skills","languages","additional_info"],
    "data_ai":   ["summary","projects","work_experience","education","publications","awards","certifications","activities","skills","languages","additional_info"],
    "design":    ["summary","projects","work_experience","education","awards","activities","certifications","publications","skills","languages","additional_info"],
    "marketing": ["summary","work_experience","projects","activities","education","awards","certifications","publications","skills","languages","additional_info"],
    "business":  ["summary","education","work_experience","projects","activities","awards","certifications","publications","skills","languages","additional_info"],
    "finance":   ["summary","education","work_experience","activities","awards","certifications","projects","publications","skills","languages","additional_info"],
    "research":  ["summary","education","projects","work_experience","publications","awards","certifications","activities","skills","languages","additional_info"],
    "general":   ["summary","education","work_experience","projects","activities","awards","certifications","publications","skills","languages","additional_info"],
}
_TPL_KEYS = list(_TPL_KO.keys())
_ROLE_KEYWORDS = {
    "software":  ["개발","개발자","developer","engineer","backend","frontend","백엔드","프론트","풀스택","fullstack","devops","서버","웹","android","ios","앱","sw","임베디드"],
    "data_ai":   ["데이터","data","ai","ml","머신러닝","딥러닝","분석가","analyst","scientist","사이언티스트","인공지능","빅데이터","llm"],
    "design":    ["디자인","design","designer","ux","ui","브랜딩","그래픽","graphic","visual","bx"],
    "marketing": ["마케팅","marketing","마케터","marketer","그로스","growth","브랜드","콘텐츠","퍼포먼스","performance","crm","광고","sns"],
    "business":  ["기획","pm","po","product manager","product owner","프로덕트","전략","strategy","경영","business","운영","operation","매니저"],
    "finance":   ["금융","finance","투자","investment","은행","증권","회계","accounting","컨설팅","consulting","컨설턴트","재무","ib"],
    "research":  ["연구","research","researcher","대학원","석사","박사","phd","교수","논문","학술","r&d"],
}

def _resolve_template(template, target_role):
    """template이 'auto'면 target_role 키워드로 결정. 매칭 없으면 'general'."""
    if template and template != "auto":
        return template if template in _TPL_KEYS else "general"
    t = (target_role or "").lower()
    best, hits = "general", 0
    for key, kws in _ROLE_KEYWORDS.items():
        h = sum(1 for kw in kws if kw in t)
        if h > hits: best, hits = key, h
    return best


# ══════════════════════════════════════════════
# 크롤러 (내장) — v1 동일
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
# Gemini 호출 — v1 동일
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
#   v1 스키마 + 각 경험 항목에 "요약"·"직무관련성"·"선택포함" 필드 추가
# ══════════════════════════════════════════════
_SYS_KO = """당신은 유저 원본 데이터에서 한국어 이력서(국문 Resume) JSON을 생성하는 엔진입니다.

[절대 원칙 — Hallucination 금지]
1. 유저가 직접 작성·입력한 사실만 사용합니다. 원문에 없는 사실·수치·기관명·기간·성과·기술을
   지어내지 마세요. 확인 불가한 정보는 null 또는 빈 배열입니다.
2. 단, "요약"은 허용됩니다: 원문에 있는 내용을 이력서용 문장으로 압축·재구성하는 것은 OK.
   요약 과정에서 새로운 사실을 추가하는 것은 금지(압축만 허용).
3. AI 분석·추천·진단 내용은 입력에 포함되어도 완전 무시합니다.
4. 연계성은 유저가 직접 기술한 내용 안에서만 도출합니다. 없으면 빈 배열.
5. 출력은 순수 JSON만. 마크다운·설명 텍스트 금지.

[요약·분류·선택 규칙]
- 각 경험(경력/프로젝트/대외활동/동아리)마다 "요약" 필드에 1~2문장 이력서용 요약을 넣습니다.
  (원문 압축·재구성. 없는 성과·수치 창작 금지. 요약할 근거가 부족하면 null.)
- 각 경험을 "직무관련성"으로 분류합니다: 지원 직무 기준 "CORE"|"RELATED"|"WEAK"|"없음".
  (직무가 주어지지 않았으면 모두 "없음".)
- 유저가 "선택한 경험 목록"에 해당하는 경험은 "선택포함"=true로 두고 반드시 결과에 포함하며
  요약·분류를 수행합니다. 선택 경험은 관련성이 낮아도 절대 제외하지 않습니다.
- 매칭된 항목의 "선택매칭" 필드에는 매칭된 유저 선택 문자열을 번역·수정 없이 한 글자도
  바꾸지 말고 그대로 복사합니다. 선택 목록과 무관한 항목의 "선택매칭"은 null.
- 선택되지 않은 경험은 지원 직무 관련성에 따라 포함 여부를 판단합니다. 무관하거나 억지로
  엮어야 하는 경험(예: 개발자 지원인데 순수 취미 연주 경력)은 과감히 제외할 수 있습니다.
  단, 억지 연결을 발명하지 마세요.

[파싱경고 최소화 규칙]
- "파싱경고"에는 심각한 문제만 기록합니다:
  (1) 원문이 중간에 잘려 경험을 온전히 읽을 수 없는 경우
  (2) 원문 내 정보가 서로 모순되는 경우
  (3) 유저가 선택한 경험을 원문에서 찾지 못한 경우
- 다음은 절대 기록하지 않습니다: 선택적 필드 부재(생년월일·전화번호·학점·기간 등은 그냥
  null 처리), 정보가 적다는 일반적 언급, AI 분석 텍스트를 무시했다는 보고.
- 기록할 것이 없으면 반드시 빈 배열 [] 입니다.

[실전 이력서 형식 규칙 — 컨설팅·금융·전문직 레쥬메 표준 반영]
- 엔트리 표기 순서: (기관명) → (직함/역할 + 프로젝트 범위) → 성과 불렛. 최신순 정렬.
- "역할범위": 직함 뒤에 붙는 담당 프로젝트/범위 한 줄(예: "신용카드사 Value-up 프로젝트",
  "3개 프로젝트 PM"). 원문에 그런 범위 서술이 있을 때만, 없으면 null. 창작 금지.
- "성과불렛": 각 경험의 성과를 2단 불렛 구조로 정리합니다. 각 원소:
    {"내용": 성과 한 문장, "세부": [방법론·근거 세부 불렛 ...]}
  · "내용"은 [동사로 시작] + [무엇을] + [어떻게] + [정량 성과]의 형태로 압축.
    (예: "여성 사용자 거래 전략 6개월 프로젝트 수행, 월 2,000건·1.5억원 매출 증대 달성")
  · 정량 수치(%, 원, 건수, 인원 등)는 반드시 원문에 있는 값만 사용. 수치 창작·과장 절대 금지.
    원문에 수치가 없으면 정량 표현 없이 사실만. 있는 수치를 지어내 채우지 마세요.
  · "세부"에는 원문에 그 성과의 방법·과정 설명이 있을 때만 하위 불렛으로 넣습니다. 없으면 [].
  · 기존 "담당업무/성과/내용/활동내용"은 원자료 보존용으로 그대로 두되, 화면 렌더의 주 표시원은
    "성과불렛"입니다. 성과불렛은 그 원자료를 압축·재구성한 것이어야 하며 새 사실 추가 금지.

[학력 병합 규칙 — 동일 기관·동일 학위는 하나로]
- 같은 학교 + 같은 학위(학사/석사/박사/수료)에 전공이 여러 개(주전공+복수전공, 주전공+부전공,
  연계전공 등)면 학력 항목을 여러 개로 쪼개지 말고 반드시 하나의 항목으로 합칩니다.
  각 전공은 "전공목록" 배열에 {"학과": 전공명, "전공구분": 구분} 형태로 하나씩 추가합니다.
  (틀린 예: "고려대학교 A전공" 항목과 "고려대학교 B전공" 항목을 따로 만드는 것 — 학교명이
   중복 표시되므로 금지. 올바른 예: "고려대학교" 항목 하나에 전공목록=[A(주전공), B(복수전공)])
- 학위 레벨이 다르면(예: 학사 vs 석사) 같은 학교라도 별도 학력 항목으로 유지합니다.
- 편입·교환학생 등 원문상 명백히 별개의 재학 이력이면 별도 항목을 유지합니다.

[표기 원칙 — 이모티콘 금지]
- 모든 텍스트 필드(요약·성과불렛·자기소개_요약 등)에 이모지·이모티콘을 절대 사용하지 않습니다.
  전문 이력서 관례에 맞는 텍스트만 출력합니다. 원문에 이모지가 있어도 요약·인용 시 제거합니다.

[어학 항목화 규칙]
- 언어 관련 정보는 모두 "어학" 섹션에만 넣습니다. "기타정보"에는 언어 정보를 넣지 마세요.
  · 시험 점수(TOEIC/TOEFL/OPIc 등)가 있으면 시험명·점수등급·취득년월에 기입.
  · 시험 없이 능통도만 있으면(예: "영어 능통", "한국어 원어민") "능통도"에 기입하고 시험 필드는 null.
  · 능통도·시험이 함께 있으면 둘 다 채웁니다. 언어당 하나의 항목.

[한국 국문 이력서 형식 기준]
- 인적사항 / 학력(최신순) / 경력(최신순) / 자격증·어학 / 대외활동·프로젝트 / 수상 / 기타정보 / 자기소개 요약

[Resume JSON 스키마 — 한국어]
{
  "meta": {"language": "ko", "format": "korean_resume", "generated_at": "YYYY-MM-DD", "source_chars": 0},
  "인적사항": {"이름": null, "영문명": null, "생년월일": null, "이메일": null, "전화번호": null, "주소": null, "가용기간": null, "링크": []},
  "학력": [{"id": 1, "학교명": null,
           "전공목록": [{"학과": null, "전공구분": "주전공|복수전공|부전공|연계전공"}],
           "학위": "학사|석사|박사|수료", "입학년월": null, "졸업년월": null,
           "졸업구분": "졸업|재학중|졸업예정|수료|중퇴", "학점": null, "만점": null,
           "우등장학": [], "주요과목": [], "논문주제": null, "비고": null}],
  "경력": [{"id": 1, "회사명": null, "부서": null, "직위": null, "역할범위": null,
           "고용형태": "정규직|계약직|인턴|파트타임|프리랜서", "입사년월": null, "퇴사년월": null,
           "재직중": false, "담당업무": [], "성과": [],
           "성과불렛": [{"내용": null, "세부": []}],
           "요약": null, "직무관련성": "CORE|RELATED|WEAK|없음", "선택포함": false, "선택매칭": null}],
  "자격증": [{"id": 1, "자격증명": null, "발급기관": null, "취득년월": null, "자격구분": "국가자격|민간자격|어학|기타"}],
  "대외활동": [{"id": 1, "활동명": null, "기관": null, "역할": null, "역할범위": null,
             "기간_시작": null, "기간_종료": null, "기간_원문": null, "진행중": false,
             "활동내용": [], "성과": [], "성과불렛": [{"내용": null, "세부": []}],
             "요약": null, "직무관련성": "CORE|RELATED|WEAK|없음", "선택포함": false, "선택매칭": null}],
  "프로젝트": [{"id": 1, "프로젝트명": null, "소속기관": null, "역할": null, "역할범위": null,
             "기간_시작": null, "기간_종료": null, "기간_원문": null,
             "사용기술": [], "내용": [], "성과": [], "성과불렛": [{"내용": null, "세부": []}],
             "요약": null, "직무관련성": "CORE|RELATED|WEAK|없음", "선택포함": false, "선택매칭": null}],
  "수상": [{"id": 1, "수상명": null, "수여기관": null, "수상년월": null, "내용": null}],
  "어학": [{"id": 1, "언어": null, "능통도": null, "시험명": null, "점수등급": null, "취득년월": null}],
  "기술및역량": {"기술스택": [], "툴": [], "소프트스킬": []},
  "동아리_학회": [{"id": 1, "단체명": null, "구분": "교내동아리|교내학회|연합동아리|외부학회|기타",
               "기간_원문": null, "역할": null, "역할범위": null, "활동내용": [],
               "성과불렛": [{"내용": null, "세부": []}],
               "요약": null, "직무관련성": "CORE|RELATED|WEAK|없음", "선택포함": false, "선택매칭": null}],
  "기타정보": {"병역": null, "관심사": []},
  "연계성": [{"항목ids": [1, 2], "연결점": "유저 데이터 안에서 확인되는 연결점만 (원문 근거 필수)"}],
  "자기소개_요약": null,
  "파싱경고": []
}"""


# ══════════════════════════════════════════════
# Resume 스키마 — 영어 (서구권 CV/Resume)
# ══════════════════════════════════════════════
_SYS_EN = """You are an engine that generates English Resume / CV JSON from user-provided raw data.

[Absolute Rules — No Hallucination]
1. Use ONLY facts the user actually wrote. Never invent facts, numbers, employers, dates,
   outcomes, or skills that are not in the source. Unverifiable info must be null / empty array.
2. "Summarization" IS allowed: compressing/rephrasing existing source content into resume
   sentences is fine. Do NOT add new facts while summarizing (compression only).
3. Ignore any AI analysis, recommendations, or diagnostic content in the input.
4. connections: only from explicit cross-references in user data. Empty array if none.
5. Output pure JSON only. No markdown, no explanatory text.

[Summarize / Classify / Select rules]
- For each experience (work_experience/projects/activities), put a 1-2 sentence resume
  summary in "summary" (compress the source; never fabricate outcomes; null if insufficient).
- Classify each with "role_relevance": "CORE"|"RELATED"|"WEAK"|"NONE" against the target role
  (all "NONE" if no role given).
- Any experience matching the user's "selected experiences list" MUST be included with
  "selected"=true, and summarized/classified. Never drop a selected experience even if weakly related.
- In "selected_match", copy the matched user-selection string EXACTLY as given, character for
  character, in its ORIGINAL language — never translate or edit it. null for unmatched items.
- For non-selected experiences, decide inclusion by relevance to the target role. Experiences
  with no reasonable connection (e.g., a pure hobby for a developer role) may be dropped —
  but never invent a forced connection.

[parse_warnings minimization rules]
- Record ONLY severe problems in "parse_warnings":
  (1) source text is truncated mid-content so an experience cannot be fully read
  (2) contradictory information within the source
  (3) a user-selected experience could not be found in the source
- NEVER record: absence of optional fields (birthdate, phone, GPA, dates — just use null),
  generic remarks that information is sparse, or notes that AI-analysis text was ignored.
- If there is nothing severe to record, output an empty array [].

[Real-resume formatting rules — consulting/finance/professional standard]
- Entry order: (organization) → (title + role_scope) → achievement bullets. Reverse-chronological.
- "role_scope": the project/scope descriptor that follows the title (e.g., "Value-up project
  for a credit card company", "Acting PM for 1 project"). Only if such a scope statement exists
  in the source; else null. Never invent.
- "bullets": structure each experience's achievements as two-level bullets. Each element:
    {"text": one-sentence achievement, "sub": [methodology/how sub-bullets ...]}
  · "text" = [start with a verb] + [what] + [how] + [quantified result], compressed.
    (e.g., "Led 6-month female-user transaction strategy, driving 2,000+ monthly deals / KRW 150M sales")
  · Quantities (%, currency, counts, headcount) MUST come only from the source. Never fabricate
    or inflate numbers. If the source has no number, state the fact without one — do not invent.
  · Put items in "sub" only when the source describes the method/process behind that achievement; else [].
  · Keep "responsibilities/achievements/description" as raw preservation; the render's primary
    source is "bullets", which must be a compression of that raw content — no new facts.

[Education merge rule — same institution + same degree level = ONE entry]
- If a student has multiple majors at the SAME institution and SAME degree level (double major,
  major + minor, interdisciplinary major), do NOT split them into separate education entries.
  Merge into ONE entry, listing each major in the "majors" array as
  {"field_of_study": ..., "type": "Major|Double Major|Minor|Interdisciplinary Major"}.
  (Wrong: two entries "Korea University — Major A" and "Korea University — Major B", which
  duplicates the institution name. Right: one "Korea University" entry with
  majors=[A (Major), B (Double Major)].)
- Keep SEPARATE entries only when the degree level differs (e.g., Bachelor's vs Master's), even
  at the same institution, or when the source clearly describes distinct enrollment periods
  (transfer, exchange program).

[No-emoji rule]
- Never use emoji or emoticons in any text field (summary, bullets, etc). Keep output in plain
  professional resume text. Strip emoji from source content even when quoting/summarizing it.

[Languages as its own section]
- Put ALL language info in the top-level "languages" section only. Do NOT put languages under
  "skills" or "additional_info".
  · If a test score exists (TOEIC/TOEFL/OPIc...), fill test/score/date.
  · If only proficiency is stated (e.g., "Native", "Fluent"), fill "proficiency" and leave test null.
  · One entry per language.

[Resume JSON Schema — English]
{
  "meta": {"language": "en", "format": "western_resume", "generated_at": "YYYY-MM-DD", "source_chars": 0},
  "contact": {"name": null, "name_ko": null, "email": null, "phone": null, "location": null,
              "availability": null, "linkedin": null, "github": null, "portfolio": null, "other_links": []},
  "summary": null,
  "education": [{"id": 1, "institution": null, "degree": "Bachelor|Master|PhD|Associate|Diploma|Certificate|Other",
                "majors": [{"field_of_study": null, "type": "Major|Double Major|Minor|Interdisciplinary Major"}],
                "start_date": null, "end_date": null,
                "status": "Graduated|In Progress|Expected|Withdrew", "gpa": null, "gpa_scale": null,
                "honors": [], "relevant_coursework": [], "thesis": null, "notes": null}],
  "work_experience": [{"id": 1, "company": null, "title": null, "role_scope": null,
                      "employment_type": "Full-time|Part-time|Internship|Contract|Freelance",
                      "start_date": null, "end_date": null, "is_current": false, "location": null,
                      "responsibilities": [], "achievements": [],
                      "bullets": [{"text": null, "sub": []}], "summary": null,
                      "role_relevance": "CORE|RELATED|WEAK|NONE", "selected": false, "selected_match": null}],
  "projects": [{"id": 1, "name": null, "organization": null, "role": null, "role_scope": null,
               "start_date": null, "end_date": null,
               "tech_stack": [], "description": [], "outcomes": [],
               "bullets": [{"text": null, "sub": []}], "summary": null,
               "role_relevance": "CORE|RELATED|WEAK|NONE", "selected": false, "selected_match": null}],
  "certifications": [{"id": 1, "name": null, "issuer": null, "date": null, "type": "National|Professional|Language|Other"}],
  "awards": [{"id": 1, "title": null, "issuer": null, "date": null, "description": null}],
  "activities": [{"id": 1, "organization": null, "type": "Club|Society|Volunteer|Competition|Leadership|Other",
                 "role": null, "role_scope": null, "start_date": null, "end_date": null, "date_raw": null,
                 "is_ongoing": false, "description": [], "achievements": [],
                 "bullets": [{"text": null, "sub": []}], "summary": null,
                 "role_relevance": "CORE|RELATED|WEAK|NONE", "selected": false, "selected_match": null}],
  "publications": [{"id": 1, "title": null, "venue": null, "date": null, "description": null}],
  "languages": [{"id": 1, "language": null, "proficiency": null, "test": null, "score": null, "date": null}],
  "skills": {"technical": [], "tools": [], "soft_skills": []},
  "additional_info": {"military": null, "interests": []},
  "connections": [{"item_ids": [1, 2], "note": "Cross-reference found within user data only (cite evidence)"}],
  "parse_warnings": []
}"""


# ══════════════════════════════════════════════
# 프롬프트 빌더 — v1 + 직무/템플릿/선택경험 주입
# ══════════════════════════════════════════════
def _build_prompt(raw_content, personal, lang, target_role, template, selected_experiences):
    today = date.today().strftime("%Y-%m-%d")
    ko = lang == "ko"

    fields_ko = [("이름(한국어)", personal.get("name_ko","")), ("이름(영문)", personal.get("name_en","")),
                 ("이메일", personal.get("email","")), ("전화번호", personal.get("phone","")),
                 ("학교", personal.get("school","")), ("학과", personal.get("department","")),
                 ("링크", personal.get("links",""))]
    fields_en = [("Name (Korean)", personal.get("name_ko","")), ("Name (English)", personal.get("name_en","")),
                 ("Email", personal.get("email","")), ("Phone", personal.get("phone","")),
                 ("School", personal.get("school","")), ("Department", personal.get("department","")),
                 ("Links", personal.get("links",""))]
    hint_lines = [f"{k}: {v}" for k, v in (fields_ko if ko else fields_en) if v]
    sel = [s for s in (selected_experiences or []) if s and s.strip()]

    if ko:
        hint = ("\n[별도 제공된 인적사항 — 인적사항 필드에 그대로 사용]\n" + "\n".join(hint_lines) + "\n") if hint_lines else ""
        role = f"\n[지원 직무] {target_role}" if target_role and target_role.strip() else "\n[지원 직무] (미지정 — 모든 직무관련성은 '없음')"
        tpl  = f"\n[이력서 템플릿] {template}"
        selb = ("\n[유저가 선택한 경험 — 반드시 포함·요약·분류하고, 매칭된 항목의 '선택매칭'에 아래 문자열을 그대로 복사]\n- "
                + "\n- ".join(sel) + "\n") if sel else \
               "\n[유저가 선택한 경험] (없음 — 전부 직무 관련성 기준으로 LLM이 요약·분류·판단)\n"
        return (f"아래 유저 원본 데이터로 한국어 이력서 JSON을 생성하세요.\n오늘 날짜: {today}"
                f"{role}{tpl}{hint}{selb}\n"
                f"AI 분석·추천 항목은 무시하고, 유저가 직접 작성한 내용만 사용합니다. "
                f"요약은 원문 압축만 허용하며 없는 사실 창작은 금지합니다.\n\n"
                f"[유저 원본 데이터]\n{raw_content}")
    hint = ("\n[Separately provided personal info — use as-is]\n" + "\n".join(hint_lines) + "\n") if hint_lines else ""
    role = f"\n[Target role] {target_role}" if target_role and target_role.strip() else "\n[Target role] (none — set all role_relevance to 'NONE')"
    tpl  = f"\n[Resume template] {template}"
    selb = ("\n[User-selected experiences — MUST include, summarize, classify; copy the matched string below verbatim (no translation) into that item's 'selected_match']\n- "
            + "\n- ".join(sel) + "\n") if sel else \
           "\n[User-selected experiences] (none — LLM summarizes/classifies/decides all by relevance)\n"
    return (f"Generate English Resume JSON from the user's raw data below.\nToday: {today}"
            f"{role}{tpl}{hint}{selb}\n"
            f"Ignore AI analysis/recommendations; use only what the user wrote. "
            f"Summaries may compress source content only; never fabricate.\n\n"
            f"[User Raw Data]\n{raw_content}")


# ══════════════════════════════════════════════
# 후처리 — 템플릿 순서 반영 + 선택경험 포함 검증
# ══════════════════════════════════════════════
def _norm(s): return re.sub(r"\s+", "", (s or "")).lower()

_EXP_SECTIONS = {"ko": ["경력","프로젝트","대외활동","동아리_학회"],
                 "en": ["work_experience","projects","activities"]}
_SEL_MATCH_KEY = {"ko": "선택매칭", "en": "selected_match"}
_MIN_BLOB_MATCH = 6   # 정규화 기준 이 길이 미만의 선택어는 본문 부분일치를 신뢰하지 않음 (오탐 방지)

def _reorder(result, order, lang):
    """meta·인적사항(contact)을 앞에 두고, 템플릿 order대로 섹션을 재배치. 나머지는 뒤에 유지."""
    head = ["meta"] + (["인적사항"] if lang == "ko" else ["contact"])
    tail_last = (["연계성","파싱경고"] if lang == "ko" else ["connections","parse_warnings"])
    seq = head + [k for k in order if k in result] + \
          [k for k in result if k not in head and k not in order and k not in tail_last] + \
          [k for k in tail_last if k in result]
    return {k: result[k] for k in seq if k in result}

def _verify_selected(result, selected_experiences, lang):
    """선택 경험이 실제로 결과에 포함됐는지 검증. 누락 시 파싱경고에 기록.

    검증 방식 (rev.2):
    - meta를 제외한 "경험 섹션"의 항목만 검사 — meta.selected_experiences에 기록된
      선택 목록과 자기매칭되어 검증이 항상 통과하던 버그 방지.
    - 1차: 각 항목의 선택매칭/selected_match 에코 값과 대조.
      LLM이 매칭된 유저 선택 문자열을 원어 그대로 복사하므로 번역된 영문 출력에서도 동작.
    - 2차(보조): 경험 섹션 본문 부분일치 — 정규화 길이 ≥ _MIN_BLOB_MATCH 인 선택어만.
      짧은 선택어가 무관한 텍스트에 우연 매칭되는 오탐 방지.
    """
    sel = [s for s in (selected_experiences or []) if s and s.strip()]
    if not sel: return
    key = "ko" if lang == "ko" else "en"
    mkey = _SEL_MATCH_KEY[key]
    items = [it for sec in _EXP_SECTIONS[key]
             for it in (result.get(sec) or []) if isinstance(it, dict)]
    echoes = [e for it in items if (e := _norm(it.get(mkey))) and len(e) >= 2]
    blob = _norm(json.dumps(
        [{k: v for k, v in it.items() if k != mkey} for it in items], ensure_ascii=False))
    missing = []
    for s in sel:
        ns = _norm(s)
        if any(ns == e or ns in e or e in ns for e in echoes):
            continue
        if len(ns) >= _MIN_BLOB_MATCH and ns[:40] in blob:
            continue
        missing.append(s)
    if missing:
        wkey = "파싱경고" if lang == "ko" else "parse_warnings"
        msg = (f"선택 경험이 출력에 미포함(원문에 없거나 매칭 실패): {missing}" if lang == "ko"
               else f"Selected experiences missing from output (absent in source or unmatched): {missing}")
        result.setdefault(wkey, []).append(msg)
        print(f"  [경고] {msg}", flush=True)


# ══════════════════════════════════════════════
# 후처리 — 1페이지 예산 + 관련도 기반 자동 채움 (결정론적, LLM 미개입)
# ══════════════════════════════════════════════
_PAGE_LINE_BUDGET = 46      # 1페이지 본문 줄 수 추정치 (A4·11pt 기준, 튜닝 가능)
_REL_SCORE = {"CORE": 3, "RELATED": 2, "WEAK": 1, "없음": 0, "NONE": 0, "MANUAL_INCLUDE": 3}
_KEYS = {
    "ko": dict(disp="표시", rank="표시순위", sel="선택포함", rel="직무관련성",
               scope="역할범위", bullets="성과불렛", btext="내용", bsub="세부",
               summary="자기소개_요약", edu="학력",
               fixed=["어학", "자격증", "수상", "기술및역량", "기타정보"]),
    "en": dict(disp="display", rank="display_rank", sel="selected", rel="role_relevance",
               scope="role_scope", bullets="bullets", btext="text", bsub="sub",
               summary="summary", edu="education",
               fixed=["languages", "certifications", "awards", "skills", "additional_info"]),
}

def _item_cost(it, K):
    """항목이 렌더 시 차지하는 대략적인 줄 수 (제목 + 범위 + 불렛 + 세부)."""
    cost = 1
    if it.get(K["scope"]): cost += 1
    for b in (it.get(K["bullets"]) or []):
        if isinstance(b, dict):
            if b.get(K["btext"]): cost += 1
            cost += len(b.get(K["bsub"]) or [])
    return max(cost, 2)

def _rel_score(it, K):
    v = it.get(K["rel"])
    return _REL_SCORE.get(str(v).upper() if v else "", _REL_SCORE.get(v, 0))

def _fixed_overhead(result, K):
    """경험 섹션을 제외한 고정 영역(인적사항·요약·학력·어학·기술 등)의 추정 줄 수."""
    n = 3  # 인적사항 헤더
    if result.get(K["summary"]): n += 4
    edu = result.get(K["edu"]) or []
    n += 2 * len(edu) + (1 if edu else 0)
    for sec in K["fixed"]:
        v = result.get(sec)
        if isinstance(v, list) and v: n += len(v) + 1
        elif isinstance(v, dict) and any(v.values()): n += 3
    return n

def _apply_page_budget(result, lang, max_pages, auto_fill):
    """선택 경험은 무조건 표시, 나머지는 직무 관련도 높은 순으로 1페이지 예산까지 자동 채움.
    각 경험 항목에 표시/display(bool), 표시순위/display_rank(int|null)를 코드가 부여."""
    K = _KEYS[lang if lang == "ko" else "en"]
    entries = [(sec, it) for sec in _EXP_SECTIONS[lang if lang == "ko" else "en"]
               for it in (result.get(sec) or []) if isinstance(it, dict)]
    if not entries:
        result["meta"]["표시된_경험수" if lang == "ko" else "displayed_experience_count"] = 0
        result["meta"]["보류된_경험수" if lang == "ko" else "held_experience_count"] = 0
        return
    budget = max(12, _PAGE_LINE_BUDGET * max(1, max_pages) - _fixed_overhead(result, K))
    # 우선순위: ① 선택 경험 최우선 ② 직무 관련도 desc ③ 원래 순서(안정)
    order_idx = sorted(range(len(entries)),
                       key=lambda i: (-int(bool(entries[i][1].get(K["sel"]))),
                                      -_rel_score(entries[i][1], K), i))
    used, rank, shown, held = 0, 0, 0, 0
    for i in order_idx:
        it = entries[i][1]
        cost = _item_cost(it, K)
        is_sel = bool(it.get(K["sel"]))
        if is_sel or (auto_fill and used + cost <= budget):
            rank += 1; shown += 1; used += cost
            it[K["disp"]] = True; it[K["rank"]] = rank
        else:
            held += 1
            it[K["disp"]] = False; it[K["rank"]] = None
    result["meta"]["표시된_경험수" if lang == "ko" else "displayed_experience_count"] = shown
    result["meta"]["보류된_경험수" if lang == "ko" else "held_experience_count"] = held
    if held:
        print(f"  [1페이지] 관련도순 {shown}건 표시, {held}건 보류(display=false, 예산 {budget}줄)", flush=True)


# ══════════════════════════════════════════════
# 핵심 생성 함수 — v1 시그니처 + 확장 인자
# ══════════════════════════════════════════════
def generate(raw_content, personal, lang="ko", target_role="", template="auto",
             selected_experiences=None, max_pages=1, auto_fill=True):
    """lang: 'ko' | 'en'"""
    sys_p = _SYS_KO if lang == "ko" else _SYS_EN
    tpl_key = _resolve_template(template, target_role)
    prompt = _build_prompt(raw_content, personal, lang, target_role, tpl_key, selected_experiences)

    print(f"  Gemini 호출 중 (language={lang}, template={tpl_key})...", flush=True)
    raw = _call(sys_p, prompt)
    result = json.loads(_clean(raw))

    today = date.today().strftime("%Y-%m-%d")
    result.setdefault("meta", {})
    result["meta"].update({
        "language": lang,
        "generated_at": today,
        "source_chars": len(raw_content),
        "target_role": (target_role or None),
        "template": tpl_key,
        "selected_experiences": [s for s in (selected_experiences or []) if s and s.strip()] or None,
        "max_pages": max_pages,
        "auto_fill": auto_fill,
        "page_line_budget": _PAGE_LINE_BUDGET,
    })

    _verify_selected(result, selected_experiences, lang)
    _apply_page_budget(result, lang, max_pages, auto_fill)
    order = _TPL_KO[tpl_key] if lang == "ko" else _TPL_EN[tpl_key]
    return _reorder(result, order, lang)


# ══════════════════════════════════════════════
# Entry Point — v1 시그니처 + 3개 파라미터
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
    target_role: str = "",                    # ★ 추가: 지원 직무
    template: str = "auto",                    # ★ 추가: 이력서 템플릿 ("auto"면 직무로 자동)
    selected_experiences: list[str] = None,    # ★ 추가: 유저가 선택한 경험(제목/키워드)
    max_pages: int = 1,                        # ★ 추가: 이력서 페이지 수 상한 (기본 1페이지)
    auto_fill: bool = True,                    # ★ 추가: 남는 공간을 관련도순 경험으로 자동 채움
    language: str = "both",
    output_path: str = "resume.json",
    deep_crawl: bool = True,
):
    """
    Args:
        sources:              URL / 파일 경로 / 텍스트 목록 (여러 개 조합 가능)
        name_ko ~ links:      인적사항 (v1 동일)
        target_role:          ★ 지원 직무 (예: "백엔드 개발자"). 빈 값이면 직무관련성 전부 '없음',
                              선택되지 않은 경험도 배제하지 않음.
        template:             ★ 이력서 템플릿 키. "auto"(기본)면 target_role로 자동 결정.
                              선택지: auto | software | data_ai | design | marketing |
                                     business | finance | research | general
        selected_experiences: ★ 유저가 선택한 경험의 제목/키워드 목록.
                              - 선택된 경험 → 반드시 포함 + LLM 요약·분류
                              - 선택 안 된 경험 → LLM이 직무 관련성 기준으로 요약·분류·포함 판단
                              - None/빈 목록이면 전 경험을 LLM 판단에 위임
                              ※ 선택은 보통 2-패스: 1차 실행으로 추출된 경험 제목을 확인 →
                                유저가 고른 제목/키워드를 이 파라미터로 다시 실행.
        max_pages:            ★ 이력서 페이지 수 상한 (기본 1). 코드가 각 경험의 렌더 줄 수를
                              추정해 예산 안에서 표시할 경험을 결정. 선택 경험은 예산과 무관하게
                              항상 표시. 나머지는 직무 관련도(CORE>RELATED>WEAK) 높은 순으로 채움.
        auto_fill:            ★ True(기본)면 선택 경험 외 남는 공간을 관련도 높은 경험으로 자동 채움.
                              False면 선택 경험만 표시(표시=true)하고 나머지는 표시=false로 보류.
                              ※ 배제가 아니라 표시 플래그만 부여 — 모든 경험은 JSON에 남고,
                                프론트는 표시=true(표시순위 순)만 렌더하면 1페이지가 됨.
        language:             "ko" | "en" | "both"
        output_path:          결과 저장 경로 (both면 _ko.json / _en.json 자동 분리)
        deep_crawl:           True = 같은 도메인 링크까지 순회
    """
    assert isinstance(max_pages, int) and max_pages >= 1, "max_pages는 1 이상의 정수"
    assert language in ("ko", "en", "both"), "language는 'ko', 'en', 'both' 중 하나"
    assert template in (["auto"] + _TPL_KEYS), f"template은 'auto' 또는 {_TPL_KEYS} 중 하나"

    tpl_key = _resolve_template(template, target_role)
    print("=" * 55)
    print("  Resume JSON Generator (Selective Ver.)")
    print(f"  모델: {MODEL}  |  언어: {language}")
    print(f"  직무: {target_role or '(미지정)'}  |  템플릿: {template} → {tpl_key}")
    print(f"  페이지 상한: {max_pages}  |  관련도 자동채움: {auto_fill}")
    if selected_experiences:
        print(f"  선택 경험: {[s for s in selected_experiences if s and s.strip()]}")
    print("=" * 55)

    # 데이터 수집
    parts = [p for s in sources if (p := resolve(s, deep=deep_crawl))]
    if not parts:
        print("ERROR: 입력 데이터 없음", flush=True); sys.exit(1)

    raw_content = "\n\n---\n\n".join(parts)
    print(f"\n총 입력: {len(raw_content)}자\n", flush=True)

    personal = dict(name_ko=name_ko, name_en=name_en, email=email,
                    phone=phone, school=school, department=department, links=links)

    # 언어별 생성
    langs = ["ko", "en"] if language == "both" else [language]
    base, ext = os.path.splitext(output_path)

    results = {}
    for lang in langs:
        result = generate(raw_content, personal, lang,
                          target_role=target_role, template=template,
                          selected_experiences=selected_experiences,
                          max_pages=max_pages, auto_fill=auto_fill)
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

    return results if language == "both" else results[language]


# ══════════════════════════════════════════════
# 실행 예시
# ══════════════════════════════════════════════
if __name__ == "__main__":
    # 사용자 데이터를 아래 빈 값에 채운 뒤 실행하세요.
    #   - sources    : URL / 파일 경로 / 직접 입력 텍스트 목록 (여러 개 조합 가능)
    #   - target_role: 지원 직무 (빈 값이면 직무 필터 없음)
    #   - template   : "auto" | software | data_ai | design | marketing |
    #                  business | finance | research | general
    #   - selected_experiences: 반드시 포함할 경험의 제목/키워드 목록
    main(
        sources=[],
        name_ko="",
        name_en="",
        email="",
        phone="",
        school="",
        department="",
        links="",
        target_role="",
        template="auto",
        selected_experiences=[],
        max_pages=1,
        auto_fill=True,
        language="both",
        output_path="resume.json",
        deep_crawl=True,
    )
