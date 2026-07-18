"""
=============================================================================
 AI 자기소개서 '생성' 서비스 — 단일 파일 통합본 (Google AI Studio · Gemini 2.5 Flash)
=============================================================================

★ 이 프로그램은 자소서를 '대신 써 주는' 생성 서비스입니다. (첨삭 도구가 아님)
  사용자의 사실 데이터만을 재료로, 문항별로 그대로 제출 가능한 수준의
  완성형 자기소개서를 생성하고, 그 글을 사용자가 자기 것으로 소화하도록
  돕는 '작성 가이드'와 (부가) HR 액션플랜을 함께 제공합니다.

★ 회사 맞춤 기능: 지원 회사명을 입력하면 Google 검색(Gemini 검색 그라운딩)으로
  회사의 미션/가치/인재상을 자동 조사하고, 노골적 인용 없이 글의 방향에만
  은은하게 스며들도록(penetrate) 반영합니다.

사용법 (Colab):
  1) 이 파일 전체를 복사해서 Colab의 "새 노트북" 코드 셀 하나에 그대로 붙여넣기.
  2) [1. API 키 설정] 구역에 본인 Google AI Studio 키 입력.
  3) [9. 사용자 데이터 입력] 구역에 본인의 '사실'만 입력. (지원 회사명 포함)
  4) [10. 자소서 문항 입력] 구역에 지원할 회사의 실제 문항 입력.
  5) 셀을 실행(▶️)하면 문항별 완성형 자소서가 출력됩니다.

설계 원칙:
  제1원칙(환각 방지)  : 자소서 내용은 오직 '사용자 데이터'에서만 나옵니다.
                       모범 자소서(문체 참조용)는 내용을 복사하지 않으며,
                       생성 후 근거 검증 → 자동 교정 과정을 거칩니다.
  제2원칙(문체/맞춤법) : 어미 통일('~했습니다'체), 1인칭 시점 유지, 나열식·
                       돌림노래식 반복 금지. 생성 후 최종 다듬기(polish)
                       패스로 제출본 수준까지 다듬습니다.
  직무별 구분         : 직무별 스타일/양식/HR 평가 포인트를 반영해
                       생성·가이드·액션플랜을 모두 분기합니다.
=============================================================================
"""

# =============================================================================
# 0. 패키지 설치 (최초 1회, 이미 설치돼 있으면 자동으로 건너뜀)
# =============================================================================
try:
    from google import genai  # noqa: F401
except ImportError:
    import subprocess
    import sys
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "google-genai"])

import os
import json as _json
from dataclasses import dataclass, field, asdict
from pydantic import BaseModel


# =============================================================================
# 1. [API 키 설정] ★ 여기에 본인 키를 입력하세요 ★
# =============================================================================
#  Google AI Studio(https://aistudio.google.com/apikey) 에서 발급받은 키를
#  아래 큰따옴표 안에 붙여넣으세요. 모델은 gemini-2.5-flash 로 고정되어 있습니다.
#
#  Colab에서 키를 코드에 남기고 싶지 않다면, 왼쪽 사이드바 🔑(Secrets) 메뉴에
#  'GOOGLE_AI_STUDIO_API_KEY' 이름으로 등록하면 아래 resolve_api_key() 가
#  자동으로 찾아 씁니다.
# =============================================================================
GOOGLE_AI_STUDIO_API_KEY = ""          # 예: "AIza...."
GEMINI_MODEL_NAME = "gemini-2.5-flash"

# 생성 파라미터: temperature 를 낮춰 환각(지어내기)을 최소화합니다. (제1원칙)
GENERATION_CONFIG = {
    "temperature": 0.25,
    "top_p": 0.9,
    "top_k": 40,
    "max_output_tokens": 4096,
}
# 근거 검증(사실 확인) 단계는 더 엄격(결정적)하게 설정합니다.
VERIFICATION_CONFIG = {
    "temperature": 0.0,
    "top_p": 1.0,
    "max_output_tokens": 2048,
}


