"""
DART 공시 자동 요약·분석 웹앱 (Streamlit)
실행: streamlit run app.py
"""

import pandas as pd
import streamlit as st

import dart_api as dart
import gemini_analyzer as ai
import export_utils as ex

st.set_page_config(page_title="DART 공시 분석기", page_icon="📊", layout="wide")

# ------------------------------------------------------------------ 사이드바
st.sidebar.title("⚙️ 설정")
st.sidebar.markdown(
    "1. **DART 인증키**: [발급받기](https://opendart.fss.or.kr/) (무료)\n"
    "2. **Gemini API 키**: [발급받기](https://aistudio.google.com/app/apikey) (무료, 카드 불필요)"
)
dart_key = st.sidebar.text_input("DART 인증키", type="password")
gemini_key = st.sidebar.text_input("Gemini API 키", type="password")
model = st.sidebar.selectbox(
    "분석 모델",
    ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"],
    index=0,
)
include_report = st.sidebar.checkbox(
    "사업보고서 본문까지 분석 (느림)", value=True,
    help="원문을 내려받아 사업 내용까지 분석합니다. 시간이 더 걸립니다.",
)

# ------------------------------------------------------------------ 본문 헤더
st.title("📊 DART 공시 자동 요약·분석기")
st.caption("회사명을 입력하면 DART 공시를 조회해 재무·사업·특이사항을 AI가 정리합니다.")

query = st.text_input("회사명을 입력하세요", placeholder="예) 삼성전자, 카카오, NAVER")

# 세션 상태 초기화
for k in ("companies", "selected", "result"):
    st.session_state.setdefault(k, None)


# ------------------------------------------------------------------ 1) 회사 검색
def do_search():
    if not dart_key:
        st.warning("먼저 사이드바에 DART 인증키를 입력해주세요.")
        return
    if not query.strip():
        st.warning("회사명을 입력해주세요.")
        return
    with st.spinner("회사 목록에서 검색 중..."):
        try:
            companies = dart.load_corp_list(dart_key)
        except Exception as e:
            st.error(f"회사 목록 조회 실패: {e}")
            return
        matched = dart.search_companies(companies, query)
    if not matched:
        st.info("일치하는 회사를 찾지 못했습니다. 정확한 상호를 확인해주세요.")
        st.session_state.companies = None
    else:
        st.session_state.companies = matched
        st.session_state.result = None


def _run(selected, dart_key, gemini_key, model, include_report):
    corp_code = selected["corp_code"]
    corp_name = selected["corp_name"]
    result = {"name": corp_name}

    progress = st.progress(0, text="기업 개황 조회 중...")
    try:
        ov = dart.get_company_overview(dart_key, corp_code)
        result["overview"] = ov
        progress.progress(20, text="재무제표 조회 중...")

        fin = dart.get_financials(dart_key, corp_code)
        result["fin"] = fin
        progress.progress(45, text="공시 목록 조회 중...")

        disclosures = dart.get_disclosure_list(dart_key, corp_code)
        result["disclosures"] = disclosures
        progress.progress(60, text="사업보고서 본문 조회 중...")

        biz_text, report_nm = ("", None)
        if include_report:
            biz_text, report_nm = dart.get_business_report_text(dart_key, corp_code)
        result["biz_text"] = biz_text
        result["report_nm"] = report_nm
        progress.progress(75, text="AI가 분석 중입니다...")

        report = ai.analyze(
            api_key=gemini_key,
            corp_name=corp_name,
            overview_text=ai.build_overview_text(ov),
            financial_text=ai.build_financial_text(fin),
            business_text=biz_text,
            disclosure_text=ai.build_disclosure_text(disclosures),
            model=model,
        )
        result["report"] = report
        progress.progress(100, text="완료!")
        st.session_state.result = result
    except Exception as e:
        progress.empty()
        st.error(f"분석 중 오류가 발생했습니다: {e}")
        return
    progress.empty()


if st.button("🔍 회사 검색", type="primary"):
    do_search()


