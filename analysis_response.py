# -*- coding: utf-8 -*-
"""
analysis_response.py
====================
분석·Export 5종 analyzer 의 공통 반환 모델 (API Endpoint 계약 envelope 의 Pydantic 판).

5개 파일(individual / comprehensive / keyword_analysis / resume / cover_letter)의
main() 이 이 모듈의 모델을 import 해서 동일한 형식으로 반환한다.

  성공: status="success", result=payload, (vector: 개별·종합만)
  실패: status="error", message

[규약]
  - result(payload) 안에는 status·vector 를 넣지 않는다 (계약 §3.6 #25·#26)
  - result 최상위 키에 A_/B_/C_ 순서 접두사를 붙이지 않는다 (§3.4)
  - schema_version 은 analyzer 가 아니라 tasks.py(백엔드)가 주입한다 (§3.5)
  - vector 컬럼이 있는 타입(개별·종합)만 VectorAnalysisResponse 를 쓴다 (§3.2)
    · 키워드·레쥬메·자소서 테이블엔 vector 컬럼 자체가 없으므로 vector 필드도 없다.

[소비 측 주의 — 계약 §3.3]
  반환 타입이 dict 가 아니라 Pydantic 모델이므로, tasks.py 등 소비 코드는
  isinstance(r, dict) / r["result"] 대신 아래처럼 접근한다.
      if r.status != "success": ...
      payload, vector = r.result, getattr(r, "vector", None)
  또는 r = main(...).model_dump() 로 dict 변환 후 기존 로직을 유지한다.

  콘솔 출력은 r.model_dump_json(indent=2, exclude_none=True) 를 쓴다
  (Pydantic v2 는 한글을 이스케이프 없이 UTF-8 그대로 출력).
"""

from pydantic import BaseModel


class AnalysisResponse(BaseModel):
    """vector 가 없는 analyzer(키워드·레쥬메·자소서)용 공통 반환 모델.

    성공: status="success", result=payload.
    실패: status="error", message.
    """
    status: str                     # "success" | "error"
    result: dict | None = None      # 분석 payload (성공 시)
    message: str | None = None      # 실패 사유 (실패 시)


class VectorAnalysisResponse(AnalysisResponse):
    """vector 컬럼이 있는 analyzer(개별·종합)용 — 임베딩 벡터 필드 추가.

    vector 는 result 밖 별도 필드로만 존재한다(§3.2). 임베딩 실패 시 None.
    """
    vector: list[float] | None = None