def resolve_api_key() -> str:
    """Colab Secrets(🔑) 또는 환경변수 우선, 없으면 위 상수 사용."""
    try:
        from google.colab import userdata  # type: ignore
        key = userdata.get("GOOGLE_AI_STUDIO_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "") or GOOGLE_AI_STUDIO_API_KEY


# =============================================================================
# 2. 데이터 구조
#    UserProfile       : 자소서 내용의 유일한 사실 출처
#    ReferenceExample  : 모범 자소서 1건(문체 참조 전용)
#    AnswerResult      : 문항 1개의 완성형 자소서 + 부속 정보
#    ApplicationResult : 지원서 전체(여러 문항) 결과 묶음
# =============================================================================
@dataclass
class UserProfile:
    name: str = ""
    target_company: str = ""
    target_job: str = ""
    education: list = field(default_factory=list)
    experiences: list = field(default_factory=list)
    projects: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    certifications: list = field(default_factory=list)
    awards: list = field(default_factory=list)
    activities: list = field(default_factory=list)
    achievements: list = field(default_factory=list)
    strengths: list = field(default_factory=list)
    motivation: str = ""
    career_goal: str = ""
    extra_notes: str = ""

    def to_dict(self):
        return asdict(self)

    def is_empty(self) -> bool:
        d = self.to_dict()
        for k, v in d.items():
            if isinstance(v, str) and v.strip():
                return False
            if isinstance(v, list) and len(v) > 0:
                return False
        return True


@dataclass
class ReferenceExample:
    region: str = "KR"       # "KR" 또는 "US"
    job_key: str = "general"
    text: str = ""
    source: str = ""
    tags: list = field(default_factory=list)


@dataclass
class GroundingReport:
    grounded: bool = True
    unsupported_claims: list = field(default_factory=list)
    notes: str = ""


@dataclass
class AnswerResult:
    """문항 1개에 대한 완성형 자소서 + 부속 정보."""
    question: str = ""
    cover_letter: str = ""                        # ★ 주 결과물: 완성형 자소서 본문
    grounding: GroundingReport = field(default_factory=GroundingReport)
    writing_guide: str = ""                       # 이 자소서를 내 것으로 만드는 가이드


@dataclass
class ApplicationResult:
    """지원서 전체(여러 문항) 산출물 묶음."""
    answers: list = field(default_factory=list)   # AnswerResult 리스트
    company_research: str = ""                    # 회사 리서치 요약(참고용)
    action_plan: str = ""                         # (부가) HR 관점 액션플랜
    meta: dict = field(default_factory=dict)


# =============================================================================
# 3. 직무별 스타일 / 양식 / HR 평가 포인트  (+ 한국형/미국형 문화 차이)
# =============================================================================
REGION_STYLE = {
    "KR": {
        "label": "한국형 자기소개서",
        "guidance": (
            "- 문항별(지원동기/성장과정/성격의 장단점/입사 후 포부 등) 서술형 구성.\n"
            "- 존댓말/문어체, 겸손하되 근거 있는 자신감.\n"
            "- 두괄식(핵심 문장 먼저) + 구체적 경험 + 회사/직무 연결 마무리.\n"
            "- 정량적 성과(숫자)와 6하원칙 기반 심층 서술 선호.\n"
            "- 과장/추상적 미사여구 지양, 사실 기반 스토리텔링."
        ),
    },
    "US": {
        "label": "US-style Cover Letter",
        "guidance": (
            "- One-page business letter format (greeting, 3~4 paragraphs, closing).\n"
            "- Confident, achievement-oriented, action-verb driven.\n"
            "- Quantified impact ('increased X by Y%'), tailored to the role.\n"
            "- Directly map your experience to the job description keywords.\n"
            "- Concise, no personal/family background, professional tone."
        ),
    },
}

JOB_PROFILES = {
    "general": {
        "label": "일반/공통",
        "competencies": ["문제해결", "성실성", "협업", "성장의지"],
        "hr_focus": ["직무 이해도", "조직 적합성", "성장 가능성", "진정성"],
        "tone": "신뢰감 있고 담백한 문어체, 두괄식",
        "structure": ["핵심 강점 요약", "구체적 경험(심층 서술)", "직무/회사 연결", "포부"],
        "keywords": ["직무 역량", "협업", "성과", "학습"],
        "good_signals": ["직무 연관 프로젝트", "정량 성과", "지속적 학습 이력"],
    },
    "backend": {
        "label": "백엔드 개발",
        "competencies": ["자료구조/알고리즘", "데이터베이스 설계", "API/서버 아키텍처", "성능 최적화", "트러블슈팅"],
        "hr_focus": ["기술 깊이", "시스템적 사고", "협업(코드리뷰/Git)", "장애 대응 경험"],
        "tone": "논리적·정량적, 기술 용어를 정확히 사용하되 과시하지 않음",
        "structure": ["기술 문제 정의", "설계/구현 선택과 근거", "성능·장애 개선 수치", "협업 방식", "성장 방향"],
        "keywords": ["트래픽", "레이턴시", "쿼리 최적화", "테스트 커버리지", "장애 복구"],
        "good_signals": ["대용량 트래픽 처리 경험", "오픈소스 기여", "부하테스트/모니터링 구축", "코드리뷰 문화 경험"],
    },
    "frontend": {
        "label": "프론트엔드 개발",
        "competencies": ["UI/UX 구현", "상태관리", "성능(렌더링/번들)", "접근성/크로스브라우징"],
        "hr_focus": ["사용자 관점", "협업(디자이너/기획)", "코드 품질", "최신 생태계 이해"],
        "tone": "사용자 가치 중심 + 기술적 근거",
        "structure": ["사용자 문제", "구현/최적화 선택", "지표 개선(로딩/전환율)", "협업", "성장"],
        "keywords": ["렌더링 최적화", "웹 성능", "컴포넌트 설계", "접근성", "전환율"],
        "good_signals": ["Lighthouse 점수 개선", "디자인시스템 구축", "실사용자 A/B 테스트"],
    },
    "data": {
        "label": "데이터 분석/사이언스",
        "competencies": ["통계/분석", "SQL/데이터 파이프라인", "머신러닝", "인사이트 도출/시각화"],
        "hr_focus": ["가설-검증 사고", "비즈니스 임팩트로 연결", "커뮤니케이션", "재현성"],
        "tone": "가설→분석→결론의 논리 흐름, 수치로 말하기",
        "structure": ["문제/가설", "데이터·방법", "분석 결과(수치)", "의사결정 기여", "한계와 개선"],
        "keywords": ["가설검정", "지표(KPI)", "실험설계", "예측 정확도", "의사결정"],
        "good_signals": ["실제 지표를 움직인 분석", "Kaggle/공모전", "대시보드 운영", "A/B 테스트 설계"],
    },
    "pm": {
        "label": "기획/PM/PO",
        "competencies": ["문제 정의", "우선순위/로드맵", "이해관계자 조율", "데이터 기반 의사결정"],
        "hr_focus": ["오너십", "커뮤니케이션", "실행력", "성과 책임"],
        "tone": "명료하고 구조적, 결과와 학습 강조",
        "structure": ["해결한 문제", "가설과 우선순위", "실행/협업", "성과 지표", "배운 점"],
        "keywords": ["로드맵", "리텐션", "전환율", "우선순위", "이해관계자"],
        "good_signals": ["0→1 프로덕트 출시", "지표 개선 리드", "사이드 프로젝트 운영"],
    },
    "marketing": {
        "label": "마케팅",
        "competencies": ["시장/고객 이해", "캠페인 기획·실행", "퍼포먼스 분석", "브랜드 커뮤니케이션"],
        "hr_focus": ["창의성+데이터", "실행 성과(ROI)", "트렌드 감각", "협업"],
        "tone": "설득력 있고 생동감 있되 성과는 숫자로",
        "structure": ["타깃/문제", "캠페인 아이디어", "실행", "성과(전환/ROAS)", "인사이트"],
        "keywords": ["ROAS", "전환율", "타깃팅", "콘텐츠", "그로스"],
        "good_signals": ["실제 캠페인 성과 수치", "SNS 채널 운영 성장", "그로스 실험"],
    },
    "sales": {
        "label": "영업/세일즈",
        "competencies": ["고객 관계", "협상/설득", "목표 달성", "시장 이해"],
        "hr_focus": ["목표 지향성", "실적", "끈기/회복탄력성", "신뢰 형성"],
        "tone": "적극적·자신감, 실적 중심",
        "structure": ["목표/도전", "접근 전략", "실행", "실적(수치)", "관계·신뢰"],
        "keywords": ["목표 달성률", "신규 고객", "매출 성장", "협상"],
        "good_signals": ["매출/실적 달성 경험", "고객 확보 사례", "영업 인턴십"],
    },
    "hr": {
        "label": "인사/HR",
        "competencies": ["채용/평가", "조직문화", "노무/제도 이해", "데이터 기반 HR"],
        "hr_focus": ["사람에 대한 통찰", "공정성", "커뮤니케이션", "제도 실행력"],
        "tone": "균형감 있고 신뢰가는 문체",
        "structure": ["사람/조직 문제", "제도·프로그램 기획", "실행/조율", "성과", "가치관"],
        "keywords": ["채용", "온보딩", "조직문화", "리텐션", "제도 개선"],
        "good_signals": ["채용/교육 프로그램 운영", "설문·데이터로 문화 개선", "동아리 운영"],
    },
    "finance": {
        "label": "재무/회계",
        "competencies": ["재무제표 이해", "정확성", "분석/예측", "규정 준수"],
        "hr_focus": ["꼼꼼함/정확성", "윤리성", "분석력", "책임감"],
        "tone": "정확하고 신중, 근거 중심",
        "structure": ["과제", "분석/처리", "정확성 확보 방법", "성과", "직업윤리"],
        "keywords": ["재무분석", "예산", "결산", "리스크", "규정준수"],
        "good_signals": ["재무 관련 자격증", "회계 실무/인턴", "재무모델링 프로젝트"],
    },
    "design": {
        "label": "디자인(UX/UI/그래픽)",
        "competencies": ["문제 정의", "사용자 리서치", "비주얼/인터랙션", "협업"],
        "hr_focus": ["문제해결형 디자인", "포트폴리오 근거", "협업", "성과 연결"],
        "tone": "사용자·문제 중심, 담백하게",
        "structure": ["사용자 문제", "리서치/컨셉", "디자인 결정 근거", "결과(지표)", "회고"],
        "keywords": ["사용성", "프로토타입", "디자인시스템", "전환율", "리서치"],
        "good_signals": ["실사용성 개선 사례", "디자인시스템 구축", "사용자 인터뷰"],
    },
    "research": {
        "label": "연구개발(R&D)",
        "competencies": ["전공 전문성", "실험설계", "논리적 문제해결", "논문/특허"],
        "hr_focus": ["전문 깊이", "끈기", "재현성/정직성", "실용화 연결"],
        "tone": "정밀하고 논리적, 근거·데이터 중심",
        "structure": ["연구 문제", "가설/방법", "실험·결과", "의의/한계", "발전 방향"],
        "keywords": ["실험설계", "재현성", "논문", "특허", "성능 개선"],
        "good_signals": ["논문/학회 발표", "연구 인턴", "특허", "실험 자동화"],
    },
    "operations": {
        "label": "생산/품질/운영",
        "competencies": ["공정 이해", "품질관리(QC/QA)", "개선(Lean/6시그마)", "안전"],
        "hr_focus": ["현장 감각", "개선 마인드", "책임감", "협업"],
        "tone": "실무적·구체적, 개선 수치 강조",
        "structure": ["현장 문제", "원인 분석", "개선 실행", "성과(불량률/생산성)", "협업"],
        "keywords": ["불량률", "생산성", "공정개선", "표준화", "안전"],
        "good_signals": ["공정/품질 개선 성과", "현장 실습", "6시그마/QC 자격"],
    },
}

ALIASES = {
    "서버": "backend", "server": "backend", "be": "backend", "백엔드": "backend",
    "프론트": "frontend", "fe": "frontend", "web": "frontend", "프론트엔드": "frontend",
    "데이터분석": "data", "데이터사이언스": "data", "ds": "data", "ml": "data", "ai": "data",
    "프로덕트": "pm", "po": "pm", "product": "pm", "기획": "pm",
    "마케터": "marketing", "growth": "marketing", "그로스": "marketing",
    "세일즈": "sales", "영업": "sales",
    "인사": "hr", "피플": "hr", "people": "hr",
    "회계": "finance", "재무": "finance", "accounting": "finance",
    "ux": "design", "ui": "design", "디자이너": "design",
    "연구": "research", "rnd": "research", "r&d": "research",
    "생산": "operations", "품질": "operations", "qa": "operations", "qc": "operations",
}


def normalize_job_key(job_key: str) -> str:
    """사용자가 넣은 직무 문자열을 표준 key 로 변환."""
    if not job_key:
        return "general"
    k = job_key.strip().lower()
    if k in JOB_PROFILES:
        return k
    if k in ALIASES:
        return ALIASES[k]
    for alias, target in ALIASES.items():
        if alias in k:
            return target
    return "general"


def get_job_profile(job_key: str) -> dict:
    key = normalize_job_key(job_key)
    profile = dict(JOB_PROFILES.get(key, JOB_PROFILES["general"]))
    profile["key"] = key
    return profile


def get_region_style(region: str) -> dict:
    r = (region or "KR").upper()
    return REGION_STYLE.get(r, REGION_STYLE["KR"])


def list_job_keys():
    return list(JOB_PROFILES.keys())


# =============================================================================
# 4. Gemini 클라이언트 (신형 SDK 우선, 없으면 구형 SDK로 자동 폴백)
#    - generate()             : 일반 텍스트 생성
#    - generate_with_search() : Google 검색 그라운딩 생성 (회사 리서치용)
# =============================================================================
class GeminiClient:
    """Gemini 텍스트 생성용 최소 래퍼."""

    def __init__(self, api_key: str = None, model_name: str = None):
        self.api_key = api_key if api_key is not None else resolve_api_key()
        self.model_name = model_name or GEMINI_MODEL_NAME
        self._backend = None
        self._client = None
        self._model = None

        if not self.api_key:
            raise ValueError(
                "API 키가 비어 있습니다. [1. API 키 설정] 구역의 GOOGLE_AI_STUDIO_API_KEY 에 "
                "키를 넣거나 Colab Secrets(🔑)에 GOOGLE_AI_STUDIO_API_KEY 를 등록하세요."
            )
        self._init_backend()

    def _init_backend(self):
        try:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
            self._backend = "genai"
            return
        except Exception:
            pass
        try:
            import google.generativeai as genai_old
            genai_old.configure(api_key=self.api_key)
            self._model = genai_old.GenerativeModel(self.model_name)
            self._backend = "generativeai"
            return
        except Exception as exc:
            raise ImportError(
                "google-genai 또는 google-generativeai 패키지가 필요합니다.\n"
                "  pip install google-genai\n"
                f"원본 오류: {exc}"
            ) from exc

    def generate(self, prompt: str, generation_config: dict = None) -> str:
        cfg = generation_config or GENERATION_CONFIG
        if self._backend == "genai":
            return self._generate_new(prompt, cfg)
        return self._generate_old(prompt, cfg)

    def generate_with_search(self, prompt: str, generation_config: dict = None) -> str:
        """Google 검색 그라운딩을 켜고 생성한다 (지원 회사 리서치용).

        신형 SDK(google-genai)에서만 검색이 동작하며, 실패하거나 구형 SDK인
        경우 일반 생성으로 폴백한다(이때 모델에게 '모르면 지어내지 말 것'을
        프롬프트로 강제하고 있어야 함).
        """
        cfg = generation_config or GENERATION_CONFIG
        if self._backend == "genai":
            try:
                from google.genai import types
                gen_cfg = types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=cfg.get("max_output_tokens", 2048),
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                )
                resp = self._client.models.generate_content(
                    model=self.model_name, contents=prompt, config=gen_cfg,
                )
                text = (getattr(resp, "text", "") or "").strip()
                if text:
                    return text
            except Exception:
                pass
        return self.generate(prompt, cfg)

    def _generate_new(self, prompt: str, cfg: dict) -> str:
        from google.genai import types
        gen_cfg = types.GenerateContentConfig(
            temperature=cfg.get("temperature", 0.25),
            top_p=cfg.get("top_p", 0.9),
            top_k=cfg.get("top_k", 40),
            max_output_tokens=cfg.get("max_output_tokens", 4096),
        )
        resp = self._client.models.generate_content(
            model=self.model_name, contents=prompt, config=gen_cfg,
        )
        return (getattr(resp, "text", "") or "").strip()

    def _generate_old(self, prompt: str, cfg: dict) -> str:
        resp = self._model.generate_content(
            prompt,
            generation_config={
                "temperature": cfg.get("temperature", 0.25),
                "top_p": cfg.get("top_p", 0.9),
                "top_k": cfg.get("top_k", 40),
                "max_output_tokens": cfg.get("max_output_tokens", 4096),
            },
        )
        return (getattr(resp, "text", "") or "").strip()


