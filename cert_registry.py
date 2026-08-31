"""
cert_registry.py  (Career Analysis AI v1.2)
============================================================================
자격증 실재성 검증 레지스트리 — 공신력 있는 출처에서 종목 목록을 수집한다.

v1.2 에서 신규 추가된 모듈이다.

────────────────────────────────────────────────────────────────────────
왜 만들었나
────────────────────────────────────────────────────────────────────────
v1.1 은 자격증 화이트리스트를 코드 안에 상수로 박아두었다. "사회분석사" 같은
환각 자격증은 확실히 막았지만, 목록 자체가 고정이라 신설·폐지·개명되는
종목을 반영하려면 사람이 코드를 고쳐야 했다. 감사 보고서에서 "화이트리스트가
서서히 낡는다"고 지적한 부분이 이것이다.

이 모듈은 자격 시험을 실제로 주관하는 기관의 공개 API 에서 종목 목록을
가져와 캐시하고, 검증을 통과한 경우에만 레지스트리에 반영한다.

────────────────────────────────────────────────────────────────────────
설계 원칙 — 자동화가 오히려 환각을 만들지 않도록
────────────────────────────────────────────────────────────────────────
외부에서 목록을 받아오는 순간, 잘못된 응답(엔드포인트 변경, 인증 실패 안내
HTML, 빈 결과)이 좋은 목록을 덮어쓸 위험이 새로 생긴다. 그래서 수집 결과는
아래를 전부 통과해야만 채택된다:

  1) 앵커 검증  — 해당 출처에 반드시 있어야 할 실재 종목(정보처리기사 등)이
                  결과에 포함되어야 한다. 하나라도 없으면 응답 전체를 폐기.
  2) 규모 검증  — 종목 수가 출처별 최소 기준 이상이어야 한다.
  3) 형태 검증  — 종목명이 사람이 읽는 자격증명 형태여야 한다.
                  (HTML 조각·JSON 파편·과도한 길이는 개별 폐기)
  4) 합집합 반영 — 검증을 통과해도 기존 목록을 '대체' 하지 않고 '합집합' 한다.
                  국제 자격(AWS·PMP·TOEIC)은 국내 기관이 제공하지 않으므로
                  내장 목록이 항상 바닥을 받친다.
  5) 폐지 후보는 사람이 판단 — 국내 종목 중 최신 수집 결과에 없는 항목은
                  자동 삭제하지 않고 stale_candidates 로 보고만 한다.

즉 파이프라인은 목록을 넓히는 방향으로만 자동 동작하고, 줄이는 판단은
사람에게 남긴다. 네트워크가 끊겨도, 키가 없어도, 응답이 깨져도 검증은
내장 목록으로 계속 동작한다.

────────────────────────────────────────────────────────────────────────
사용법
────────────────────────────────────────────────────────────────────────
  # 상태 확인 (캐시 신선도, 출처별 수집 현황)
  python cert_registry.py --status

  # 공식 출처에서 목록 갱신 (네트워크 필요)
  export DATA_GO_KR_SERVICE_KEY="공공데이터포털에서 발급받은 디코딩 키"
  python cert_registry.py --refresh

  # 개별 자격증명 검증
  python cert_registry.py --check 사회분석사

환경변수:
  DATA_GO_KR_SERVICE_KEY : 공공데이터포털(data.go.kr) 일반 인증키(Decoding)
  CAREER_CERT_CACHE      : 캐시 파일 경로 (기본 ~/.cache/career_ai/certs.json)
  CAREER_CERT_TTL_DAYS   : 캐시 유효 기간, 일 단위 (기본 30)
  CAREER_CERT_OFFLINE    : "1" 이면 네트워크 수집을 아예 시도하지 않음
============================================================================
"""

from __future__ import annotations

import json
import os
import re
import sys
import difflib
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

__version__ = "1.2.0"

_KST = timezone(timedelta(hours=9), name="KST")

