# -*- coding: utf-8 -*-
"""
StockExplorer 1단계 저평가 탐색 스캐너 (프리컴퓨트)
- 네이버 시가총액 목록에서 KOSPI/KOSDAQ 전 종목의 코드/현재가/주식수를 수집
- 종목마다 FnGuide/네이버 재무를 스크래핑해 적정주가·목표주가·EPS·BPS·부채비율 등을 계산
  (윈도우 메인 프로그램 app_window_v1.0.0.py 의 analyze_stock 로직과 동일)
- 결과를 Supabase Edge Function(admin_save_stage1_results)에 업로드

환경변수:
  SUPABASE_FUNCTIONS_URL  기본값 있음 (없으면 하드코딩된 기본 URL 사용)
  SUPABASE_ANON_KEY       기본값 있음
  ADMIN_TOKEN             (필수) 결과 업로드 인증용
  SCAN_LIMIT              (선택) 시장별 상위 N개만 스캔 (테스트용). 미설정 시 전체.
사용:
  ADMIN_TOKEN=... python scanner.py
"""
import os
import io
import re
import sys
import json
import time
import datetime
import concurrent.futures

import requests
import pandas as pd
from bs4 import BeautifulSoup

SUPABASE_FUNCTIONS_URL = os.environ.get(
    "SUPABASE_FUNCTIONS_URL",
    "https://lcjsjcwifrqwlsfjfuwl.supabase.co/functions/v1/app-backend")
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "sb_publishable_PlnvY9umJA7St9TUZp88Zg_UCozbAbk")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "0"))  # 0 = 전체

HEADERS = {"User-Agent": "Mozilla/5.0"}
ETF_KEYS = ['KODEX', 'TIGER', 'KBSTAR', 'KINDEX', 'ARIRANG', 'KOSEF', 'HANARO',
            'ACE', 'SOL', 'TIMEFOLIO', 'FOCUS', '마이티', 'TREX', '히어로즈', 'VITA']


def safe_float(val):
    try:
        if pd.isna(val):
            return None
        v = str(val).replace(',', '').strip()
        if not v or v == '-' or v == 'N/A':
            return None
        return float(v)
    except Exception:
        return None


def fetch_page_data(sosok, page):
    url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(res.text, 'html.parser')
        table = soup.find('table', {'class': 'type_2'})
        if not table:
            return [], False
        data = []
        has_data = False
        for row in table.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) > 5:
                a_tag = cols[1].find('a')
                if a_tag:
                    name = a_tag.text.strip()
                    code = a_tag['href'].split('code=')[-1]
                    close_txt = cols[2].text.strip().replace(',', '')
                    stocks_txt = cols[7].text.strip().replace(',', '')
                    if close_txt and stocks_txt:
                        data.append({'Code': code, 'Name': name,
                                     'Close': float(close_txt),
                                     'Stocks': float(stocks_txt) * 1000})
                        has_data = True
        return data, has_data
    except Exception:
        return [], False


def collect_market(sosok):
    """한 시장(0=KOSPI,1=KOSDAQ)의 전 종목을 시총순으로 수집."""
    out = []
    for page in range(1, 46):
        rows, has = fetch_page_data(sosok, page)
        if not has:
            break
        out.extend(rows)
        if SCAN_LIMIT and len(out) >= SCAN_LIMIT:
            out = out[:SCAN_LIMIT]
            break
    return out