# =============================================================================
# 5. 모범 자소서(레퍼런스) 저장소 — 문체 참조 전용, 내용 복사 금지
# =============================================================================
TARGET_RATIO = {"KR": 0.75, "US": 0.25}   # 한국형 750 : 미국형 250 목표 비율


class ReferenceStore:
    """모범 자소서 저장소. loader 함수로 DB에서 lazy-load 하거나 add() 로 직접 추가."""

    def __init__(self, loader=None):
        self._items = []
        self._loader = loader
        self._loaded = False

    def add(self, example: ReferenceExample):
        example.job_key = normalize_job_key(example.job_key)
        example.region = (example.region or "KR").upper()
        self._items.append(example)

    def add_many(self, examples):
        for e in examples:
            self.add(e)

    def _ensure_loaded(self):
        if not self._loaded and self._loader is not None:
            self.add_many(self._loader() or [])
        self._loaded = True

    def __len__(self):
        self._ensure_loaded()
        return len(self._items)

    def select_examples(self, job_key, region, k=3, max_chars_each=1400):
        self._ensure_loaded()
        job_key = normalize_job_key(job_key)
        region = (region or "KR").upper()

        same_job_region = [e for e in self._items if e.job_key == job_key and e.region == region]
        same_region = [e for e in self._items if e.region == region and e not in same_job_region]
        others = [e for e in self._items if e not in same_job_region and e not in same_region]

        ordered = []
        for bucket in (same_job_region, same_region, others):
            for e in bucket:
                if len(ordered) >= k:
                    break
                ordered.append(e)
            if len(ordered) >= k:
                break

        trimmed = []
        for e in ordered:
            text = e.text.strip()
            if max_chars_each and len(text) > max_chars_each:
                text = text[:max_chars_each] + " …(이하 생략)"
            trimmed.append(ReferenceExample(region=e.region, job_key=e.job_key, text=text,
                                            source=e.source, tags=e.tags))
        return trimmed

    def distribution(self):
        self._ensure_loaded()
        dist = {"KR": 0, "US": 0, "total": 0}
        for e in self._items:
            dist[e.region] = dist.get(e.region, 0) + 1
            dist["total"] += 1
        return dist

    def balance_report(self) -> str:
        dist = self.distribution()
        total = dist["total"] or 1
        kr_ratio = dist.get("KR", 0) / total
        us_ratio = dist.get("US", 0) / total
        return (
            f"[레퍼런스 분포] 총 {dist['total']}건 (KR {dist.get('KR', 0)} / US {dist.get('US', 0)})\n"
            f" - 현재 비율  KR {kr_ratio:.0%} : US {us_ratio:.0%}\n"
            f" - 목표 비율  KR {TARGET_RATIO['KR']:.0%} : US {TARGET_RATIO['US']:.0%}"
        )


