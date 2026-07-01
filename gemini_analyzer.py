"""
Gemini 분석 모듈 (무료 API)
- 재무 요약표, 사업보고서 본문, 공시목록을 받아
  강점/약점/최근 특이사항 등 한국어 종합 분석을 생성한다.
- 구글 AI Studio(aistudio.google.com/app/apikey)에서 무료 API 키 발급.
"""

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-2.5-flash"


def _fmt_won(v):
    if v is None:
        return "-"
    try:
        eok = v / 1e8  # 억 원 단위로 보기 쉽게
        return f"{eok:,.1f}억"
    except Exception:
        return str(v)


def build_financial_text(fin):
    """재무 데이터(dict)를 표 형태 텍스트로 변환."""
    if not fin:
        return "재무 데이터 없음"
    years = fin["years"]
    lines = [f"재무제표 기준: {fin['fs_div']} (연결=CFS, 별도=OFS), 단위 원"]
    header = "계정 | " + " | ".join(str(y) + "년" for y in years)
    lines.append(header)
    for name, vals in fin["rows"].items():
        lines.append(f"{name} | " + " | ".join(_fmt_won(v) for v in vals))
    return "\n".join(lines)


def build_disclosure_text(disclosures, limit=20):
    if not disclosures:
        return "최근 공시 없음"
    lines = []
    for d in disclosures[:limit]:
        lines.append(f"- {d.get('rcept_dt','')} : {d.get('report_nm','')}")
    return "\n".join(lines)


def build_overview_text(ov):
    """기업개황 JSON을 짧은 텍스트로."""
    if not ov:
        return "정보 없음"
    fields = [
        ("회사명", "corp_name"),
        ("대표자", "ceo_nm"),
        ("업종", "induty_code"),
        ("설립일", "est_dt"),
        ("상장시장", "corp_cls"),
        ("주소", "adres"),
        ("홈페이지", "hm_url"),
    ]
    lines = []
    for label, key in fields:
        val = ov.get(key)
        if val:
            lines.append(f"{label}: {val}")
    return "\n".join(lines) if lines else "정보 없음"


SYSTEM_PROMPT = (
    "당신은 한국 기업 공시 자료를 분석하는 재무 애널리스트입니다. "
    "제공된 DART 공시 데이터만 근거로 사용하고, 자료에 없는 내용은 추측하지 마세요. "
    "숫자는 억 원 단위로 알기 쉽게 설명하고, 전문 용어는 짧게 풀어 씁니다. "
    "반드시 한국어로, 결론을 먼저 제시한 뒤 근거를 붙입니다."
)

USER_TEMPLATE = """다음은 '{corp_name}'의 DART 공시 자료입니다.

[기업 개황]
{overview}

[재무제표 요약]
{financials}

[사업보고서 본문 발췌]
{business_text}

[최근 공시 목록]
{disclosures}

위 자료를 바탕으로 아래 형식의 마크다운 리포트를 작성하세요.

## 한눈에 보기
- 3~4문장으로 회사의 현재 상태를 요약(핵심 결론 먼저).

## 재무 흐름
- 매출, 영업이익, 순이익의 최근 3개년 추세와 그 의미.
- 재무 안정성(자산, 부채, 자본) 간단 평가.

## 강점
- 사업보고서, 재무에 근거한 강점 2~4가지.

## 약점 / 리스크
- 근거 있는 약점, 위험요인 2~4가지.

## 최근 특이사항
- 공시 목록에서 눈에 띄는 이벤트(증자, 배당, 계약, 소송 등) 정리.

## 종합 의견
- 균형 잡힌 마무리. 투자 권유는 하지 말고, 판단에 필요한 정보를 제공하는 톤으로.

주의: 자료가 부족한 항목은 '자료에 정보가 부족함'이라고 솔직히 적으세요.
"""


def analyze(
    api_key,
    corp_name,
    overview_text,
    financial_text,
    business_text,
    disclosure_text,
    model=DEFAULT_MODEL,
):
    client = genai.Client(api_key=api_key)
    user_msg = USER_TEMPLATE.format(
        corp_name=corp_name,
        overview=overview_text or "정보 없음",
        financials=financial_text or "정보 없음",
        business_text=(business_text or "정보 없음")[:12000],
        disclosures=disclosure_text or "정보 없음",
    )
    cfg_kwargs = dict(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=8192,
        temperature=0.4,
    )
    # gemini-2.5 계열은 '생각(thinking)'에 토큰을 소모해 답변이 잘릴 수 있으므로 끈다.
    if "2.5" in model:
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
    resp = client.models.generate_content(
        model=model,
        contents=user_msg,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    text = getattr(resp, "text", None)
    if text:
        return text
    parts = []
    for c in getattr(resp, "candidates", []) or []:
        content = getattr(c, "content", None)
        for p in getattr(content, "parts", []) or []:
            if getattr(p, "text", ""):
                parts.append(p.text)
    if parts:
        return "".join(parts)
    return "분석 결과를 생성하지 못했습니다. 잠시 후 다시 시도해주세요."