def analyze_stock(ticker, name, market, current_price, shares, marcap_rank):
    """윈도우 메인 프로그램 analyze_stock(현재기준)과 동일 로직. 우선주/ETF는 제외."""
    is_pref = not str(ticker).endswith('0') or name.endswith('우') or '우(' in name or '우B' in name
    if is_pref:
        return None
    is_etf = any(k in name for k in ETF_KEYS) or name.endswith('ETF')
    if is_etf:
        return None

    url_fin = f"https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp?pGB=1&gicode=A{ticker}"
    t_equity = 0.0; t_debt = 0.0; c_liab = 0.0; a_eps = 0.0; past_eps = 0.0
    q_eps = 0.0; bps = 0.0; past_bps = 0.0; op_profit = 0.0; is_future_eps = False

    sector = "-"
    naver_html = None
    try:
        res_naver = requests.get(f"https://finance.naver.com/item/main.naver?code={ticker}", headers=HEADERS, timeout=6)
        res_naver.encoding = 'utf-8'
        naver_html = res_naver.text
        soup = BeautifulSoup(naver_html, 'html.parser')
        h4 = soup.select_one('h4.h_sub.sub_tit7')
        if h4:
            a_tag = h4.find('a')
            if a_tag:
                sector = a_tag.text.strip()
    except Exception:
        pass

    try:
        res_fin = requests.get(url_fin, headers=HEADERS, timeout=6)
        res_fin.encoding = 'utf-8'
        tables_fin = pd.read_html(io.StringIO(res_fin.text))

        df_bs = df_bs_q = df_is = df_is_q = None
        for tbl in tables_fin:
            if tbl.empty or len(tbl.columns) < 2:
                continue
            row_names = [str(r.iloc[0]).replace('\xa0', ' ').replace(' ', '').strip()
                         for _, r in tbl.iterrows()]
            has_equity = any(nm in ('자본', '자본총계') for nm in row_names)
            has_debt = any(nm in ('부채', '부채총계') for nm in row_names)
            has_op = any('영업이익' in nm for nm in row_names)
            has_net = any('지배주주순이익' in nm or '당기순이익' in nm for nm in row_names)
            yr_cols = [c for c in tbl.columns[1:] if '/' in str(c) and '전년동기' not in str(c)]
            dec_count = sum(1 for c in yr_cols if str(c).endswith('/12') or str(c).endswith('.12'))
            if has_equity and has_debt:
                if df_bs is None: df_bs = tbl
                elif df_bs_q is None: df_bs_q = tbl
            elif has_op and has_net and yr_cols:
                if dec_count >= len(yr_cols) * 0.5 and df_is is None:
                    df_is = tbl
                elif df_is_q is None:
                    df_is_q = tbl

        if df_bs is None:
            return None

        _bs_recent = df_bs_q if df_bs_q is not None else df_bs
        _bsq_cols = [j for j, c in enumerate(_bs_recent.columns)
                     if j > 0 and '/' in str(c) and '전년동기' not in str(c)]
        for _, row in _bs_recent.iterrows():
            nm = str(row.iloc[0]).replace('\xa0', ' ').replace(' ', '').strip()
            if nm in ['자본', '자본총계'] and t_equity == 0:
                for j in reversed(_bsq_cols):
                    vf = safe_float(row.iloc[j])
                    if vf is not None: t_equity = vf * 100000000; break
            if nm in ['부채', '부채총계'] and t_debt == 0:
                for j in reversed(_bsq_cols):
                    vf = safe_float(row.iloc[j])
                    if vf is not None: t_debt = vf * 100000000; break
            if '유동부채' in nm and '비유동' not in nm and c_liab == 0:
                for j in reversed(_bsq_cols):
                    vf = safe_float(row.iloc[j])
                    if vf is not None: c_liab = vf * 100000000; break

        bs_annual_cols = [j for j, c in enumerate(df_bs.columns)
                          if j > 0 and (str(c).endswith('/12') or str(c).endswith('.12'))
                          and '전년동기' not in str(c)]
        if not bs_annual_cols:
            bs_annual_cols = [j for j, c in enumerate(df_bs.columns)
                              if j > 0 and '/' in str(c) and '전년동기' not in str(c)]
        t_equity_annual = 0.0
        t_equity_past_annual = 0.0
        for _, row in df_bs.iterrows():
            nm = str(row.iloc[0]).replace('\xa0', ' ').replace(' ', '').strip()
            if nm in ['자본', '자본총계']:
                for j in reversed(bs_annual_cols):
                    vf = safe_float(row.iloc[j])
                    if vf is not None: t_equity_annual = vf * 100000000; break
                if len(bs_annual_cols) >= 2:
                    vf2 = safe_float(row.iloc[bs_annual_cols[-2]])
                    if vf2 is not None: t_equity_past_annual = vf2 * 100000000
                break
        if t_equity_annual == 0: t_equity_annual = t_equity
        if t_equity_past_annual == 0: t_equity_past_annual = t_equity_annual
    except Exception:
        return None

    # ── 네이버 주요재무정보(FnGuide 컨센서스): EPS/BPS/영업이익 ──
    _main_ok = False
    try:
        if naver_html is None:
            res_naver2 = requests.get(f"https://finance.naver.com/item/main.naver?code={ticker}", headers=HEADERS, timeout=10)
            res_naver2.encoding = 'utf-8'
            naver_html = res_naver2.text

        df_fh = None
        for _tbl in pd.read_html(io.StringIO(naver_html)):
            try:
                _cells = [str(r.iloc[0]).replace(' ', '') for _, r in _tbl.iterrows()]
            except Exception:
                continue
            if any('EPS(원)' in c for c in _cells) and any('BPS(원)' in c for c in _cells):
                df_fh = _tbl
                break

        if df_fh is not None:
            annual_cols = []
            for j, c in enumerate(df_fh.columns):
                parts = [str(x) for x in (c if isinstance(c, tuple) else (c,))]
                joined = ' '.join(parts)
                if j == 0 or '연간' not in joined:
                    continue
                m_y = re.search(r'(\d{4}\.\d{2})', joined)
                if not m_y:
                    continue
                annual_cols.append((j, m_y.group(1), '(E)' in joined))
            annual_cols.sort(key=lambda x: x[1], reverse=True)

            def _pick_annual(row):
                vals = []
                for j, yymm, is_est in annual_cols:
                    v = safe_float(row.iloc[j])
                    if v is None:
                        continue
                    vals.append((yymm, is_est, v))
                if not vals:
                    return None, False, None
                ests = [t for t in vals if t[1]]
                chosen = min(ests, key=lambda t: t[0]) if ests else vals[0]
                older_act = [t for t in vals if not t[1] and t[0] < chosen[0]]
                past = older_act[0][2] if older_act else chosen[2]
                return chosen[2], chosen[1], past

            if annual_cols:
                for _, row in df_fh.iterrows():
                    nm = str(row.iloc[0]).replace('\xa0', ' ').replace(' ', '').strip()
                    if nm == 'EPS(원)' and a_eps == 0:
                        _v, _e, _pv = _pick_annual(row)
                        if _v is not None and _v != 0:
                            a_eps = _v; is_future_eps = _e; past_eps = _pv if _pv is not None else _v
                    elif nm == 'BPS(원)' and bps == 0:
                        _v, _, _pv = _pick_annual(row)
                        if _v is not None and _v > 0:
                            bps = _v; past_bps = _pv if _pv is not None and _pv > 0 else _v
                    elif nm == '영업이익' and op_profit == 0:
                        _v, _, _ = _pick_annual(row)
                        if _v is not None and _v != 0:
                            op_profit = _v

        if bps == 0 and shares > 0:
            _bps_eq = t_equity_annual if t_equity_annual > 0 else t_equity
            _bps_eq_p = t_equity_past_annual if t_equity_past_annual > 0 else _bps_eq
            if _bps_eq > 0:
                bps = _bps_eq / shares
                past_bps = (_bps_eq_p / shares) if _bps_eq_p > 0 else bps

        if a_eps != 0 and bps != 0:
            _main_ok = True
    except Exception:
        pass

    # ── 네이버 실패 시 SVD_Finance 연간IS 폴백 ──
    if not _main_ok and df_is is not None and shares > 0:
        _cur_year2 = datetime.datetime.now().year
        _iy_cols = [j for j, c in enumerate(df_is.columns)
                    if j > 0 and str(c).strip().endswith('/12') and '전년동기' not in str(c)]
        _row_gov = _row_net = None
        for _, row in df_is.iterrows():
            nm = str(row.iloc[0]).replace('\xa0', ' ').replace(' ', '').strip()
            if '영업이익' in nm and '발표기준' not in nm and op_profit == 0:
                for j in reversed(_iy_cols):
                    vf = safe_float(row.iloc[j])
                    if vf: op_profit = vf; break
            if '비지배' in nm: continue
            if '지배주주순이익' in nm and _row_gov is None: _row_gov = row
            elif nm == '당기순이익' and _row_net is None: _row_net = row
        _ni_row = _row_gov if _row_gov is not None else _row_net
        if _ni_row is not None and a_eps == 0:
            _y_vals = []
            for j in reversed(_iy_cols):
                vf = safe_float(_ni_row.iloc[j])
                if vf is not None: _y_vals.append((vf, str(df_is.columns[j])))
            if _y_vals:
                _v0, _c0 = _y_vals[0]
                a_eps = (_v0 * 100000000) / shares
                try: is_future_eps = int(_c0.split('/')[0].strip()) > _cur_year2
                except Exception: is_future_eps = False
                past_eps = (_y_vals[1][0] * 100000000) / shares if len(_y_vals) >= 2 else a_eps
        if bps == 0:
            _bps_eq = t_equity_annual if t_equity_annual > 0 else t_equity
            if _bps_eq > 0:
                bps = _bps_eq / shares
        if a_eps != 0 and bps != 0:
            _main_ok = True

    if past_bps == 0:
        past_bps = bps
    if a_eps == 0 or bps == 0 or t_equity == 0:
        return None

    d_ratio = (t_debt / t_equity) * 100
    pnlty = ((t_debt - t_equity) / shares) if d_ratio > 100 else 0
    p10 = (a_eps * 10) + bps - pnlty
    p15 = (a_eps * 15) + bps - pnlty
    t10 = (a_eps * 10) + bps - (c_liab / shares)
    t15 = (a_eps * 15) + bps - (c_liab / shares)

    return {
        "code": ticker, "market": market, "name": name, "marcap_rank": marcap_rank,
        "current_price": current_price,
        "fair10": round(p10, 2), "target10": round(t10, 2),
        "gap10": round(((p10 - current_price) / current_price) * 100, 2) if p10 > 0 and current_price else 0,
        "fair15": round(p15, 2), "target15": round(t15, 2),
        "sector": sector, "eps": round(a_eps, 2), "eps_est": bool(is_future_eps),
        "bps": round(bps, 2), "debt_ratio": round(d_ratio, 2),
        "op_profit": round(op_profit, 2),
        "total_debt": t_debt, "current_liab": c_liab, "total_equity": t_equity,
        "shares": shares,
    }