# =============================================================================
# 6. 프롬프트 조립 — ★ 핵심: '완성형 자소서'를 대신 써 주는 집필 프롬프트
# =============================================================================
ANTI_HALLUCINATION_RULES = """\
[절대 규칙 — 반드시 지킬 것]
1) (제1원칙) 아래 '사용자 사실 원장'에 있는 내용만 지원자의 사실로 사용한다.
   - 원장에 없는 수치, 경험, 자격, 기술, 직함을 새로 만들어내지 말 것.
   - 어떤 사실도 추측/과장/윤색하지 말 것. 특히 숫자는 원장에 있는 값만 사용.
   - '스타일 예시'의 문장/사실을 절대 그대로 가져오지 말 것(문체만 참고).
2) (제2원칙) 한국어 맞춤법·띄어쓰기·문맥을 자연스럽게. 자소서 특유의
   담백하고 신뢰감 있는 문어체를 사용하고, 문장 간 논리가 매끄럽게 이어질 것.
3) 진부한 상투어(예: "저는 어릴 적부터", "귀사의 무궁한 발전")는 지양하고,
   구체적 사실과 성과 중심으로 서술할 것.
"""

WRITING_STYLE_RULES = """\
[서술 방식 규칙 — 나열 금지, 깊이 있게]
1) 경험 나열 금지: 문항과 회사에 가장 잘 맞는 경험 1개(많아야 2개)만 골라
   깊게 서술할 것. 여러 경험을 얕게 훑으며 나열하는 글은 실패작이다.
   - 선택한 경험은 무엇을(What), 언제(When), 어디서(Where), 어떻게(How)
     했는지, 그래서 어떤 결과와 성과가 나왔는지가 하나의 이야기로
     이어지도록 쓸 것.
2) 돌림노래 금지: 같은 강점·성과·표현을 말만 바꿔 반복하지 말 것.
   한 번 말한 내용은 다시 등장시키지 않는다.
3) 어미와 시점:
   - 어미는 '~했습니다/~입니다'체로 일관되게 통일할 것.
   - 단, 연속된 문장이 똑같은 어미로 끝나 단조로워지지 않도록 문장 구조와
     길이를 다양화할 것. (예: 명사절 활용, 쉼표로 절 연결, 문장 길이 변주)
   - '~하는 전문가이다', '~한 사람이다' 같은 제3자 관찰 시점의 단정 표현 금지.
     글 전체가 지원자 본인이 직접 말하는 1인칭 시점이어야 한다.
   - '~을 했습니다. ~을 했습니다. ~을 했습니다.' 식의 행동 보고 나열체 금지.
     행동 사이의 고민, 판단, 이유가 문장으로 드러나야 한다.
"""


def build_fact_sheet(user: UserProfile) -> str:
    """UserProfile 을 '사실 원장' 텍스트로 직렬화 (자소서 내용의 유일한 근거)."""
    data = user.to_dict()
    lines = []
    label = {
        "name": "이름", "target_company": "지원회사", "target_job": "지원직무",
        "education": "학력", "experiences": "경력/인턴", "projects": "프로젝트",
        "skills": "보유역량/기술", "certifications": "자격증", "awards": "수상",
        "activities": "대외활동/동아리", "achievements": "정량성과",
        "strengths": "강점/성향", "motivation": "지원동기 메모",
        "career_goal": "입사후 포부", "extra_notes": "기타 메모",
    }
    for key, kor in label.items():
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, list):
            lines.append(f"- {kor}:")
            for item in val:
                if isinstance(item, dict):
                    lines.append(f"    · {', '.join(f'{k}: {v}' for k, v in item.items())}")
                else:
                    lines.append(f"    · {item}")
        else:
            lines.append(f"- {kor}: {val}")
    return "\n".join(lines) if lines else "(제공된 사실 데이터 없음)"


def build_style_examples_block(examples) -> str:
    if not examples:
        return "(스타일 참고용 예시 없음 — 아래 직무 스타일 가이드만 따르세요.)"
    blocks = [f"[스타일 예시 {i} | {ex.region} | {ex.job_key}]\n{ex.text}" for i, ex in enumerate(examples, 1)]
    return "\n\n".join(blocks)


def build_company_research_prompt(company_name: str) -> str:
    """지원 회사의 가치/지향점 리서치 프롬프트 (Google 검색 그라운딩과 함께 사용)."""
    return f"""\
당신은 기업 리서처입니다. Google 검색을 활용해 '{company_name}' 회사에 대해
아래 항목을 조사하고 간결하게 정리하세요. (한국 기업일 가능성이 높습니다)

1) 미션/비전/핵심가치 (공식 홈페이지·채용 페이지 표현 위주)
2) 인재상 (공개된 것이 있다면)
3) 최근 1~2년의 주력 사업/전략 방향
4) 조직문화 키워드

[규칙]
- 각 항목 2~3줄 이내로 간결하게.
- 검색으로 확인되지 않는 항목은 "확인 불가"로만 표시하고 절대 지어내지 말 것.
- 요약만 출력(사족 없이).
"""