_CACHE_PATH = os.getenv(
    "CAREER_CERT_CACHE",
    os.path.join(os.path.expanduser("~"), ".cache", "career_ai", "certs.json"),
)
_TTL_DAYS = int(os.getenv("CAREER_CERT_TTL_DAYS", "30"))
_OFFLINE = os.getenv("CAREER_CERT_OFFLINE", "") == "1"
_FETCH_TIMEOUT = 20
_MAX_PAGES = 40
_PAGE_SIZE = 500


def _log(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


# ══════════════════════════════════════════════
# 1  출처 정의
# ══════════════════════════════════════════════
@dataclass
class SourceSpec:
    """
    자격 시험 주관 기관의 공개 API 정의.

    url_templates 에 후보를 여러 개 두는 이유:
      공공데이터포털은 서비스 개편 시 엔드포인트 경로가 바뀌는 일이 있다.
      후보를 순서대로 시도하고, 앵커 검증을 통과한 첫 응답만 채택한다.
      운영 중 경로가 또 바뀌면 CAREER_CERT_API_URL 로 덮어쓸 수 있다.
    """
    key: str
    label: str
    authority: str                 # 주관 기관
    homepage: str                  # 사람이 직접 확인할 공식 페이지
    url_templates: list[str]
    key_env: str | None
    fmt: str                       # "json" | "xml"
    name_fields: tuple[str, ...]   # 종목명이 담기는 필드/태그 후보
    anchors: tuple[str, ...]       # 이 출처에 반드시 있어야 하는 실재 종목
    min_count: int
    domestic: bool = True          # 국내 국가자격 여부 (폐지 후보 판단에 사용)


# 공공데이터포털(data.go.kr) 게이트웨이를 통한 한국산업인력공단 자격 정보.
# serviceKey 는 포털에서 활용신청 후 발급되는 일반 인증키(Decoding)를 사용한다.
_DATA_GO_KR = "https://apis.data.go.kr"

SOURCES: list[SourceSpec] = [
    SourceSpec(
        key="hrdk_national_technical",
        label="국가기술자격 종목",
        authority="한국산업인력공단 (Q-Net)",
        homepage="https://www.q-net.or.kr",
        url_templates=[
            f"{_DATA_GO_KR}/B490007/qualExamSchd/getQualExamSchdList",
            f"{_DATA_GO_KR}/B490007/qualifiCationList/getQualifiCationList",
            "https://openapi.q-net.or.kr/api/service/rest/InquiryQualInfoSVC/getList",
        ],
        key_env="DATA_GO_KR_SERVICE_KEY",
        fmt="xml",
        name_fields=("jmNm", "jmfldnm", "jmcd_nm", "qualgbnm", "itemName", "jmNam"),
        anchors=("정보처리기사", "산업안전기사", "사회조사분석사"),
        min_count=200,
    ),
    SourceSpec(
        key="hrdk_national_professional",
        label="국가전문자격 종목",
        authority="한국산업인력공단 (Q-Net)",
        homepage="https://www.q-net.or.kr",
        url_templates=[
            f"{_DATA_GO_KR}/B490007/qualExamSchdNPQ/getQualExamSchdNPQList",
            "https://openapi.q-net.or.kr/api/service/rest/InquiryTestInformationNPQSVC/getList",
        ],
        key_env="DATA_GO_KR_SERVICE_KEY",
        fmt="xml",
        name_fields=("jmNm", "jmfldnm", "qualgbnm", "itemName"),
        anchors=("공인노무사", "관세사"),
        min_count=20,
    ),
]

# 환경변수로 엔드포인트를 통째로 덮어쓸 수 있는 탈출구.
# 포털에서 경로가 바뀌었을 때 코드 수정 없이 대응하기 위함.
_URL_OVERRIDE = os.getenv("CAREER_CERT_API_URL", "")
if _URL_OVERRIDE:
    SOURCES[0].url_templates.insert(0, _URL_OVERRIDE)


# ══════════════════════════════════════════════
# 2  내장 시드 목록 (네트워크 없이도 항상 동작하는 바닥)
# ══════════════════════════════════════════════
#  공식 출처 수집이 실패하거나(키 없음·네트워크 차단·응답 이상) 아직 한 번도
#  갱신하지 않았을 때 쓰이는 기본 목록. 수집에 성공해도 이 목록은 제거되지
#  않고 합집합으로 유지된다 — 국제 자격(AWS·PMP·TOEIC 등)은 국내 자격 기관이
#  제공하지 않으므로 이 시드가 유일한 출처다.

SEED_CERTS: list[str] = [
    # ── IT / 데이터 (국가기술자격) ──
    "정보처리기사", "정보처리산업기사", "정보처리기능사",
    "정보보안기사", "정보보안산업기사",
    "빅데이터분석기사", "전자계산기조직응용기사", "전자계산기기사",
    "정보통신기사", "정보통신산업기사", "무선설비기사", "방송통신기사",
    "정보기기운용기능사", "컴퓨터활용능력", "워드프로세서",
    # ── IT 민간·공인 ──
    "SQLD", "SQL 개발자", "SQLP", "SQL 전문가",
    "ADsP", "데이터분석 준전문가", "ADP", "데이터분석 전문가",
    "DAsP", "DAP", "데이터아키텍처 준전문가",
    "리눅스마스터", "네트워크관리사", "PC정비사",
    "GTQ", "GTQi", "COS", "COS Pro",
    "멀티미디어콘텐츠제작전문가", "게임그래픽전문가", "게임기획전문가",
    "게임프로그래밍전문가", "웹디자인기능사", "컴퓨터그래픽스운용기능사",
    "시각디자인산업기사", "제품디자인기사", "전자출판기능사",
    # ── 경영 / 사무 / 조사 ──
    "사회조사분석사",          # ★ "사회분석사" 는 실재하지 않음 (여기가 정본)
    "소비자전문상담사", "텔레마케팅관리사", "CS리더스관리사",
    "비서", "경영지도사", "기술지도사",
    "품질경영기사", "품질경영산업기사",
    "물류관리사", "유통관리사", "국제무역사", "무역영어",
    "ERP정보관리사", "전산세무", "전산회계", "재경관리사", "회계관리",
    "FAT", "TAT", "IFRS관리사",
    # ── 금융 ──
    "투자자산운용사", "금융투자분석사", "증권투자권유자문인력",
    "펀드투자권유자문인력", "파생상품투자권유자문인력",
    "재무위험관리사", "FRM", "CFA", "AFPK", "CFP",
    "신용분석사", "여신심사역", "자산관리사", "은행텔러",
    "국제금융역", "외환전문역", "보험계리사", "손해사정사",
    # ── 전문 자격 (국가전문자격) ──
    "공인회계사", "세무사", "변리사", "관세사", "감정평가사",
    "공인노무사", "법무사", "행정사", "공인중개사", "주택관리사",
    "사회복지사", "보육교사", "직업상담사", "청소년상담사",
    "임상심리사", "정신건강임상심리사", "평생교육사", "건강가정사",
    # ── 안전 / 환경 / 산업 ──
    "산업안전기사", "산업안전산업기사", "건설안전기사", "건설안전산업기사",
    "산업위생관리기사", "인간공학기사", "소방설비기사", "소방안전관리자",
    "위험물산업기사", "위험물기능사", "가스기사", "가스산업기사",
    "대기환경기사", "수질환경기사", "폐기물처리기사", "소음진동기사",
    "토양환경기사", "자연생태복원기사", "온실가스관리기사",
    # ── 건설 / 기계 / 전기 ──
    "토목기사", "건축기사", "건축설비기사", "실내건축기사", "조경기사",
    "측량및지형공간정보기사", "건설재료시험기사",
    "일반기계기사", "기계설계기사", "공조냉동기계기사", "에너지관리기사",
    "생산자동화산업기사", "메카트로닉스기사", "용접기사", "금형기사",
    "전기기사", "전기산업기사", "전기공사기사", "전기기능사",
    "전자기사", "전자산업기사", "화공기사", "화학분석기사",
    "자동차정비기사", "자동차정비산업기사",
    # ── 식품 / 서비스 / 기타 ──
    "식품기사", "식품산업기사", "영양사", "위생사",
    "조리기능사", "한식조리기능사", "양식조리기능사", "중식조리기능사",
    "일식조리기능사", "복어조리기능사", "제과기능사", "제빵기능사",
    "바리스타", "미용사", "컨벤션기획사", "관광통역안내사",
    "국내여행안내사", "호텔경영사", "호텔관리사", "스포츠지도사",
    "생활스포츠지도사", "건설기계운전기능사", "지게차운전기능사",
    # ── 어학 / 한국사 ──
    "한국사능력검정시험", "KBS한국어능력시험", "국어능력인증시험",
    "TOEIC", "TOEIC Speaking", "TOEIC Writing", "OPIc", "TEPS",
    "TOEFL", "IELTS", "HSK", "JLPT", "JPT", "DELE", "DELF", "TestDaF",
    "FLEX", "SNULT",
    # ── 국제 IT / 클라우드 / PM ──
    "AWS Certified Cloud Practitioner",
    "AWS Certified Solutions Architect Associate",
    "AWS Certified Solutions Architect Professional",
    "AWS Certified Developer Associate",
    "AWS Certified SysOps Administrator Associate",
    "AWS Certified DevOps Engineer Professional",
    "AWS Certified Machine Learning Specialty",
    "Microsoft Certified Azure Fundamentals", "AZ-900",
    "Microsoft Certified Azure Administrator Associate", "AZ-104",
    "Microsoft Certified Azure Developer Associate", "AZ-204",
    "Microsoft Certified Azure Data Scientist Associate", "DP-100",
    "Google Cloud Associate Cloud Engineer",
    "Google Cloud Professional Cloud Architect",
    "Google Cloud Professional Data Engineer",
    "CKA", "Certified Kubernetes Administrator",
    "CKAD", "Certified Kubernetes Application Developer", "CKS",
    "RHCSA", "RHCE", "CCNA", "CCNP",
    "OCA", "OCP", "OCJP", "Oracle Certified Professional",
    "CompTIA Security+", "CompTIA Network+", "CompTIA A+", "CompTIA Linux+",
    "CISA", "CISM", "CISSP", "CEH", "ISMS-P 인증심사원",
    "PMP", "CAPM", "PRINCE2", "ITIL Foundation",
    "MOS", "Microsoft Office Specialist",
    "Tableau Desktop Specialist", "Tableau Certified Data Analyst",
    "SnowPro Core", "Databricks Certified Data Engineer Associate",
    "Google Ads Certification", "Google Analytics Certification",
]


# 반복 관측된 환각 자격증 — 어떤 출처에서 들어오든 항상 차단
BLOCKED_CERTS: list[str] = [
    "사회분석사",            # 실재하지 않음 (혼동 대상: 사회조사분석사)
    "데이터분석사",          # 실재하지 않음 (혼동 대상: ADsP / ADP)
    "빅데이터분석사",        # 실재하지 않음 (혼동 대상: 빅데이터분석기사)
    "경영분석사",
    "취업컨설턴트자격증",
    "커리어코치자격증",
    "AI활용능력사",
    "디지털역량인증사",
    "인공지능전문가자격증",
]


# ══════════════════════════════════════════════
# 3  정규화 & 검증 규칙
# ══════════════════════════════════════════════
def norm_cert(name: str) -> str:
    """자격증명 정규화: 괄호·구분자·급수·장식어 제거 후 소문자화."""
    if not name:
        return ""
    s = str(name)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[\s_\-·.]+", "", s)
    s = re.sub(r"(특급|고급|중급|초급|[1-9]급|Level[1-9])$", "", s, flags=re.I)
    s = re.sub(r"(자격증|자격|시험|취득|과정)$", "", s)
    return s.lower()


# 종목명으로 인정할 수 있는 형태인지 (HTML·JSON 파편·안내문 걸러내기)
_NAME_OK = re.compile(r"^[0-9A-Za-z가-힣ㆍ·\s()\-+/&'.,]{2,40}$")
_NAME_BAD = re.compile(
    r"(<[a-z/!]|\{|\}|\[|\]|&[a-z]+;|https?://|SERVICE|ERROR|없습니다|잘못된|인증키|"
    r"등록되지|서비스|재시도|점검)",
    re.IGNORECASE,
)


def _plausible_cert_name(name: str) -> bool:
    n = (name or "").strip()
    if not n or not _NAME_OK.match(n) or _NAME_BAD.search(n):
        return False
    # 한글이나 영문 알파벳이 최소한 두 글자는 있어야 한다
    return len(re.findall(r"[가-힣A-Za-z]", n)) >= 2


# ══════════════════════════════════════════════
# 4  수집 (fetch)
# ══════════════════════════════════════════════
def _http_get(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "career-analysis-ai/1.2 (cert-registry)",
                "Accept": "application/json, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        _log(f"      HTTP {e.code} — {url.split('?')[0]}")
    except Exception as e:
        _log(f"      요청 실패: {type(e).__name__}: {e}")
    return None


def _walk_json_names(node, fields: tuple[str, ...], out: list[str]) -> None:
    """응답 구조를 몰라도 되도록, 지정 필드명을 재귀 탐색해 값을 모은다."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in fields and isinstance(v, str):
                out.append(v)
            else:
                _walk_json_names(v, fields, out)
    elif isinstance(node, list):
        for item in node:
            _walk_json_names(item, fields, out)


def _walk_xml_names(root: ET.Element, fields: tuple[str, ...], out: list[str]) -> None:
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in fields and el.text and el.text.strip():
            out.append(el.text.strip())


def _extract_names(payload: bytes, spec: SourceSpec) -> list[str]:
    """JSON/XML 어느 쪽이든 종목명 후보를 뽑아낸다."""
    text = payload.decode("utf-8", errors="replace").strip()
    names: list[str] = []

    if text.startswith("{") or text.startswith("["):
        try:
            _walk_json_names(json.loads(text), spec.name_fields, names)
        except json.JSONDecodeError:
            pass
    if not names:
        try:
            _walk_xml_names(ET.fromstring(text), spec.name_fields, names)
        except ET.ParseError:
            pass
    return names


def _build_url(base: str, service_key: str, page: int, spec: SourceSpec) -> str:
    params = {
        "serviceKey": service_key,
        "pageNo": str(page),
        "numOfRows": str(_PAGE_SIZE),
        "dataFormat": spec.fmt,
        "_type": spec.fmt,
    }
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(params)


def _fetch_source(spec: SourceSpec) -> dict:
    """
    한 출처에서 종목명을 수집한다.
    반환: {status, names, endpoint, error}
      status ∈ ok | no_key | unreachable | rejected
    """
    service_key = os.getenv(spec.key_env or "", "").strip() if spec.key_env else ""
    if spec.key_env and not service_key:
        return {"status": "no_key", "names": [],
                "error": f"환경변수 {spec.key_env} 미설정 — 공공데이터포털에서 활용신청 후 발급"}

    for base in spec.url_templates:
        _log(f"    시도: {base}")
        collected: list[str] = []
        for page in range(1, _MAX_PAGES + 1):
            payload = _http_get(_build_url(base, service_key, page, spec))
            if payload is None:
                break
            page_names = _extract_names(payload, spec)
            if not page_names:
                break
            collected.extend(page_names)
            if len(page_names) < _PAGE_SIZE:
                break

        if not collected:
            continue

        clean = sorted({n.strip() for n in collected if _plausible_cert_name(n)})
        ok, reason = _validate(clean, spec)
        if ok:
            _log(f"      OK {len(clean)}개 종목 수집")
            return {"status": "ok", "names": clean, "endpoint": base, "error": None}
        _log(f"      폐기: {reason}")

    return {"status": "unreachable", "names": [], "endpoint": None,
            "error": "모든 후보 엔드포인트에서 유효한 응답을 얻지 못함"}


def _validate(names: list[str], spec: SourceSpec) -> tuple[bool, str]:
    """
    앵커·규모 검증. 이 관문을 통과하지 못한 응답은 레지스트리에 반영되지 않는다.
    엔드포인트가 바뀌었거나 인증 오류 안내가 돌아온 경우를 여기서 잡는다.
    """
    if len(names) < spec.min_count:
        return False, f"종목 수 부족 ({len(names)} < 최소 {spec.min_count})"

    normalized = {norm_cert(n) for n in names}
    missing = [a for a in spec.anchors if norm_cert(a) not in normalized]
    if missing:
        return False, f"앵커 종목 누락 ({', '.join(missing)}) — 엔드포인트/응답 형식 확인 필요"

    return True, "검증 통과"


# ══════════════════════════════════════════════
# 5  캐시
# ══════════════════════════════════════════════
def _read_cache() -> dict | None:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("certs"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_cache(data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _CACHE_PATH)   # 원자적 교체 — 쓰다 죽어도 기존 캐시 보존
        return True
    except OSError as e:
        _log(f"  캐시 저장 실패: {e}")
        return False


def _cache_age_days(data: dict) -> float | None:
    try:
        fetched = datetime.fromisoformat(data["fetched_at"])
        return (datetime.now(_KST) - fetched).total_seconds() / 86400
    except (KeyError, ValueError, TypeError):
        return None


# ══════════════════════════════════════════════
# 6  레지스트리
# ══════════════════════════════════════════════
@dataclass
class CertRegistry:
    names: list[str] = field(default_factory=list)
    origin: str = "seed"           # seed | cache | live
    fetched_at: str | None = None
    age_days: float | None = None
    sources: list[dict] = field(default_factory=list)
    stale_candidates: list[str] = field(default_factory=list)
    _index: dict[str, str] = field(default_factory=dict, repr=False)
    _blocked: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self):
        self._index = {norm_cert(c): c for c in self.names}
        self._blocked = {norm_cert(c) for c in BLOCKED_CERTS}

    # ── 검증 ──
    def verify(self, name: str, strict: bool = True) -> tuple[bool, str]:
        """
        (통과여부, 사유).
        정본명이 후보명의 부분문자열이면 통과 ("정보처리기사 실기" OK).
        반대 방향은 통과시키지 않는다 → "사회분석사" 차단.
        """
        n = norm_cert(name)
        if not n:
            return False, "자격증명이 비어 있음"

        if n in self._blocked:
            close = difflib.get_close_matches(n, list(self._index), n=1, cutoff=0.6)
            hint = f" (실재 유사 자격: {self._index[close[0]]})" if close else ""
            return False, f"실재하지 않는 자격증으로 확인됨{hint}"

        if n in self._index:
            return True, "검증 통과"

        for known in self._index:
            if len(known) >= 3 and known in n:
                return True, "검증 통과"

        if not strict:
            return True, "비엄격 모드 통과"

        close = difflib.get_close_matches(n, list(self._index), n=1, cutoff=0.7)
        hint = f" (오기 가능성: {self._index[close[0]]})" if close else ""
        return False, f"실재 확인 불가 — 검증된 자격증 목록에 없음{hint}"

    def prompt_block(self, limit: int = 220) -> str:
        """프롬프트에 주입할 화이트리스트 블록."""
        shown = self.names[:limit]
        src = {
            "live": f"공식 출처 수집 ({self.fetched_at})",
            "cache": f"공식 출처 캐시 (수집 {self.fetched_at})",
            "seed":  "내장 기본 목록",
        }.get(self.origin, self.origin)
        more = f"\n(외 {len(self.names) - len(shown)}종 추가 검증 가능)" if len(self.names) > len(shown) else ""
        return (
            "=== [자격증 추천 화이트리스트 — 이 목록 밖은 전부 자동 폐기] ===\n"
            f"출처: {src} · 총 {len(self.names)}종\n"
            "category 가 '자격증'인 추천은 아래 목록에 있는 이름만 사용하십시오.\n"
            "목록에 없는 자격증을 쓰면 후처리에서 삭제되어 사용자에게 도달하지 않습니다.\n"
            "이름을 변형·축약·조합하지 말고 아래 표기를 그대로 쓰십시오.\n"
            "(예: '사회분석사'는 존재하지 않는 이름입니다. 정확한 명칭은 '사회조사분석사'입니다.)\n"
            "추천할 만한 것이 목록에 없으면 자격증 추천을 생략하고 다른 category 로 대체하십시오.\n\n"
            + ", ".join(shown) + more + "\n" + "=" * 44
        )

    def status_lines(self) -> list[str]:
        lines = [
            f"레지스트리: {len(self.names)}종 (출처: {self.origin})",
            f"캐시 경로 : {_CACHE_PATH}",
        ]
        if self.fetched_at:
            age = f"{self.age_days:.1f}일 전" if self.age_days is not None else "?"
            fresh = "신선" if (self.age_days or 999) < _TTL_DAYS else f"만료 (TTL {_TTL_DAYS}일)"
            lines.append(f"마지막 수집: {self.fetched_at} ({age}, {fresh})")
        else:
            lines.append("마지막 수집: 없음 — 내장 목록만 사용 중")
        for s in self.sources:
            mark = {"ok": "OK  ", "no_key": "KEY ", "unreachable": "NET ", "rejected": "REJ "}
            lines.append(
                f"  [{mark.get(s.get('status'), '??  ')}] {s.get('label', s.get('key'))}"
                f" — {s.get('count', 0)}종"
                + (f" · {s['error']}" if s.get("error") else "")
            )
        if self.stale_candidates:
            lines.append(
                f"  폐지 후보 {len(self.stale_candidates)}건 (자동 삭제 안 함, 사람이 확인 필요): "
                + ", ".join(self.stale_candidates[:10])
            )
        return lines


# ══════════════════════════════════════════════
# 7  로딩 파이프라인
# ══════════════════════════════════════════════
def _merge(fetched: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """
    합집합 반영 + 폐지 후보 산출.
    시드를 절대 축소하지 않는다 — 국제 자격은 국내 기관이 제공하지 않기 때문.
    """
    all_fetched: list[str] = []
    for names in fetched.values():
        all_fetched.extend(names)

    merged = list(SEED_CERTS)
    seen = {norm_cert(c) for c in merged}
    for name in all_fetched:
        n = norm_cert(name)
        if n and n not in seen:
            merged.append(name)
            seen.add(n)

    # 국내 종목으로 보이는 시드 중 이번 수집에 없는 것 = 폐지·개명 후보
    stale: list[str] = []
    if all_fetched:
        fetched_norm = {norm_cert(n) for n in all_fetched}
        for c in SEED_CERTS:
            if re.search(r"(기사|산업기사|기능사|기능장|기술사)$", c) and norm_cert(c) not in fetched_norm:
                stale.append(c)

    return merged, stale


def refresh(verbose: bool = True) -> CertRegistry:
    """공식 출처에서 수집해 캐시를 갱신한다."""
    if verbose:
        _log("[자격증 레지스트리] 공식 출처 수집 시작")

    fetched: dict[str, list[str]] = {}
    source_meta: list[dict] = []

    for spec in SOURCES:
        if verbose:
            _log(f"  · {spec.label} — {spec.authority}")
        if _OFFLINE:
            source_meta.append({"key": spec.key, "label": spec.label, "status": "unreachable",
                                "count": 0, "error": "CAREER_CERT_OFFLINE=1"})
            continue
        result = _fetch_source(spec)
        if result["status"] == "ok":
            fetched[spec.key] = result["names"]
        source_meta.append({
            "key": spec.key, "label": spec.label, "authority": spec.authority,
            "homepage": spec.homepage, "status": result["status"],
            "count": len(result["names"]), "endpoint": result.get("endpoint"),
            "error": result.get("error"),
        })

    merged, stale = _merge(fetched)
    now = datetime.now(_KST)

    if fetched:
        _write_cache({
            "version": 1,
            "fetched_at": now.isoformat(timespec="seconds"),
            "sources": source_meta,
            "certs": merged,
            "stale_candidates": stale,
        })
        origin = "live"
        if verbose:
            _log(f"  수집 완료: {len(merged)}종 (신규 {len(merged) - len(SEED_CERTS)}종)")
    else:
        origin = "seed"
        if verbose:
            _log("  수집 실패 — 내장 목록으로 계속 동작합니다 (검증 기능은 정상)")

    return CertRegistry(
        names=merged, origin=origin,
        fetched_at=now.isoformat(timespec="seconds") if fetched else None,
        age_days=0.0 if fetched else None,
        sources=source_meta, stale_candidates=stale,
    )


def load_certs(allow_refresh: bool = True) -> CertRegistry:
    """
    레지스트리 로드. 폴백 순서:
      신선한 캐시 → (만료 시) 공식 출처 수집 → 만료된 캐시 → 내장 시드
    어느 단계에서 실패해도 검증 기능 자체는 항상 동작한다.
    """
    cached = _read_cache()
    if cached:
        age = _cache_age_days(cached)
        if age is not None and age < _TTL_DAYS:
            return CertRegistry(
                names=cached["certs"], origin="cache",
                fetched_at=cached.get("fetched_at"), age_days=age,
                sources=cached.get("sources", []),
                stale_candidates=cached.get("stale_candidates", []),
            )

    if allow_refresh and not _OFFLINE:
        reg = refresh(verbose=False)
        if reg.origin == "live":
            return reg

    if cached:   # 만료됐어도 내장 시드보다는 최신이다
        return CertRegistry(
            names=cached["certs"], origin="cache",
            fetched_at=cached.get("fetched_at"), age_days=_cache_age_days(cached),
            sources=cached.get("sources", []),
            stale_candidates=cached.get("stale_candidates", []),
        )

    return CertRegistry(names=list(SEED_CERTS), origin="seed")


# ══════════════════════════════════════════════
# 8  CLI
# ══════════════════════════════════════════════
def _main(argv: list[str]) -> int:
    args = argv[1:]

    if "--refresh" in args:
        reg = refresh(verbose=True)
        print("\n".join(reg.status_lines()))
        return 0 if reg.origin == "live" else 1

    if "--check" in args:
        i = args.index("--check")
        if i + 1 >= len(args):
            print("사용법: python cert_registry.py --check <자격증명>")
            return 2
        reg = load_certs(allow_refresh=False)
        name = " ".join(args[i + 1:])
        ok, reason = reg.verify(name)
        print(f"{'PASS' if ok else 'DROP'}  {name}  —  {reason}")
        return 0 if ok else 1

    reg = load_certs(allow_refresh=False)
    print("\n".join(reg.status_lines()))
    if "--status" not in args:
        print("\n사용법: --status | --refresh | --check <자격증명>")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
