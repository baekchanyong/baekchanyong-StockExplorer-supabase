# -*- coding: utf-8 -*-
"""
프리컴퓨트 그룹 '데이터 수집' 워커 (LLM 없음) — 하루 1회.
뉴스 / 공시 / 증권사 리포트 / 변동성 이벤트 원본을 수집해 Supabase(group_raw)에 저장한다.
요약(LLM)은 하지 않는다 → 모바일 요청 시 서버가 '사용자별 선택 AI'로 요약(get_*_report).

윈도우 프로그램 소스 이식:
  뉴스     ← NewsView (finance.naver.com/news/mainnews.naver)
  공시     ← NewsView (opendart.fss.or.kr/api/list.json, pblntf_ty B/D)
  리포트   ← ReportView (finance.naver.com/research/company_list.naver)
  변동성   ← MacroView (FRED + 한국은행 ECOS)

환경변수:
  ADMIN_TOKEN  (필수) 업로드 인증 + 수집키 조회
  수집용 API 키(DART/FRED/ECOS/YouTube)는 관리자(1등급)가 AI환경설정에 등록한 키를
  admin_get_collect_key로 자동 조회한다 → GitHub Secret 추가 불필요.
"""
import os
import re
import sys
import datetime
import urllib.parse as up
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

FUNCTIONS_URL = os.environ.get(
    "SUPABASE_FUNCTIONS_URL",
    "https://lcjsjcwifrqwlsfjfuwl.supabase.co/functions/v1/app-backend")
ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "sb_publishable_PlnvY9umJA7St9TUZp88Zg_UCozbAbk")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
HDR = {"Authorization": f"Bearer {ANON_KEY}", "Content-Type": "application/json"}


def _post(action, **params):
    payload = {"action": action, "admin_token": ADMIN_TOKEN, **params}
    return requests.post(FUNCTIONS_URL, json=payload, headers=HDR, timeout=60)


def get_collect_keys():
    """관리자 등록 수집키(dart/fred/bok/youtube) 조회."""
    try:
        r = _post("admin_get_collect_key")
        if r.status_code == 200:
            d = r.json()
            return {
                "dart": d.get("dart_api_key", "") or "",
                "fred": d.get("fred_api_key", "") or "",
                "bok": d.get("bok_api_key", "") or "",
                "youtube": d.get("youtube_api_key", "") or "",
            }
        print("수집키 조회 실패:", r.status_code, r.text[:120])
    except Exception as e:
        print("수집키 조회 예외:", e)
    return {"dart": "", "fred": "", "bok": "", "youtube": ""}


def save_group(feature, data):
    r = _post("admin_save_group_raw", feature=feature, data=data,
              collected_at=datetime.datetime.utcnow().isoformat() + "Z")
    print(f"upload[{feature}]:", r.status_code, r.text[:150])