def build_company_block(company_research: str) -> str:
    """생성 프롬프트에 넣을 '회사 가치 은은하게 반영' 지시 블록."""
    if not (company_research or "").strip():
        return "(회사 리서치 정보 없음 — 이 항목은 무시하고 직무 중심으로 작성)"
    return f"""{company_research}

[회사 가치 반영 규칙 — 반드시 지킬 것]
- 위 리서치의 회사 가치·지향점을 직접 인용하거나 나열하지 말 것.
  ("귀사의 핵심가치인 OO처럼", "귀사의 인재상에 부합하는" 등 노골적 언급 금지)
- 대신 ① 어떤 경험을 고를지, ② 그 경험에서 무엇을 강조할지, ③ 포부의 방향을
  회사가 가는 방향과 자연스럽게 맞닿게 하는 수준으로만 은은하게 스며들게 할 것.
- 읽는 인사담당자가 '이 지원자, 우리 회사를 제대로 이해하고 있네'라고
  느끼되, 회사 소개를 베꼈다는 인상은 받지 않아야 한다."""


def build_generation_prompt(user, job_key, region, question, examples,
                            max_chars=1000, tone="", company_research=""):
    """완성형 자소서 집필 프롬프트 — 이 서비스의 심장."""
    profile = get_job_profile(job_key)
    region_style = get_region_style(region)
    fact_sheet = build_fact_sheet(user)
    style_block = build_style_examples_block(examples)
    company_block = build_company_block(company_research)
    tone_line = tone.strip() or profile["tone"]
    length_line = (
        f"- 분량: 공백 포함 약 {max_chars}자. 제한에 최대한 가깝게 충분히 채울 것(짧게 끝내지 말 것)."
        if max_chars else "- 분량: 문항에 적절한 완결된 길이로 충분히 작성."
    )
    question_line = question.strip() or "자유 형식의 자기소개서(핵심 강점과 지원동기 중심)"

    return f"""\
당신은 지원자를 대신해 '그대로 제출해도 좋은 완성형 {region_style['label']}'를
집필하는 전문 자기소개서 작가입니다. 아래 사실 데이터를 재료로, 채용 담당자를
설득하는 완결된 글 한 편을 완성하는 것이 당신의 임무입니다.

{ANTI_HALLUCINATION_RULES}

{WRITING_STYLE_RULES}

[집필 지침 — 이 글은 '초안'이 아니라 '완성본'이다]
1) 두괄식: 첫 1~2문장에서 글의 핵심 메시지(강점과 직무 적합성)를 제시할 것.
2) 본문은 위 서술 방식 규칙에 따라, 선택한 핵심 경험 하나를 What/When/Where/How
   와 결과·성과가 이어지는 완결된 이야기로 깊게 풀어 쓸 것.
   - '사실'은 원장의 것만 사용하되, 문장·서사·연결·표현은 전문 작가 수준으로
     풍부하게 발전시킬 것. (사실 추가 금지 ≠ 건조한 나열)
3) 마지막 문단은 지원 회사·직무와의 연결과 구체적인 기여 방향으로 마무리할 것.
4) "[보완필요: ...]" 표시는 문항이 반드시 요구하는 내용인데 원장에 근거가 전혀
   없을 때만 최소한으로 사용할 것. 원장의 사실로 채울 수 있으면 사용하지 말 것.

[지원 직무 프로필]
- 직무: {profile['label']} (key={profile['key']})
- 이 직무에서 인사팀이 중점적으로 보는 것: {', '.join(profile['hr_focus'])}
- 핵심 역량 키워드: {', '.join(profile['competencies'])}
- 권장 문체/톤: {tone_line}
- 권장 구성 흐름: {' → '.join(profile['structure'])}
- 자연스럽게 녹이면 좋은 키워드(단, 사실과 무관하면 억지로 넣지 말 것):
  {', '.join(profile['keywords'])}

[지역(문화) 스타일 가이드]
{region_style['guidance']}

[지원 회사 리서치 — 은은하게만 반영할 것]
{company_block}

[사용자 사실 원장 — 지원자 사실의 유일한 출처]
{fact_sheet}

[문체 참고용 스타일 예시 — 내용 복붙 금지, 전개/톤만 참고]
{style_block}

[작성 요청]
- 자소서 문항: {question_line}
{length_line}
- 위 '권장 구성 흐름'을 기본 골격으로 하되, 문항 성격에 맞게 자연스럽게 조정.
- 한국형이라면 소제목(예: [데이터로 리스크를 읽는 힘])을 활용해도 좋음.

[출력 형식]
- 완성된 자소서 본문만 출력(머리말/설명/사족 없이).
"""


def build_verification_prompt(generated_text, user, company_research=""):
    fact_sheet = build_fact_sheet(user)
    company_note = ""
    if (company_research or "").strip():
        company_note = f"""
[참고 — 회사 공개 정보(검색 리서치 결과)]
{company_research}
- 위 회사 정보의 범위 안에 있는 '회사에 대한' 서술은 문제 삼지 않는다.
  (단, 지원자 '본인의 경험/성과'는 반드시 사실 원장에 근거해야 한다)"""
    return f"""\
당신은 자기소개서 팩트체커입니다. 아래 '사용자 사실 원장'에 근거가 없는
문장/주장(지어낸 수치·경험·자격·기술 등)을 찾아내세요.

[사용자 사실 원장 — 지원자 사실의 유일한 출처]
{fact_sheet}
{company_note}

[검사할 자기소개서]
{generated_text}

[판정 규칙]
- 원장으로 뒷받침되지 않는 '지원자 본인에 대한' 구체적 사실 주장만 문제 삼는다.
- 일반적 다짐/포부/의지 표현(구체 사실 아님)이나 "[보완필요:...]" 표시는 문제 아님.
- 원장 내용을 자연스럽게 바꿔 쓴 것은 문제 아님(의미가 같으면 OK).

[출력 — 반드시 아래 JSON 만 출력]
{{
  "grounded": true 또는 false,
  "unsupported_claims": ["문제 문장 1", "문제 문장 2"],
  "notes": "간단한 총평(한 문장)"
}}
"""


def build_correction_prompt(generated_text, user, unsupported_claims, company_research=""):
    fact_sheet = build_fact_sheet(user)
    claims = "\n".join(f"- {c}" for c in unsupported_claims) or "- (없음)"
    company_note = ""
    if (company_research or "").strip():
        company_note = f"""
[참고 — 회사 공개 정보(검색 리서치 결과), 이 범위의 회사 서술은 유지 가능]
{company_research}"""
    return f"""\
아래 자기소개서에서 '근거 없는 문장'들을 제거하거나, 사실 원장에 맞게
고쳐 쓰세요. 새로운 사실을 추가하지 말고, 문맥이 매끄럽도록 다듬으세요.

[사용자 사실 원장 — 지원자 사실의 유일한 출처]
{fact_sheet}
{company_note}

[제거/수정 대상(근거 없는 문장)]
{claims}

[원본 자기소개서]
{generated_text}

[요구사항]
- 근거 없는 사실은 삭제하거나, 원장에 있는 사실로 대체.
- 삭제로 빈 곳이 생기면 원장의 다른 사실로 자연스럽게 연결.
- 정 채울 근거가 없으면 "[보완필요: ...]"로 표시.
- 맞춤법·문맥·자소서 문체를 자연스럽게 유지.
- 완성된 본문만 출력.
"""


