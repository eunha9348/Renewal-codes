# -*- coding: utf-8 -*-
"""
analysis_response.py
====================
분석·Export 5종 analyzer 의 공통 반환 모델 (API Endpoint 계약 envelope 의 Pydantic 판).

★ 성공(success)과 실패(error)를 서로 다른 모델로 명확히 분리한다.
   - 성공 응답은 result 를 담고 message 가 없다.
   - 실패 응답은 message 를 담고 result·vector 가 없다.
   - status 는 각 모델에 Literal 기본값으로 고정되어 호출부가 넘길 필요가 없고,
     실수로 뒤섞일 수 없다.

  성공(개별·종합) : VectorSuccessResponse  → { status:"success", result, vector? }
  성공(키워드·레쥬메·자소서) : SuccessResponse → { status:"success", result }
  실패(4종 공통) : ErrorResponse          → { status:"error", message }

[규약]
  - result(payload) 안에는 status·vector 를 넣지 않는다 (계약 §3.6 #25·#26)
  - result 최상위 키에 A_/B_/C_ 순서 접두사를 붙이지 않는다 (§3.4)
  - schema_version 은 analyzer 가 아니라 tasks.py(백엔드)가 주입한다 (§3.5)
  - vector 컬럼이 있는 타입(개별·종합)만 VectorSuccessResponse 를 쓴다 (§3.2)
    · 키워드·레쥬메·자소서 테이블엔 vector 컬럼 자체가 없으므로 vector 필드도 없다.

[소비 측 주의 — 계약 §3.3]
  반환 타입이 dict 가 아니라 Pydantic 모델이므로, tasks.py 등 소비 코드는
  isinstance(r, dict) / r["result"] 대신 아래처럼 접근한다.
      if r.status != "success":                 # 실패 → ErrorResponse.message
          fail(r.message)
      else:                                      # 성공 → (Vector)SuccessResponse
          save(r.result, getattr(r, "vector", None))
  또는 r = main(...).model_dump() 로 dict 변환 후 기존 로직을 유지한다.

  콘솔 출력은 r.model_dump_json(indent=2, exclude_none=True) 를 쓴다
  (Pydantic v2 는 한글을 이스케이프 없이 UTF-8 그대로 출력).
"""

from typing import Literal

from pydantic import BaseModel


# ══════════════════════════════════════════════
# 성공 응답 (success) — result 를 담는다
# ══════════════════════════════════════════════
class SuccessResponse(BaseModel):
    """성공 응답 — vector 가 없는 analyzer(키워드·레쥬메·자소서)용.

    status 는 "success" 로 고정. result 에 분석 payload 를 담는다.
    result 는 필수 — 성공인데 payload 가 없는 상태는 성립하지 않는다
    (실패라면 ErrorResponse.message 로 표현할 것). ErrorResponse.message 필수와 대칭.
    """
    status: Literal["success"] = "success"
    result: dict


class VectorSuccessResponse(SuccessResponse):
    """성공 응답 — vector 가 있는 analyzer(개별·종합)용.

    임베딩 벡터는 result 밖 별도 필드로만 존재한다(§3.2).
    vector 는 옵셔널 — 임베딩 호출이 실패해도(get_embedding→None) 분석 자체는
    성공일 수 있고, 계약도 vector 를 nullable 로 다룬다(tasks.py: r.get("vector")).
    즉 '성공엔 result 필수, vector 는 있으면 담고 없으면 None' 이 의도된 설계다.
    """
    vector: list[float] | None = None


# ══════════════════════════════════════════════
# 실패 응답 (error) — message 를 담는다 (result·vector 없음)
# ══════════════════════════════════════════════
class ErrorResponse(BaseModel):
    """실패 응답 — 4종 공통.

    status 는 "error" 로 고정. message 는 필수(실패 사유). result·vector 는 없다.
    """
    status: Literal["error"] = "error"
    message: str
