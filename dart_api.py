"""
DART 오픈 API 연동 모듈
- 회사 검색(고유번호), 기업개황, 공시목록, 재무제표, 사업보고서 본문 조회
공식 문서: https://opendart.fss.or.kr/guide/main.do
"""

import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

BASE = "https://opendart.fss.or.kr/api"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)


class DartError(Exception):
    """DART API 관련 오류"""
    pass


# --------------------------------------------------------------------------
# 1. 회사 고유번호(corp_code) 목록 -- 회사명으로 corp_code를 찾기 위해 사용
# --------------------------------------------------------------------------
def _corpcode_cache_path():
    return os.path.join(CACHE_DIR, "corpcode.xml")


def load_corp_list(api_key, force_refresh=False):
    """
    전체 회사 고유번호 목록을 내려받아 [(corp_code, corp_name, stock_code), ...] 반환.
    최초 1회 다운로드 후 로컬에 캐시(하루 유지)한다.
    """
    path = _corpcode_cache_path()
    fresh = (
        os.path.exists(path)
        and (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))) < timedelta(days=1)
    )

    if force_refresh or not fresh:
        url = f"{BASE}/corpCode.xml"
        r = requests.get(url, params={"crtfc_key": api_key}, timeout=30)
        r.raise_for_status()
        # 응답이 ZIP인지 확인 (에러면 JSON/XML 텍스트가 옴)
        if r.content[:2] != b"PK":
            _raise_status_from_text(r.text)
            raise DartError("회사 목록을 내려받지 못했습니다. API 키를 확인해주세요.")
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            name = zf.namelist()[0]
            xml_bytes = zf.read(name)
        with open(path, "wb") as f:
            f.write(xml_bytes)
    else:
        with open(path, "rb") as f:
            xml_bytes = f.read()

    root = ET.fromstring(xml_bytes)
    companies = []
    for item in root.iter("list"):
        companies.append(
            {
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "stock_code": (item.findtext("stock_code") or "").strip(),
            }
        )
    return companies


def search_companies(companies, keyword):
    """
    회사명 키워드로 목록에서 후보를 찾는다.
    상장사(종목코드 있음)를 우선 정렬하고, 완전일치를 맨 위로 올린다.
    """
    kw = keyword.strip()
    if not kw:
        return []
    matched = [c for c in companies if kw in c["corp_name"]]

    def sort_key(c):
        exact = 0 if c["corp_name"] == kw else 1
        listed = 0 if c["stock_code"] else 1  # 상장사 우선
        return (exact, listed, len(c["corp_name"]))

    matched.sort(key=sort_key)
    return matched[:30]


# --------------------------------------------------------------------------
# 2. 기업 개황
# --------------------------------------------------------------------------
def get_company_overview(api_key, corp_code):
    data = _get_json("company.json", api_key, {"corp_code": corp_code})
    return data


# --------------------------------------------------------------------------
# 3. 공시 목록
# --------------------------------------------------------------------------
def get_disclosure_list(api_key, corp_code, months_back=12, max_count=30):
    end = datetime.now()
    bgn = end - timedelta(days=months_back * 31)
    params = {
        "corp_code": corp_code,
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": 100,
        "page_no": 1,
    }
    data = _get_json("list.json", api_key, params, allow_empty=True)
    items = data.get("list", []) if data else []
    return items[:max_count]


# --------------------------------------------------------------------------
# 4. 재무제표 (단일회사 전체 재무제표)
#    한 번 호출로 당기/전기/전전기 3개년 금액을 함께 얻는다.
# --------------------------------------------------------------------------
# 관심 계정: (표시명, 매칭 키워드들, 재무제표 구분)
KEY_ACCOUNTS = [
    ("매출액", ["매출액", "수익(매출액)", "영업수익"], "IS"),
    ("영업이익", ["영업이익"], "IS"),
    ("당기순이익", ["당기순이익", "당기순이익(손실)"], "IS"),
    ("자산총계", ["자산총계"], "BS"),
    ("부채총계", ["부채총계"], "BS"),
    ("자본총계", ["자본총계"], "BS"),
]

REPRT_ANNUAL = "11011"  # 사업보고서


def get_financials(api_key, corp_code, latest_year=None):
    """
    최신 사업연도부터 뒤로 내려가며 재무제표를 조회한다.
    연결(CFS) 우선, 없으면 별도(OFS).
    반환: {"years": [연도3개], "rows": {계정명: [금액3개]}, "fs_div": "CFS/OFS", "base_year": int}
    """
    if latest_year is None:
        latest_year = datetime.now().year - 1

    last_err = None
    for year in range(latest_year, latest_year - 4, -1):  # 최근 4년까지 시도
        for fs_div in ("CFS", "OFS"):
            try:
                data = _get_json(
                    "fnlttSinglAcntAll.json",
                    api_key,
                    {
                        "corp_code": corp_code,
                        "bsns_year": str(year),
                        "reprt_code": REPRT_ANNUAL,
                        "fs_div": fs_div,
                    },
                    allow_empty=True,
                )
            except DartError as e:
                last_err = e
                continue
            if not data or not data.get("list"):
                continue
            return _parse_financials(data["list"], year, fs_div)
    if last_err:
        raise last_err
    return None


