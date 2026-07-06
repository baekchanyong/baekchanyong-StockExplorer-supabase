# -*- coding: utf-8 -*-
"""
2-1 시장 관심도 '데이터 수집' 워커 (프리컴퓨트, LLM 없음)
- 구글뉴스 RSS + DuckDuckGo 웹검색 + YouTube Data API 로 최근 1주 데이터 수집
- 수집 원본을 Supabase(admin_save_stage2_raw)에 저장
- 요약(LLM)은 하지 않는다 → 모바일 요청 시 서버가 '사용자별 등록 AI'로 요약 (백엔드 get_stage2_report)

환경변수:
  ADMIN_TOKEN       (필수) 업로드 인증
  YOUTUBE_API_KEY   (선택) 지정 시 이 키 우선 사용. 미지정 시 관리자(1등급)가
                    AI환경설정에 등록한 유튜브 키를 서버에서 가져와 사용. 둘 다 없으면 유튜브 생략.
"""
import os
import re
import sys
import datetime
import urllib.parse
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
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def fetch_news_rss(query, max_items=20, days=7):
    """구글 뉴스 RSS (최근 days일)"""
    out = []
    try:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(f"{query} when:{days}d")
               + "&hl=ko&gl=KR&ceid=KR:ko")
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item')[:max_items]:
                title = (item.findtext('title') or "").strip()
                desc = re.sub(r'<[^>]+>', '', item.findtext('description') or "").strip()
                pub = (item.findtext('pubDate') or "")[:16]
                src_el = item.find('source')
                src = src_el.text.strip() if src_el is not None and src_el.text else ""
                out.append({"title": title, "desc": desc[:150], "date": pub, "source": src})
    except Exception as e:
        print("news_rss 실패:", query, e)
    return out


def fetch_ddg(query, max_items=12):
    """DuckDuckGo HTML 웹검색 (해외반응/일반의견)"""
    out = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        resp = requests.get(url, headers=UA, timeout=12)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            for a in soup.find_all('a', {'class': 'result__snippet'})[:max_items]:
                desc = a.text.strip()
                title_el = a.find_previous('h2', {'class': 'result__title'})
                title = title_el.text.strip() if title_el else ""
                out.append({"title": title, "desc": desc[:150]})
    except Exception as e:
        print("ddg 실패:", query, e)
    return out


def get_youtube_key():
    """유튜브 키: env 우선, 없으면 관리자가 AI환경설정에 등록한 키를 서버에서 조회."""
    if YOUTUBE_API_KEY:
        return YOUTUBE_API_KEY
    try:
        resp = requests.post(FUNCTIONS_URL,
                             json={"action": "admin_get_collect_key", "admin_token": ADMIN_TOKEN},
                             headers={"Authorization": f"Bearer {ANON_KEY}",
                                      "Content-Type": "application/json"}, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("youtube_api_key", "") or ""
        print("youtube 키 조회 실패:", resp.status_code, resp.text[:120])
    except Exception as e:
        print("youtube 키 조회 예외:", e)
    return ""


def fetch_youtube(query, api_key, max_items=10):
    if not api_key:
        return []
    out = []
    try:
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"key": api_key, "q": query, "part": "snippet", "type": "video",
                  "order": "viewCount", "publishedAfter": cutoff, "maxResults": max_items, "regionCode": "KR"}
        r = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=12)
        if r.status_code == 200:
            for it in r.json().get("items", []):
                sn = it.get("snippet", {})
                out.append({"title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
                            "date": (sn.get("publishedAt", "") or "")[:10]})
        else:
            print("youtube 오류:", r.status_code, r.text[:150])
    except Exception as e:
        print("youtube 실패:", e)
    return out


def date_range(*lists):
    from email.utils import parsedate_to_datetime
    dates = []
    for lst in lists:
        for it in lst:
            pd = it.get("date", "")
            if not pd:
                continue
            try:
                if 'T' in pd or '-' in pd[:10]:
                    dates.append(datetime.datetime.strptime(pd[:10], "%Y-%m-%d"))
                else:
                    dates.append(parsedate_to_datetime(pd))
            except Exception:
                pass
    if dates:
        return f"{min(dates).strftime('%y.%m.%d')} ~ {max(dates).strftime('%y.%m.%d')}"
    today = datetime.datetime.now()
    return f"{(today - datetime.timedelta(days=7)).strftime('%y.%m.%d')} ~ {today.strftime('%y.%m.%d')}"


def collect_sources(*lists):
    names = []
    for lst in lists:
        for it in lst:
            s = it.get("source", "").strip()
            if s and s not in names:
                names.append(s)
    return names[:8]


def main():
    if not ADMIN_TOKEN:
        print("ERROR: ADMIN_TOKEN 필요"); sys.exit(1)

    print("뉴스 수집...")
    news_trend = fetch_news_rss("주식 투자 AI 반도체 원자력 코인 핫이슈", 30) \
        + fetch_news_rss("주식 시장 주요 테마", 20)
    news_ip = fetch_news_rss("게임 기대작 신작 영화 애니메이션", 12)
    news_ma = fetch_news_rss("대기업 신사업 인수합병 M&A", 15)
    news_policy = fetch_news_rss("정부 정책 수혜주 지원 사업", 15)

    print("웹검색(해외/일반/IP) 수집...")
    foreign = fetch_ddg("us global stock market trend semiconductor ai", 12)
    opinion = fetch_ddg("주식 투자 추천 테마 개인투자자 의견 블로그 후기", 12)
    web_ip = fetch_ddg("최신 게임 기대작 후속작 출시 예정 GTA6, 주요 영화 애니 후속작", 12)

    print("유튜브 수집...")
    yt_key = get_youtube_key()
    youtube = fetch_youtube("주식 투자 AI 반도체 원자력", yt_key)

    raw = {
        "date_range": date_range(news_trend, news_ip, news_ma, news_policy),
        "sources": collect_sources(news_trend, news_ip, news_ma, news_policy),
        "news_trend": news_trend, "news_ip": news_ip, "web_ip": web_ip,
        "news_ma": news_ma, "news_policy": news_policy,
        "foreign": foreign, "opinion": opinion, "youtube": youtube,
    }
    counts = {k: len(v) for k, v in raw.items() if isinstance(v, list)}
    print("수집 건수:", counts)

    payload = {"action": "admin_save_stage2_raw", "admin_token": ADMIN_TOKEN,
               "data": raw, "collected_at": datetime.datetime.utcnow().isoformat() + "Z"}
    resp = requests.post(FUNCTIONS_URL, json=payload,
                         headers={"Authorization": f"Bearer {ANON_KEY}",
                                  "Content-Type": "application/json"}, timeout=60)
    print("upload_raw:", resp.status_code, resp.text[:200])
    resp.raise_for_status()
    print("완료.")


if __name__ == "__main__":
    main()