# ── 뉴스: 네이버 금융 주요뉴스(오늘/어제) ────────────────────────────────
def collect_news():
    def one_day(date_str):
        out = []
        try:
            url = f"https://finance.naver.com/news/mainnews.naver?date={date_str}"
            resp = requests.get(url, headers=UA, timeout=12)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("div.mainNewsList ul.newsList li dl dd.articleSubject a") \
                or soup.select("div.mainNewsList ul.newsList li dl dt.articleSubject a")
            for a in items[:15]:
                title = a.text.strip()
                if not title:
                    continue
                dl = a.find_parent("dl")
                press, desc = "", ""
                if dl:
                    p_el = dl.select_one("dd.writing")
                    press = p_el.text.strip() if p_el else ""
                    s_el = dl.select_one("dd.articleSummary")
                    if s_el:
                        desc = re.sub(r"\s+", " ", s_el.text).strip()[:150]
                out.append({"title": title, "desc": desc, "press": press, "at": date_str})
        except Exception as e:
            print("news 수집 실패:", date_str, e)
        return out

    now = datetime.datetime.now()
    today = one_day(now.strftime("%Y-%m-%d"))
    yesterday = one_day((now - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    return {"today": today, "yesterday": yesterday}


# ── 공시: DART OpenAPI (최근 2일, 주요사항보고 B + 지분공시 D) ────────────
def collect_disclosure(dart_key):
    if not dart_key:
        print("공시 생략: DART 키 없음")
        return {"kospi": [], "kosdaq": []}
    kospi, kosdaq = [], []
    seen = set()
    try:
        for offset in range(2):  # 오늘, 어제
            d_str = (datetime.datetime.now() - datetime.timedelta(days=offset)).strftime("%Y%m%d")
            for ty in ("B", "D"):
                url = (f"https://opendart.fss.or.kr/api/list.json?crtfc_key={dart_key}"
                       f"&bgn_de={d_str}&end_de={d_str}&pblntf_ty={ty}&page_count=100")
                r = requests.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                if data.get("status") != "000":
                    continue
                for it in data.get("list", []):
                    key = (it.get("corp_name", ""), it.get("report_nm", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {"name": it.get("corp_name", ""),
                           "title": it.get("report_nm", ""),
                           "at": it.get("rcept_dt", "")}
                    cls = it.get("corp_cls", "")  # Y=유가(KOSPI), K=코스닥
                    if cls == "Y":
                        kospi.append(row)
                    elif cls == "K":
                        kosdaq.append(row)
    except Exception as e:
        print("disclosure 수집 실패:", e)
    return {"kospi": kospi[:40], "kosdaq": kosdaq[:40]}


# ── 증권사 리포트: 네이버 리서치 목록(최근 영업일) ──────────────────────
def collect_report():
    reports = []
    try:
        url = "https://finance.naver.com/research/company_list.naver"
        resp = requests.get(url, headers=UA, timeout=12)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.select_one("table.type_1")
        if not table:
            return {"reports": []}
        for row in table.select("tr")[:80]:
            cols = row.select("td")
            if len(cols) < 5:
                continue
            title_a = cols[1].select_one("a")
            if not title_a:
                continue
            stock = cols[0].text.strip()
            title = title_a.text.strip()
            broker = cols[2].text.strip()
            date = cols[4].text.strip()
            if not title:
                continue
            reports.append({"stock": stock, "broker": broker, "title": title,
                            "opinion": "", "target_price": "", "at": date})
        # 가장 최근 영업일 것만 (여러 날짜 섞임 방지)
        latest = max((r["at"] for r in reports), default="")
        if latest:
            reports = [r for r in reports if r["at"] == latest]
    except Exception as e:
        print("report 수집 실패:", e)
    return {"reports": reports[:40]}


# ── 변동성: FRED 지표 + 한국은행 ECOS + 네 마녀의 날 ─────────────────────
def collect_macro(fred_key, bok_key):
    indicators, events = [], []
    # FRED
    if fred_key:
        fred_series = [
            ("DFEDTARU", "미국 기준금리 상단(%)", "lin"),
            ("M2SL", "미국 M2 통화량(10억$)", "lin"),
            ("CPIAUCSL", "미국 CPI YoY(%)", "pc1"),
            ("PPIACO", "미국 PPI YoY(%)", "pc1"),
            ("PCEPI", "미국 PCE YoY(%)", "pc1"),
            ("UNRATE", "미국 실업률(%)", "lin"),
            ("PAYEMS", "미국 비농업고용 전월변동(천명)", "chg"),
            ("A191RL1Q225SBEA", "미국 실질GDP 성장률(%)", "lin"),
        ]
        for sid, label, units in fred_series:
            try:
                url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
                       f"&api_key={fred_key}&file_type=json&sort_order=desc&limit=2&units={units}")
                r = requests.get(url, timeout=10)
                if r.status_code != 200:
                    continue
                obs = r.json().get("observations", [])
                if not obs:
                    continue
                val = obs[0].get("value", ".")
                at = obs[0].get("date", "")
                change = ""
                if val != "." and len(obs) > 1 and obs[1].get("value", ".") != ".":
                    try:
                        change = f"{float(val) - float(obs[1]['value']):+.2f}"
                    except Exception:
                        pass
                if val != ".":
                    try:
                        val = f"{float(val):.2f}"
                    except Exception:
                        pass
                    indicators.append({"name": label, "value": val, "change": change, "at": at})
            except Exception as e:
                print("FRED 실패:", sid, e)
    else:
        print("변동성: FRED 키 없음(미국 지표 생략)")

    # 한국은행 ECOS (기준금리, BSI)
    if bok_key:
        try:
            now_s = datetime.datetime.now().strftime("%Y%m%d")
            past_s = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime("%Y%m%d")
            now_m = datetime.datetime.now().strftime("%Y%m")
            past_m = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime("%Y%m")
            u_rate = f"https://ecos.bok.or.kr/api/StatisticSearch/{bok_key}/json/kr/1/1/731Y001/D/{past_s}/{now_s}/0101000"
            r = requests.get(u_rate, timeout=10)
            if r.status_code == 200:
                rows = r.json().get("StatisticSearch", {}).get("row", [])
                if rows:
                    indicators.append({"name": "한국 기준금리(%)", "value": rows[-1]["DATA_VALUE"],
                                       "change": "", "at": rows[-1]["TIME"]})
            u_bsi = f"https://ecos.bok.or.kr/api/StatisticSearch/{bok_key}/json/kr/1/1/512Y015/M/{past_m}/{now_m}/9998001"
            r = requests.get(u_bsi, timeout=10)
            if r.status_code == 200:
                rows = r.json().get("StatisticSearch", {}).get("row", [])
                if rows:
                    indicators.append({"name": "한국 전산업 BSI", "value": rows[-1]["DATA_VALUE"],
                                       "change": "", "at": rows[-1]["TIME"]})
        except Exception as e:
            print("ECOS 실패:", e)
    else:
        print("변동성: ECOS 키 없음(한국 지표 생략)")

    # 네 마녀의 날(3·6·9·12월 세번째 금요일) — 다음 도래일
    try:
        now = datetime.datetime.now()
        for m in (3, 6, 9, 12):
            y = now.year
            first = datetime.date(y, m, 1)
            # 세번째 금요일
            fridays = [d for d in range(1, 22)
                       if datetime.date(y, m, d).weekday() == 4]
            wd = datetime.date(y, m, fridays[2])
            if wd >= now.date():
                events.append({"title": "네 마녀의 날(선물·옵션 동시만기)",
                               "desc": "지수 변동성 확대 가능", "at": wd.isoformat()})
                break
    except Exception as e:
        print("witching 계산 실패:", e)

    return {"indicators": indicators, "events": events}


def main():
    if not ADMIN_TOKEN:
        print("ERROR: ADMIN_TOKEN 필요")
        sys.exit(1)
    keys = get_collect_keys()

    print("뉴스 수집...")
    save_group("news", collect_news())
    print("공시 수집...")
    save_group("disclosure", collect_disclosure(keys["dart"]))
    print("증권사 리포트 수집...")
    save_group("report", collect_report())
    print("변동성 수집...")
    save_group("macro", collect_macro(keys["fred"], keys["bok"]))
    print("완료.")


if __name__ == "__main__":
    main()