def build_polish_prompt(generated_text, user, max_chars=1000):
    """최종 다듬기(polish) — 제출 직전 완성도 끌어올리기 (제2원칙 강화)."""
    fact_sheet = build_fact_sheet(user)
    length_line = (
        f"- 분량: 공백 포함 약 {max_chars}자 수준을 유지할 것."
        if max_chars else "- 분량: 현재 수준을 유지할 것."
    )
    return f"""\
당신은 자기소개서 전문 교정가입니다. 아래 자기소개서를 '제출 직전 최종본'
수준으로 다듬으세요.

[사용자 사실 원장 — 유일한 사실 출처]
{fact_sheet}

[다듬을 자기소개서]
{generated_text}

[교정 지침]
- 새로운 사실을 추가하지 말 것(표현·흐름만 개선).
- 맞춤법·띄어쓰기·조사 오류, 어색한 번역투 교정.
- 어미 점검(중요):
  · '~했습니다/~입니다'체로 통일하되, 같은 어미가 3문장 이상 연속되면
    문장 구조를 바꿔 리듬을 살릴 것.
  · '~하는 전문가이다', '~한 사람이다' 같은 3인칭 관찰자식 표현이 남아
    있으면 1인칭 서술로 교정할 것.
- 반복 점검(중요): 같은 강점·성과·표현이 돌림노래처럼 반복되면 가장 강한
  한 곳만 남기고 정리할 것. 행동 보고 나열체('~했습니다'의 연속)는 행동
  사이의 고민과 판단이 드러나는 문장으로 연결할 것.
- 문단 연결과 문장 호흡을 매끄럽게, 두괄식 구조는 유지·강화.
- "[보완필요: ...]" 표시가 있다면 그대로 유지.
{length_line}
- 다듬어진 본문만 출력.
"""


def build_writing_guide_prompt(cover_letter_text, question, user, job_key):
    """'이 자소서를 내 것으로 만드는 가이드' — 사용자가 완성본을 소화하도록 돕는다."""
    profile = get_job_profile(job_key)
    fact_sheet = build_fact_sheet(user)
    question_line = question.strip() or "자유 형식의 자기소개서"
    return f"""\
당신은 자기소개서 코치입니다. 아래는 지원자의 사실 데이터로 AI가 대신 작성한
완성형 자기소개서입니다. 지원자가 이 글을 '자기 것'으로 소화해 최종 제출본을
완성하고, 면접까지 대비할 수 있도록 돕는 가이드를 작성하세요.

[지원 직무]
- {profile['label']} — 인사팀 평가 포인트: {', '.join(profile['hr_focus'])}

[사용자 사실 원장]
{fact_sheet}

[자소서 문항]
{question_line}

[완성된 자기소개서]
{cover_letter_text}

[작성 규칙]
- 새로운 사실을 지어내라고 제안하지 말 것.
- 이 글을 '고쳐야 할 초안'이 아니라 '완성본'으로 대하되, 지원자 본인만이
  더할 수 있는 것(디테일, 감정, 당시의 판단)을 짚어줄 것.

[출력 형식]
1) 이 자소서의 전략 (2~3문장): 어떤 강점을 어떤 순서로 배치했고, 왜 이 구성이
   해당 직무 인사담당자에게 효과적인지.
2) 문단별 해설: 각 문단의 역할(후킹/근거/직무연결/포부)과 활용된 지원자 경험.
3) 직접 손보면 더 좋아지는 포인트: "[보완필요]" 표시가 있다면 채우는 법,
   지원자 본인만 아는 디테일(당시의 판단, 감정, 대화, 시행착오)을 추가하면
   좋은 위치와 방법.
4) 제출 전 최종 체크리스트 (5개 내외).
5) 이 자소서 기반 예상 면접 질문 3개와 답변 포인트.
"""


def build_action_plan_prompt(user, job_key):
    profile = get_job_profile(job_key)
    fact_sheet = build_fact_sheet(user)
    return f"""\
당신은 채용 담당자(HR) 관점을 잘 아는 커리어 멘토입니다.
'{profile['label']}' 직무 지원자를 위해, 현재 이력을 진단하고
'더 좋은 인재/자소서가 되기 위한 액션플랜'을 제안하세요.

[이 직무에서 HR이 중점적으로 보는 것]
{', '.join(profile['hr_focus'])}

[강한 인상을 주는 활동/경험(참고)]
{', '.join(profile['good_signals'])}

[핵심 역량]
{', '.join(profile['competencies'])}

[사용자 사실 원장 — 현재 보유 이력]
{fact_sheet}

[작성 규칙]
- 먼저, 원장에 '이미 있는 강점'과 '비어 있는 부분(gap)'을 구분해서 진단.
- 액션플랜은 '앞으로 하면 좋을 것'으로 명확히 미래형 제안(사실로 단정 금지).
- 각 액션은 (무엇을 / 왜 HR이 좋아하는지 / 자소서에 어떻게 쓸지) 포함.
- 실행 난이도/기간 감각(단기 1~3개월 / 중기 3~6개월)을 함께 제시.

[출력 형식]
1) 현재 강점 진단
2) 부족한 부분(HR 관점 gap)
3) 액션플랜 (우선순위 순, 각 항목: 활동 / HR이 좋아하는 이유 / 자소서 활용법 / 기간)
4) 한 줄 요약
"""


def safe_parse_json(text: str) -> dict:
    """모델 응답에서 JSON 블록만 안전하게 파싱(코드펜스 섞여도 처리)."""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    try:
        return _json.loads(t)
    except Exception:
        return {}


# =============================================================================
# 7. 생성 파이프라인:
#    회사 리서치 → 집필 → 근거 검증(환각 탐지) → 교정 → 최종 다듬기
# =============================================================================
def research_company(client, company_name: str) -> str:
    """Google 검색 그라운딩으로 지원 회사의 가치/지향점을 조사해 요약."""
    name = (company_name or "").strip()
    if not name:
        return ""
    prompt = build_company_research_prompt(name)
    try:
        return client.generate_with_search(prompt)
    except Exception:
        return ""


def generate_draft(client, user, job_key, region, question, examples,
                   max_chars=1000, tone="", company_research=""):
    prompt = build_generation_prompt(user, job_key, region, question, examples,
                                     max_chars, tone, company_research)
    return client.generate(prompt, GENERATION_CONFIG)


