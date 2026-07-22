# -*- coding: utf-8 -*-
"""
프리컴퓨트 그룹 '데이터 수집' 워커 (LLM 없음) — 하루 1회.
윈도우 프로그램(NewsView/ReportView/MacroView)의 수집 로직을 그대로 이식해
뉴스/공시/증권사리포트/변동성 원본을 Supabase(group_raw)에 저장한다.
요약(LLM)은 모바일 요청 시 서버가 '윈도우와 동일한 프롬프트'로 수행(get_*_report).

환경변수:
  ADMIN_TOKEN  (필수) 업로드 + 수집키 조회
  수집키(DART/FRED/ECOS)는 관리자(1등급)가 등록한 키를 admin_get_collect_key로 자동 조회.
"""
import os
import re
import sys
import datetime
import urllib.parse as up

import requests
from bs4 import BeautifulSoup

FUNCTIONS_URL = os.environ.get(
    "SUPABASE_FUNCTIONS_URL",
    "https://lcjsjcwifrqwlsfjfuwl.supabase.co/functions/v1/app-backend")
ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY", "sb_publishable_PlnvY9umJA7St9TUZp88Zg_UCozbAbk")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
HDR = {"Authorization": f"Bearer {ANON_KEY}", "Content-Type": "application/json"}


def _post(action, **params):
    return requests.post(FUNCTIONS_URL, json={"action": action, "admin_token": ADMIN_TOKEN, **params},
                         headers=HDR, timeout=90)


def get_collect_keys():
    try:
        r = _post("admin_get_collect_key")
        if r.status_code == 200:
            d = r.json()
            return {"dart": d.get("dart_api_key", "") or "", "fred": d.get("fred_api_key", "") or "",
                    "bok": d.get("bok_api_key", "") or "", "youtube": d.get("youtube_api_key", "") or ""}
    except Exception as e:
        print("수집키 조회 예외:", e)
    return {"dart": "", "fred": "", "bok": "", "youtube": ""}


def save_group(feature, data):
    r = _post("admin_save_group_raw", feature=feature, data=data,
              collected_at=datetime.datetime.utcnow().isoformat() + "Z")
    print(f"upload[{feature}]:", r.status_code, r.text[:150])


# ══════════════════ 뉴스 (윈도우 NewsView) ══════════════════
# 네이버 금융 주요뉴스 목록 → 각 기사 본문(dic_area) 수집 → 본문 텍스트를 요약 근거로 저장.
def _news_one_day(target_date):
    date_str = target_date.strftime("%Y-%m-%d")
    items = []
    try:
        url = f"https://finance.naver.com/news/mainnews.naver?date={date_str}"
        resp = requests.get(url, headers=UA, timeout=12)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        news_list = soup.select("div.mainNewsList ul.newsList li dl dd.articleSubject a") \
            or soup.select("div.mainNewsList ul.newsList li dl dt.articleSubject a")
        for a in news_list[:15]:
            title = a.text.strip()
            link = a.get("href") or ""
            if not title or not link:
                continue
            if not link.startswith("http"):
                link = "https://finance.naver.com" + link
            try:
                q = up.parse_qs(up.urlparse(link).query)
                if "office_id" in q and "article_id" in q:
                    link = f"https://n.news.naver.com/mnews/article/{q['office_id'][0]}/{q['article_id'][0]}"
            except Exception:
                pass
            dl = a.find_parent("dl")
            press = ""
            if dl:
                ps = dl.select_one("dd.articleSummary span.press")
                if ps:
                    press = ps.text.strip()
            items.append({"title": title, "link": link, "press": press})
    except Exception as e:
        print("news list 실패:", date_str, e)

    # 본문 수집
    text = ""
    for it in items:
        try:
            r = requests.get(it["link"], headers=UA, timeout=6)
            b = BeautifulSoup(r.text, "html.parser").select_one("article#dic_area")
            if b:
                text += f"\n\n[제목: {it['title']}, 언론사: {it['press']}]\n" + b.text.strip()[:1000]
        except Exception:
            pass
    return {"count": len(items), "text": text[:9000],
            "items": [{"title": i["title"], "press": i["press"]} for i in items]}


def collect_news():
    now = datetime.datetime.now()
    base = now - datetime.timedelta(days=1) if now.hour < 3 else now
    return {"today": _news_one_day(base),
            "yesterday": _news_one_day(base - datetime.timedelta(days=1))}