def upload(results):
    payload = {"action": "admin_save_stage1_results", "admin_token": ADMIN_TOKEN,
               "results": results, "scan_at": datetime.datetime.utcnow().isoformat() + "Z"}
    resp = requests.post(SUPABASE_FUNCTIONS_URL, json=payload,
                         headers={"Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                                  "Content-Type": "application/json"}, timeout=60)
    print("upload:", resp.status_code, resp.text[:300])
    resp.raise_for_status()


def main():
    if not ADMIN_TOKEN:
        print("ERROR: ADMIN_TOKEN 환경변수가 필요합니다."); sys.exit(1)

    t0 = time.time()
    universe = []
    for sosok, mkt in [(0, "KOSPI"), (1, "KOSDAQ")]:
        rows = collect_market(sosok)
        for rank, r in enumerate(rows, start=1):
            r["Market"] = mkt; r["Rank"] = rank
        universe.extend(rows)
        print(f"{mkt}: {len(rows)} 종목 수집")

    print(f"총 {len(universe)} 종목 분석 시작...")
    results = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(analyze_stock, r["Code"], r["Name"], r["Market"],
                          r["Close"], r["Stocks"], r["Rank"]): r for r in universe}
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  진행 {done}/{len(universe)} ... (유효 {len(results)})")
            try:
                d = fut.result()
                if d:
                    results.append(d)
            except Exception:
                pass

    # 시장+시총순위로 정렬
    results.sort(key=lambda d: (d["market"], d["marcap_rank"]))
    print(f"분석 완료: 유효 {len(results)}종목 / 소요 {time.time()-t0:.0f}s")
    upload(results)
    print("완료.")


if __name__ == "__main__":
    main()