def verify_grounding(client, generated_text, user, company_research=""):
    prompt = build_verification_prompt(generated_text, user, company_research)
    raw = client.generate(prompt, VERIFICATION_CONFIG)
    parsed = safe_parse_json(raw)
    if not parsed:
        return GroundingReport(grounded=False, unsupported_claims=[],
                               notes="검증 응답 파싱 실패 — 사람이 직접 사실 확인 권장.")
    return GroundingReport(
        grounded=bool(parsed.get("grounded", False)),
        unsupported_claims=list(parsed.get("unsupported_claims", []) or []),
        notes=str(parsed.get("notes", "")),
    )


def correct_draft(client, generated_text, user, unsupported_claims, company_research=""):
    prompt = build_correction_prompt(generated_text, user, unsupported_claims, company_research)
    return client.generate(prompt, GENERATION_CONFIG)


def polish_draft(client, generated_text, user, max_chars=1000):
    """어미·반복·맞춤법·문맥을 제출본 수준으로 다듬는 최종 패스."""
    prompt = build_polish_prompt(generated_text, user, max_chars)
    return client.generate(prompt, GENERATION_CONFIG)


def generate_grounded_cover_letter(client, user, job_key, region, question,
                                   examples, max_chars=1000, tone="",
                                   company_research="", max_iterations=2, polish=True):
    """(완성형 자소서 본문, 근거검증 리포트) 반환.

    생성 → 검증/교정 반복 → 최종 다듬기 = 제출 가능한 완성형 자소서.
    """
    if user is None or user.is_empty():
        raise ValueError("사용자 데이터(UserProfile)가 비어 있습니다. 최소 1개 이상의 사실을 입력하세요.")

    text = generate_draft(client, user, job_key, region, question, examples,
                          max_chars, tone, company_research)
    report = verify_grounding(client, text, user, company_research)

    iterations = 0
    while (not report.grounded) and report.unsupported_claims and iterations < max_iterations:
        text = correct_draft(client, text, user, report.unsupported_claims, company_research)
        report = verify_grounding(client, text, user, company_research)
        iterations += 1

    if polish:
        text = polish_draft(client, text, user, max_chars)

    report.notes = (report.notes + f" (교정 반복 {iterations}회)").strip()
    return text, report


# =============================================================================
# 7-1. '이 자소서를 내 것으로 만드는 가이드'
# =============================================================================
def build_writing_guide(client, cover_letter_text, question, user, job_key):
    prompt = build_writing_guide_prompt(cover_letter_text, question, user, job_key)
    return client.generate(prompt, GENERATION_CONFIG)


# =============================================================================
# 7-2. (부가) HR 관점 액션플랜
# =============================================================================
def suggest_action_plan(client, user, job_key):
    prompt = build_action_plan_prompt(user, job_key)
    return client.generate(prompt, GENERATION_CONFIG)


# =============================================================================
# 7-3. 전체 오케스트레이션 — 문항별 완성형 자소서 일괄 생성
# =============================================================================
def generate_application(client, user, job_key="general", region="KR",
                         questions=None, tone="", store=None,
                         num_style_examples=3, use_company_research=True,
                         include_writing_guide=True, include_action_plan=True,
                         max_grounding_iterations=2, polish=True):
    """지원서 전체(여러 문항)의 완성형 자소서를 생성한다.

    questions            : 문항 목록. "문자열" 그대로 넣거나
                           {"question": str, "max_chars": int} 딕셔너리로 넣거나
                           둘 다 가능. 비어 있으면 자유 형식 1건을 생성한다.
    use_company_research : True 면 user.target_company 를 Google 검색으로 조사해
                           회사 가치/지향점을 글에 은은하게 반영한다.
    """
    if not questions:
        questions = [{"question": "", "max_chars": 1000}]

    # 0) 지원 회사 리서치 (문항 전체에 공통 반영)
    company_research = ""
    if use_company_research and (user.target_company or "").strip():
        company_research = research_company(client, user.target_company)

    # 1) 문체 예시 선별 (모든 문항에 공통 사용)
    examples = []
    if store is not None and len(store) > 0:
        examples = store.select_examples(job_key, region, k=num_style_examples)

    answers = []
    for q in questions:
        # 문항을 문자열("...")로 넣거나 {"question": "...", "max_chars": N}
        # 딕셔너리로 넣거나 둘 다 허용한다.
        if isinstance(q, str):
            question_text = q
            max_chars = 1000
        else:
            question_text = str(q.get("question", "") or "")
            max_chars = int(q.get("max_chars", 1000) or 0)

        cover_letter, grounding = generate_grounded_cover_letter(
            client, user, job_key, region, question_text, examples,
            max_chars=max_chars, tone=tone, company_research=company_research,
            max_iterations=max_grounding_iterations, polish=polish,
        )

        answer = AnswerResult(
            question=question_text or "(자유 형식)",
            cover_letter=cover_letter,
            grounding=grounding,
        )
        if include_writing_guide:
            answer.writing_guide = build_writing_guide(
                client, cover_letter, question_text, user, job_key)
        answers.append(answer)

    result = ApplicationResult(answers=answers, company_research=company_research)

    if include_action_plan:
        result.action_plan = suggest_action_plan(client, user, job_key)

    profile = get_job_profile(job_key)
    result.meta = {
        "job_key": normalize_job_key(job_key),
        "job_label": profile["label"],
        "region": (region or "KR").upper(),
        "num_questions": len(answers),
        "num_style_examples": len(examples),
        "company_research_used": bool(company_research),
        "all_grounded": all(a.grounding.grounded for a in answers),
    }
    return result


def format_application(result: ApplicationResult) -> str:
    """결과 출력 — 완성형 자소서가 주인공이 되도록 구성."""
    lines = []
    lines.append("=" * 70)
    lines.append(
        f" AI 자기소개서 생성 결과 | 직무: {result.meta.get('job_label')}"
        f" / 지역: {result.meta.get('region')}"
        f" | 문항 {result.meta.get('num_questions')}개"
    )
    if result.meta.get("company_research_used"):
        lines.append("  ▸ 지원 회사 리서치가 글의 방향에 은은하게 반영되었습니다.")
    lines.append("=" * 70)

    for i, ans in enumerate(result.answers, 1):
        lines.append(f"\n【문항 {i}】 {ans.question}")
        lines.append("-" * 70)
        lines.append(ans.cover_letter)
        lines.append("")

        g = ans.grounding
        if g.grounded:
            lines.append("  ▸ 근거 검증: 통과 ✅ — 사용자 데이터에 없는 내용이 발견되지 않았습니다.")
        else:
            lines.append("  ▸ 근거 검증: 확인 필요 ⚠️")
            for c in g.unsupported_claims:
                lines.append(f"      · {c}")
            if g.notes:
                lines.append(f"      ({g.notes})")

        if ans.writing_guide:
            lines.append("")
            lines.append("  ── 이 자소서를 내 것으로 만드는 가이드 ──")
            lines.append(ans.writing_guide)

    if result.company_research:
        lines.append("\n" + "=" * 70)
        lines.append("■ (참고) 지원 회사 리서치 요약 — 글에는 은은하게만 반영됨")
        lines.append("-" * 70)
        lines.append(result.company_research)

    if result.action_plan:
        lines.append("\n" + "=" * 70)
        lines.append("■ (부가) HR 관점 커리어 액션플랜 — 다음 지원까지 준비하면 좋은 것")
        lines.append("-" * 70)
        lines.append(result.action_plan)

    lines.append("=" * 70)
    return "\n".join(lines)