# ══════════════════ 공시 (윈도우 NewsView DART) ══════════════════
# DART OpenAPI 최근 3일, 주요사항보고(B)+지분공시(D). corp_cls(Y=KOSPI,K=KOSDAQ)로 시장 구분.
def collect_disclosure(dart_key):
    if not dart_key:
        print("공시 생략: DART 키 없음")
        return {"items": []}
    items = []
    seen = set()
    try:
        for off in range(3):  # 오늘, 어제, 그제
            d_str = (datetime.datetime.now() - datetime.timedelta(days=off)).strftime("%Y%m%d")
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
                    key = (it.get("corp_name", ""), it.get("report_nm", ""), it.get("rcept_dt", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append({"corp_name": it.get("corp_name", ""), "report_nm": it.get("report_nm", ""),
                                  "flr_nm": it.get("flr_nm", ""), "rcept_dt": it.get("rcept_dt", ""),
                                  "corp_cls": it.get("corp_cls", "")})
    except Exception as e:
        print("disclosure 실패:", e)
    return {"items": items}


# ══════════════════ 증권사 리포트 (윈도우 ReportView) ══════════════════
# 네이버 리서치 목록에서 가장 최근 영업일 발간 리포트 최대 20건.
def collect_report():
    reports = []
    try:
        r = requests.get("https://finance.naver.com/research/company_list.naver", headers=UA, timeout=12)
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.select_one("table.type_1")
        if table:
            for row in table.select("tr")[:60]:
                cols = row.select("td")
                if len(cols) >= 5:
                    a = cols[1].select_one("a")
                    if not a:
                        continue
                    reports.append({"stock": cols[0].text.strip(), "title": a.text.strip(),
                                    "broker": cols[2].text.strip(), "at": cols[4].text.strip()})
        latest = max((x["at"] for x in reports), default="")
        reports = [x for x in reports if x["at"] == latest][:20] if latest else reports[:20]
        return {"reports": reports, "latest_date": latest}
    except Exception as e:
        print("report 실패:", e)
        return {"reports": [], "latest_date": ""}


# ══════════════════ 변동성 (윈도우 MacroView) ══════════════════
# FRED 지표 + 한국은행 ECOS + 발표 캘린더(2026 fallback + 네 마녀의 날) + 구글뉴스 전망 헤드라인.
FALLBACK_DATES = {
    "fomc": ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"],
    "bok": ["2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28", "2026-07-16", "2026-08-27", "2026-10-22", "2026-11-26"],
    "cpi": ["2026-01-14", "2026-02-12", "2026-03-12", "2026-04-14", "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-15", "2026-10-14", "2026-11-13", "2026-12-10"],
    "ppi": ["2026-01-15", "2026-02-13", "2026-03-13", "2026-04-15", "2026-05-13", "2026-06-11", "2026-07-15", "2026-08-13", "2026-09-16", "2026-10-15", "2026-11-14", "2026-12-11"],
    "pce": ["2026-01-30", "2026-02-27", "2026-03-27", "2026-04-30", "2026-05-29", "2026-06-26", "2026-07-30", "2026-08-28", "2026-09-25", "2026-10-30", "2026-11-25", "2026-12-23"],
    "empsit": ["2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"],
    "gdp": ["2026-01-29", "2026-04-30", "2026-07-30", "2026-10-29"],
    "m2": ["2026-01-27", "2026-02-24", "2026-03-24", "2026-04-28", "2026-05-26", "2026-06-23", "2026-07-28", "2026-08-25", "2026-09-22", "2026-10-27", "2026-11-24", "2026-12-22"],
}


def _prev_next(dates):
    now = datetime.date.today()
    ds = [datetime.datetime.strptime(d, "%Y-%m-%d").date() for d in dates]
    past = [d for d in ds if d < now]
    fut = [d for d in ds if d >= now]
    prev = max(past).isoformat() if past else min(ds).isoformat()
    nxt = min(fut).isoformat() if fut else max(ds).isoformat()
    return prev, nxt


def _witching():
    now = datetime.date.today()
    for m in (3, 6, 9, 12):
        fris = [d for d in range(1, 22) if datetime.date(now.year, m, d).weekday() == 4]
        wd = datetime.date(now.year, m, fris[2])
        if wd >= now:
            prev_m = {3: 12, 6: 3, 9: 6, 12: 9}[m]
            py = now.year - 1 if m == 3 else now.year
            pf = [d for d in range(1, 22) if datetime.date(py, prev_m, d).weekday() == 4]
            return datetime.date(py, prev_m, pf[2]).isoformat(), wd.isoformat()
    return "", ""


def collect_macro(fred_key, bok_key):
    fred_text = "[미국 FRED 공식 거시경제 팩트 수치 (최우선 데이터)]\n"
    if fred_key:
        series = [("DFEDTARU", "미국 연방기금 목표금리 상단(%)", "lin"), ("DFEDTARL", "미국 연방기금 목표금리 하단(%)", "lin"),
                  ("M2SL", "미국 M2 통화공급량(10억$)", "lin"), ("CPIAUCSL", "미국 CPI 전년비(%)", "pc1"),
                  ("PPIACO", "미국 PPI 전년비(%)", "pc1"), ("PCEPI", "미국 PCE 전년비(%)", "pc1"),
                  ("UNRATE", "미국 실업률(%)", "lin"), ("PAYEMS", "미국 비농업고용 전월변동(천명)", "chg"),
                  ("A191RL1Q225SBEA", "미국 실질GDP 성장률(전분기比 연율 %)", "lin")]
        for sid, label, units in series:
            try:
                u = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
                     f"&api_key={fred_key}&file_type=json&sort_order=desc&limit=1&units={units}")
                obs = requests.get(u, timeout=8).json().get("observations", [])
                if obs and obs[0].get("value", ".") != ".":
                    v = obs[0]["value"]
                    try:
                        v = f"{float(v):.2f}"
                    except Exception:
                        pass
                    fred_text += f"- {label}: {v} (기준월/분기: {obs[0].get('date','')})\n"
            except Exception as e:
                print("FRED", sid, e)
    else:
        fred_text += "(FRED 키 미등록)\n"
    fred_text += "\n"

    bok_text = "[한국은행 ECOS 공식 거시경제 팩트 수치 (최우선 데이터)]\n"
    if bok_key:
        try:
            now_s = datetime.datetime.now().strftime("%Y%m%d")
            past_s = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime("%Y%m%d")
            now_m, past_m = now_s[:6], past_s[:6]
            r = requests.get(f"https://ecos.bok.or.kr/api/StatisticSearch/{bok_key}/json/kr/1/1/731Y001/D/{past_s}/{now_s}/0101000", timeout=8)
            rows = r.json().get("StatisticSearch", {}).get("row", []) if r.status_code == 200 else []
            if rows:
                bok_text += f"- 한국은행 기준금리: {rows[-1]['DATA_VALUE']}% (발표일자: {rows[-1]['TIME']})\n"
            r = requests.get(f"https://ecos.bok.or.kr/api/StatisticSearch/{bok_key}/json/kr/1/1/512Y015/M/{past_m}/{now_m}/9998001", timeout=8)
            rows = r.json().get("StatisticSearch", {}).get("row", []) if r.status_code == 200 else []
            if rows:
                bok_text += f"- 한국 기업경기실사지수(전산업 BSI): {rows[-1]['DATA_VALUE']} (발표월: {rows[-1]['TIME']})\n"
        except Exception as e:
            print("ECOS", e)
    else:
        bok_text += "(ECOS 키 미등록)\n"
    bok_text += "\n"

    # 발표 캘린더(fallback 기준 직전/다음)
    cal = "[프로그램 내부 계산 캘린더 팩트 (직전/다음 발표일)]\n"
    labels = {"fomc": "미국 FOMC", "bok": "한국은행 금통위", "m2": "미국 M2", "cpi": "미국 CPI",
              "ppi": "미국 PPI", "pce": "미국 PCE", "empsit": "미국 비농업고용", "gdp": "미국 GDP"}
    for k, lab in labels.items():
        p, n = _prev_next(FALLBACK_DATES[k])
        cal += f"- {lab}: 직전 {p} / 다음 {n}\n"
    wp, wn = _witching()
    cal += f"- 네 마녀의 날(선물·옵션 동시만기): 직전 {wp} / 다음 {wn}\n\n"

    # 구글뉴스 전망 헤드라인
    news_text = "[구글뉴스 추가 헤드라인 (전망/컨센서스 확인용)]\n"
    try:
        for q in ("미국 금리 CPI 전망 컨센서스", "FOMC 금통위 기준금리 전망"):
            url = ("https://news.google.com/rss/search?q=" + up.quote(f"{q} when:14d") + "&hl=ko&gl=KR&ceid=KR:ko")
            import xml.etree.ElementTree as ET
            root = ET.fromstring(requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).content)
            for it in root.findall(".//item")[:10]:
                t = (it.findtext("title") or "").strip()
                if t:
                    news_text += f"- {t}\n"
    except Exception as e:
        print("macro news", e)

    return {"text": fred_text + bok_text + cal + news_text}


def main():
    if not ADMIN_TOKEN:
        print("ERROR: ADMIN_TOKEN 필요")
        sys.exit(1)
    keys = get_collect_keys()
    print("뉴스 수집..."); save_group("news", collect_news())
    print("공시 수집..."); save_group("disclosure", collect_disclosure(keys["dart"]))
    print("리포트 수집..."); save_group("report", collect_report())
    print("변동성 수집..."); save_group("macro", collect_macro(keys["fred"], keys["bok"]))
    print("완료.")


if __name__ == "__main__":
    main()