def _parse_financials(rows, base_year, fs_div):
    # 3개년 라벨: 당기 / 전기 / 전전기
    years = [base_year, base_year - 1, base_year - 2]
    result = {name: [None, None, None] for name, _, _ in KEY_ACCOUNTS}

    for r in rows:
        acc_nm = (r.get("account_nm") or "").strip()
        for name, keywords, _sj in KEY_ACCOUNTS:
            if result[name][0] is not None:
                continue  # 이미 채워짐
            if any(acc_nm == k or acc_nm.startswith(k) for k in keywords):
                result[name] = [
                    _to_num(r.get("thstrm_amount")),
                    _to_num(r.get("frmtrm_amount")),
                    _to_num(r.get("bfefrmtrm_amount")),
                ]
                break

    return {
        "years": years,
        "rows": result,
        "fs_div": fs_div,
        "base_year": base_year,
    }


def _to_num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


# --------------------------------------------------------------------------
# 5. 사업보고서 본문 텍스트 (선택 기능)
#    최근 사업보고서 원문을 받아 텍스트를 추출한다. 용량이 커서 시간이 걸릴 수 있음.
# --------------------------------------------------------------------------
def get_latest_business_report_rcept(api_key, corp_code):
    """가장 최근 '사업보고서' 공시의 접수번호를 찾는다."""
    end = datetime.now()
    bgn = end - timedelta(days=500)
    data = _get_json(
        "list.json",
        api_key,
        {
            "corp_code": corp_code,
            "bgn_de": bgn.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "pblntf_ty": "A",  # 정기공시
            "page_count": 100,
            "page_no": 1,
        },
        allow_empty=True,
    )
    for item in (data.get("list", []) if data else []):
        if "사업보고서" in (item.get("report_nm") or ""):
            return item.get("rcept_no"), item.get("report_nm")
    return None, None


def get_business_report_text(api_key, corp_code, max_chars=12000):
    """
    최근 사업보고서 원문을 받아 사람이 읽을 수 있는 텍스트로 추출.
    실패하면 빈 문자열 반환(앱이 멈추지 않도록).
    """
    rcept_no, report_nm = get_latest_business_report_rcept(api_key, corp_code)
    if not rcept_no:
        return "", None
    try:
        r = requests.get(
            f"{BASE}/document.xml",
            params={"crtfc_key": api_key, "rcept_no": rcept_no},
            timeout=60,
        )
        r.raise_for_status()
        if r.content[:2] != b"PK":
            return "", report_nm
        texts = []
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            for name in zf.namelist():
                raw = zf.read(name)
                try:
                    txt = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        txt = raw.decode("euc-kr")
                    except UnicodeDecodeError:
                        continue
                texts.append(txt)
        full = "\n".join(texts)
        clean = _strip_markup(full)
        # '사업의 개요' 부근부터 자르면 핵심이 많이 담김
        idx = clean.find("사업의 개요")
        if idx == -1:
            idx = clean.find("회사의 개요")
        if idx > 0:
            clean = clean[idx:]
        return clean[:max_chars], report_nm
    except Exception:
        return "", report_nm


def _strip_markup(text):
    text = re.sub(r"<[^>]+>", " ", text)          # 태그 제거
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)   # HTML 엔티티 제거
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 공통 유틸
# --------------------------------------------------------------------------
def _get_json(endpoint, api_key, params, allow_empty=False):
    p = {"crtfc_key": api_key}
    p.update(params)
    r = requests.get(f"{BASE}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    data = r.json()
    status = data.get("status")
    if status == "000":
        return data
    if status == "013":  # 조회된 데이터 없음
        if allow_empty:
            return data
        raise DartError("조회된 데이터가 없습니다. (status 013)")
    _raise_status(status, data.get("message"))


STATUS_MESSAGES = {
    "010": "등록되지 않은 인증키입니다. API 키를 확인해주세요.",
    "011": "사용할 수 없는(만료/정지) 인증키입니다.",
    "012": "접근할 수 없는 IP입니다.",
    "013": "조회된 데이터가 없습니다.",
    "020": "요청 제한을 초과했습니다. (분당 요청 한도)",
    "021": "조회 가능한 회사 개수가 초과되었습니다.",
    "100": "필드의 부적절한 값입니다.",
    "101": "부적절한 접근입니다.",
    "800": "시스템 점검 중입니다.",
    "900": "정의되지 않은 오류입니다.",
    "901": "사용자 계정의 개인정보 보유기간이 만료되었습니다.",
}


def _raise_status(status, message=None):
    msg = STATUS_MESSAGES.get(status, message or f"DART 오류 (status {status})")
    raise DartError(msg)


def _raise_status_from_text(text):
    m = re.search(r'"status"\s*:\s*"(\d+)"', text)
    if m:
        _raise_status(m.group(1))