# =============================================================================
# 8. [레퍼런스 DB] 모범 자소서 연결 (선택 사항, 비워둬도 정상 동작)
# =============================================================================
#  실제 모범 자소서(한국형 750 + 미국형 250)는 별도 DB/코드에서 불러온다면
#  아래에 loader 함수를 만들어 주입하세요. 예:
#
#    def load_examples_from_db():
#        rows = your_db.query("SELECT region, job_key, text, source FROM refs")
#        return [ReferenceExample(region=r.region, job_key=r.job_key,
#                                 text=r.text, source=r.source) for r in rows]
#    reference_store = ReferenceStore(loader=load_examples_from_db)
#
#  지금은 비워둡니다(직무 스타일 가이드만으로 자소서가 생성됩니다).
# =============================================================================
reference_store = ReferenceStore()

# ↓↓↓ 여기에 DB에서 불러온 모범 자소서를 넣으세요 (지금은 공란) ↓↓↓
# reference_store.add(ReferenceExample(region="KR", job_key="backend", text="", source=""))
# reference_store.add(ReferenceExample(region="US", job_key="data",    text="", source=""))
# ↑↑↑ 공란 ↑↑↑


# =============================================================================
# 9. [사용자 데이터 입력] ★★★ 여기에 본인의 '사실'만 입력하세요 ★★★
# =============================================================================
#  제1원칙(환각 방지): 여기 없는 내용은 자소서에 절대 등장하지 않습니다.
#  비워 둔 항목은 자소서에서 다루지 않습니다.
#  리스트 항목은 "문자열" 또는 {"키": "값"} 형태 모두 가능합니다.
#
#  ※ target_company 에 회사명을 넣으면 Google 검색으로 그 회사의 가치/인재상을
#    조사해 글의 방향에 은은하게 반영합니다. (노골적 인용은 하지 않음)
# =============================================================================
user_profile = UserProfile(
    name="",
    target_company="",
    target_job="",

    education=[
        {"school": " ", "major": " ",
         "period": "", "gpa": ""},
    ],

    experiences=[
        {"company": "", "role": "",
         "period": "",
         "detail": ""},
    ],

    projects=[
        {"name": "LSTM 기반 코스피 변동성 예측 모델",
         "detail": ""},
    ],

    skills=[
        " ",
    ],

    certifications=[
        "",
    ],

    awards=[
        {"name": "", "year": ""},
    ],

    activities=[
        "",
    ],

    achievements=[
        "",
    ],

    strengths=[
        "",
    ],

    motivation="",

    career_goal="",

    extra_notes="",
)

# =============================================================================
# 10. [자소서 문항 입력] — 지원할 회사의 실제 문항을 그대로 넣으세요
# =============================================================================
#  여러 문항을 넣으면 문항별 완성형 자소서가 각각 생성됩니다.
#  비워 두면([]) 자유 형식 1건이 생성됩니다.
#  ─ 문항은 "문자열" 그대로 넣거나, {"question": "...", "max_chars": N}
#    딕셔너리로 넣거나 둘 다 가능합니다. (문자열이면 max_chars=1000 기본 적용)
# =============================================================================
QUESTIONS = [
""" """
]


# =============================================================================
# 11. [실행] 문항별 완성형 자소서 생성 + 가이드 + (부가) 액션플랜
#     직무(job_key)/지역(region) 등을 아래에서 조정하세요.
# =============================================================================
# =============================================================================
# 11. 공통 반환 모델 (Pydantic)
# =============================================================================
class AnalysisResponse(BaseModel):
    """공통 반환 모델 — API Endpoint 계약 envelope 의 Pydantic 판.

    성공: status="success", result=payload(ApplicationResult asdict).
    실패: status="error", message. 자소서 analyzer 는 vector 필드가 없다.
    소비자(tasks.py 등)는 r.status·r.result 속성 또는 r.model_dump() 로 접근한다.
    result 안에는 status 를 넣지 않는다(§3.6). schema_version 은 tasks.py 가 주입(§3.5).
    """
    status: str
    result: dict | None = None
    message: str | None = None


# =============================================================================
# 11-1. [실행] Main (엔트리포인트) — 공통 Pydantic 응답 반환
# =============================================================================
def main(
    client=None,
    user: UserProfile = None,
    job_key: str = "",
    region: str = "KR",
    questions: list = None,
    tone: str = "",
    store: ReferenceStore = None,
    num_style_examples: int = 3,
    use_company_research: bool = True,
    include_writing_guide: bool = True,
    include_action_plan: bool = True,
    max_grounding_iterations: int = 2,
    polish: bool = True,
) -> dict:
    """문항별 완성형 자소서 생성 파이프라인 엔트리포인트.

    API Endpoint 계약(§3.3 [1])의 공통 envelope 형식으로 결과를 반환한다.
    (자소서 생성은 계약 문서상 '범위 밖'이지만, 4종 분석기와 동일한 반환 형식으로
     정렬해 향후 백엔드 통합 시 혼선을 없앤다. schema_version 은 코드가 주입하되
     그 주입 위치는 tasks.py 이며 analyzer 파일에는 넣지 않는다 — §3.5.)

      성공: {"status": "success", "result": { ...ApplicationResult payload... }}
      실패: {"status": "error", "message": "..."}
    payload(result) 안에는 status·vector 를 넣지 않는다 (§3.6 #25·#26).
    출력 '내용' 은 기존과 100% 동일하며, 반환 '형식' 만 envelope 로 통일했다.
    """
    user = user if user is not None else user_profile
    questions = questions if questions is not None else QUESTIONS
    store = store if store is not None else reference_store

    try:
        if client is None:
            client = GeminiClient()   # 위 1번 구역의 API 키/모델(gemini-2.5-flash) 사용
        print(store.balance_report())
        result = generate_application(
            client=client,
            user=user,
            job_key=job_key,             # 예: "backend"/"data"/"marketing" (공란 시 general)
            region=region,               # "KR"(한국형) 또는 "US"(미국형)
            questions=questions,         # 위 10번 구역에서 입력한 문항들
            tone=tone,                   # 추가 톤 요청(비우면 직무 기본 톤)
            store=store,
            num_style_examples=num_style_examples,   # few-shot 문체 예시 개수
            use_company_research=use_company_research,  # 지원 회사 가치 은은하게 반영
            include_writing_guide=include_writing_guide,  # '내 것으로 만드는 가이드'
            include_action_plan=include_action_plan,      # (부가) HR 액션플랜
            max_grounding_iterations=max_grounding_iterations,  # 환각 검증→교정 반복
            polish=polish,               # 최종 문체 다듬기 패스(어미·반복 정리)
        )
    except Exception as e:
        resp = AnalysisResponse(status="error", message=str(e))
        print(resp.model_dump_json(indent=2, exclude_none=True))
        return resp

    # 사람이 읽는 CLI 출력은 그대로 유지 (출력 내용 불변)
    print(format_application(result))

    # ApplicationResult(dataclass) → payload dict 직렬화 (내용 불변)
    payload = asdict(result)
    return AnalysisResponse(status="success", result=payload)


if __name__ == "__main__":
    main()