# ------------------------------------------------------------------ 2) 회사 선택 + 분석
if st.session_state.companies:
    options = {
        f"{c['corp_name']}"
        + (f" (종목 {c['stock_code']})" if c["stock_code"] else " (비상장)"): c
        for c in st.session_state.companies
    }
    choice = st.selectbox("분석할 회사를 선택하세요", list(options.keys()))
    selected = options[choice]

    if st.button("📈 이 회사 분석하기", type="primary"):
        if not gemini_key:
            st.warning("AI 분석을 위해 Gemini API 키를 입력해주세요.")
        else:
            _run(selected, dart_key, gemini_key, model, include_report)


# ------------------------------------------------------------------ 3) 결과 표시
res = st.session_state.result
if res:
    st.divider()
    st.header(f"📋 {res['name']} 분석 리포트")

    ov = res.get("overview") or {}
    cols = st.columns(4)
    cols[0].metric("대표자", ov.get("ceo_nm", "-"))
    cols[1].metric("설립일", ov.get("est_dt", "-"))
    market = {"Y": "코스피", "K": "코스닥", "N": "코넥스", "E": "기타"}.get(
        ov.get("corp_cls", ""), ov.get("corp_cls", "-")
    )
    cols[2].metric("시장", market)
    cols[3].metric("종목코드", ov.get("stock_code", "-") or "비상장")

    # 재무 표 + 그래프
    fin = res.get("fin")
    if fin:
        st.subheader("💰 재무제표 (최근 3개년)")
        years = [f"{y}년" for y in fin["years"]]
        df = pd.DataFrame(
            {name: vals for name, vals in fin["rows"].items()}, index=years
        )
        # 억 원 단위 변환
        df_eok = (df / 1e8).round(1)
        df_eok.columns = [c + "(억원)" for c in df_eok.columns]
        st.dataframe(df_eok, use_container_width=True)

        chart_cols = [c for c in ["매출액", "영업이익", "당기순이익"] if c in fin["rows"]]
        if chart_cols:
            chart_df = (df[chart_cols] / 1e8).round(1)
            st.bar_chart(chart_df)
        st.caption(f"기준: {'연결재무제표' if fin['fs_div']=='CFS' else '별도재무제표'}")
    else:
        st.info("재무제표 데이터를 찾지 못했습니다. (비상장이거나 보고서 미제출)")

    # AI 리포트
    st.subheader("🤖 AI 종합 분석")
    st.markdown(res.get("report", "분석 결과 없음"))

    # 공시 목록
    disclosures = res.get("disclosures") or []
    if disclosures:
        with st.expander(f"📁 최근 공시 목록 ({len(disclosures)}건)"):
            dl_df = pd.DataFrame(
                [
                    {"접수일": d.get("rcept_dt"), "보고서명": d.get("report_nm"),
                     "제출인": d.get("flr_nm")}
                    for d in disclosures
                ]
            )
            st.dataframe(dl_df, use_container_width=True, hide_index=True)

    # 다운로드
    st.subheader("📥 리포트 저장")
    md = f"# {res['name']} 분석 리포트\n\n{res.get('report','')}"
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "📝 마크다운 (.md)",
            md,
            file_name=f"{res['name']}_분석리포트.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with c2:
        try:
            docx_bytes = ex.build_docx(res)
            st.download_button(
                "📄 Word (.docx)",
                docx_bytes,
                file_name=f"{res['name']}_분석리포트.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )
        except Exception as e:
            st.caption(f"Word 생성 오류: {e}")
    with c3:
        try:
            pptx_bytes = ex.build_pptx(res)
            st.download_button(
                "📊 PPT (.pptx)",
                pptx_bytes,
                file_name=f"{res['name']}_분석리포트.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                use_container_width=True,
            )
        except Exception as e:
            st.caption(f"PPT 생성 오류: {e}")

st.divider()
st.caption(
    "데이터 출처: 금융감독원 전자공시시스템 OpenDART. "
    "본 분석은 참고용이며 투자 판단의 책임은 이용자에게 있습니다."
)
