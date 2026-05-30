"""
보합+박스장 돌파매매 스크리너 (GitHub Actions 버전)
평일 16:10 KST 자동 실행 → 텔레그램 전송

[필터 조건]
기본 제외: ETF/ETN/스팩, 우선주, 관리종목, 투자경고/위험, 거래정지
           공시 워크아웃/횡령/배임 키워드
재무 제외: 4개 손익항목 중 하나라도 조회기간 전체 마이너스
           자본금 N/A 또는 자본잠식
기술 조건: 양봉 + 60/120일 고점 돌파 + 거래대금 30억↑ + 5일대비 1.5배↑
"""
import requests, re, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import pandas as pd

TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT  = int(os.environ.get('TG_CHAT', '0'))

CHECK_DAYS  = [60, 120]
MIN_PRICE   = 1_000
MIN_TRADING = 3_000_000_000
VOL_RATIO   = 1.5
WORKERS     = 10
DART_DAYS   = 180

H_NAVER  = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com'}
H_DART   = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://dart.fss.or.kr'}
H_FNGUID = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://comp.fnguide.com'}

EXCLUDE_KW = [
    'KODEX','TIGER','RISE','ACE','HANARO','KIWOOM','SOL','WON','PLUS',
    'TIME','FOCUS','TREX','DAISHIN','UNICORN','KoAct','IBK','HK',
    '마이티','파워','1Q','ETN','스팩','SPAC',
]
DART_KW = ['워크아웃', '횡령', '배임']
FIN_ROWS = ['매출액', '영업이익', '영업이익(발표기준)', '당기순이익']

# ══ 텔레그램 ════════════════════════════════════════════════════
def tg(text):
    if not TG_TOKEN or not TG_CHAT: print(text); return
    try:
        requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except: pass

# ══ 기본 필터 ══════════════════════════════════════════════════
def is_excluded(name):
    return any(k.upper() in name.upper() for k in EXCLUDE_KW)

def is_preferred(name):
    return bool(re.search(r'[가-힣]\d?우[A-Z]?$', name))

def fetch_bl(url):
    try:
        r = requests.get(url, headers=H_NAVER, timeout=10)
        r.encoding = 'euc-kr'
        return set(re.findall(r'code=(\d{6})', r.text))
    except: return set()

def build_blacklist():
    bl  = fetch_bl('https://finance.naver.com/sise/management.naver')
    bl |= fetch_bl('https://finance.naver.com/sise/investment_alert.naver?type=warning')
    bl |= fetch_bl('https://finance.naver.com/sise/investment_alert.naver?type=danger')
    bl |= fetch_bl('https://finance.naver.com/sise/trading_halt.naver')
    return bl

def has_bad_dart(code, name):
    try:
        start = (datetime.today() - timedelta(days=DART_DAYS)).strftime('%Y%m%d')
        r = requests.get('https://dart.fss.or.kr/dsab001/search.ax',
            params={'textCrpNm': name, 'startDt': start, 'selectKey': 'ALL'},
            headers=H_DART, timeout=10)
        text = r.content.decode('utf-8', errors='replace')
        return any(kw in text for kw in DART_KW)
    except: return False

# ══ 재무 필터 (FnGuide) ════════════════════════════════════════
def _parse_fin_table(tbl):
    result = {}
    rows = tbl.find_all('tr')
    if not rows: return result
    header = [c.get_text(strip=True) for c in rows[0].find_all(['th','td'])]
    n_data = max(len(header) - 3, 1)
    for row in rows[1:]:
        cells = row.find_all(['th','td'])
        if not cells: continue
        name = re.sub(r'계산에 참여한 계정 펼치기', '', cells[0].get_text(strip=True)).strip()
        if not name: continue
        vals = []
        for cell in cells[1: n_data + 1]:
            txt = cell.get_text(strip=True).replace(',','').replace(' ','')
            if txt in ('','-','N/A','n/a'): vals.append(None)
            else:
                try: vals.append(float(txt))
                except: vals.append(None)
        if name not in result:
            result[name] = vals
    return result

def check_financials(code):
    """Returns (status, reason): 'pass'|'fail'|'확인필요', str"""
    try:
        r = requests.get('https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp',
            params={'pGB':'1','gicode':f'A{code}','cID':'','MenuYn':'Y',
                    'ReportGB':'D','NewMenuID':'134','stkGb':'701'},
            headers=H_FNGUID, timeout=12)
        r.encoding = 'utf-8'
        if r.status_code != 200: return '확인필요', f'HTTP{r.status_code}'
        soup = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table')
        if len(tables) < 3: return '확인필요', '재무없음'

        ann = _parse_fin_table(tables[0])
        qtr = _parse_fin_table(tables[1])
        bal = _parse_fin_table(tables[2])

        # 신규상장 체크
        new_ipo = len([v for v in ann.get('매출액',[]) if v is not None]) < 2

        # 조건 A: 손익행 전체 마이너스
        for rn in FIN_ROWS:
            a3 = [v for v in ann.get(rn,[])[-3:] if v is not None]
            q3 = [v for v in qtr.get(rn,[])[-3:] if v is not None]
            vals = a3 + q3
            if vals and all(v < 0 for v in vals):
                rsn = f'{rn} 전기간 마이너스'
                return ('확인필요', f'신규상장+{rsn}') if new_ipo else ('fail', rsn)

        # 조건 B: 자본잠식
        if not [v for v in bal.get('자본금',[]) if v is not None]:
            return 'fail', '자본금N/A(자본잠식)'
        eq_recent = [v for v in bal.get('자본',[]) if v is not None][-2:]
        if eq_recent and any(v <= 0 for v in eq_recent):
            return 'fail', '자본잠식(자본음수)'

        return ('확인필요','신규상장통과') if new_ipo else ('pass','재무이상없음')
    except Exception as e:
        return '확인필요', f'조회오류:{str(e)[:30]}'

# ══ 종목 데이터 수집 ═══════════════════════════════════════════
def get_all_codes():
    all_stocks = []
    for sosok, pages, market in [(0,50,'KOSPI'),(1,37,'KOSDAQ')]:
        print(f'  {market} 수집중...')
        for page in range(1, pages+1):
            try:
                r = requests.get('https://finance.naver.com/sise/sise_market_sum.nhn',
                    params={'sosok':sosok,'page':page}, headers=H_NAVER, timeout=8)
                r.encoding = 'euc-kr'
                for m in re.finditer(
                    r'href="[^"]*code=(\d{6})"[^>]*class="tltle">([^<]+)</a>.*?'
                    r'class="number">([\d,]+)</td>', r.text, re.S):
                    code,name = m.group(1), m.group(2).strip()
                    try: close = int(m.group(3).replace(',',''))
                    except: continue
                    all_stocks.append((code,name,close))
                time.sleep(0.03)
            except: pass
    seen, unique = set(), []
    for s in all_stocks:
        if s[0] not in seen:
            seen.add(s[0]); unique.append(s)
    return unique

def parse_sise_day(code, page):
    r = requests.get('https://finance.naver.com/item/sise_day.nhn',
        params={'code':code,'page':page}, headers=H_NAVER, timeout=8)
    r.encoding = 'euc-kr'
    nums = re.findall(r'class="num"><span[^>]*>([\d,]+)</span>', r.text)
    rows = []
    for i in range(0, len(nums)-4, 5):
        try: rows.append([int(nums[i+j].replace(',','')) for j in range(5)])
        except: pass
    return rows

MAX_DAYS = max(CHECK_DAYS)

def check_stock(code, name):
    try:
        rows1 = parse_sise_day(code, 1)
        if len(rows1) < 6: return {}
        close,open_,_,_,vol = rows1[0]
        if open_==0 or close==0 or vol==0: return {}
        if close < MIN_PRICE or close <= open_: return {}
        tv = close * vol
        if tv < MIN_TRADING: return {}
        prev5 = rows1[1:6]
        if len(prev5) < 5: return {}
        avg5 = sum(r[0]*r[4] for r in prev5) / 5
        if avg5 > 0 and tv < VOL_RATIO * avg5: return {}
        all_highs = [r[2] for r in rows1[1:]]
        if not all_highs or close <= max(all_highs): return {}
        for pg in range(2, (MAX_DAYS//10)+3):
            rows = parse_sise_day(code, pg)
            if not rows: break
            if any(r[4]==0 for r in rows): return {}
            all_highs.extend([r[2] for r in rows])
            if len(all_highs) >= MAX_DAYS: break
        if len(all_highs) < MAX_DAYS//2: return {}
        base = {'종목코드':code,'종목명':name,'오늘종가':close,
                '거래대금(억)':round(tv/1e8,1),'5일평균대비':round(tv/avg5,2) if avg5 else 0}
        results = {}
        for nd in CHECK_DAYS:
            h = all_highs[:nd]
            if not h: continue
            hn = max(h)
            if hn > 0 and close > hn:
                results[nd] = {**base, f'{nd}일고점':hn, '돌파율(%)':round((close/hn-1)*100,2)}
        return results
    except: return {}

# ══ 텔레그램 결과 포맷 ══════════════════════════════════════════
def format_result(r, fin_status, fin_reason):
    if fin_status == 'pass':
        mark = '✅'
        note = '재무이상없음'
    elif fin_status == '확인필요':
        mark = '⚠️'
        note = fin_reason
    else:
        mark = '❌'
        note = fin_reason
    return (f"{mark} <b>{r['종목명']}</b>  +{r['돌파율(%)']}%\n"
            f"  {r['오늘종가']:,}원 | {r['거래대금(억)']}억 | {note}")

def send_results(bucket, today_str, excluded):
    tg(f'📊 <b>보합+박스장 돌파매매</b>\n📅 {today_str}\n──────────────────────')
    for nd in CHECK_DAYS:
        rows = sorted(bucket.get(nd,[]), key=lambda x: -x['돌파율(%)'])
        tg(f'🔵 <b>{nd}일 고점 돌파 ({len(rows)}개)</b>')
        if not rows:
            tg('없음'); continue
        lines = []
        for r in rows:
            lines.append(format_result(r, r['fin_status'], r['fin_reason']))
            if sum(len(l) for l in lines) > 3000:
                tg('\n'.join(lines)); lines = []
        if lines: tg('\n'.join(lines))

    if excluded:
        tg(f'❌ <b>재무필터 제외 {len(excluded)}종목</b>\n' +
           '\n'.join(f"  {e['name']} | {e['reason']}" for e in excluded[:10]))
    tg('✅ 완료')

# ══ Main ════════════════════════════════════════════════════════
def main():
    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d (%a)')
    if today.weekday() >= 5: print('주말 스킵'); return

    print(f'=== 보합+박스장 | {today_str} ===')
    tg(f'🔍 보합+박스장 스크리닝 시작... ({today_str})')

    bl = build_blacklist()
    print(f'블랙리스트: {len(bl)}개')

    all_stocks = get_all_codes()
    filtered_basic = [(c,n,p) for c,n,p in all_stocks
                      if p >= MIN_PRICE and not is_excluded(n)
                      and not is_preferred(n) and c not in bl]
    print(f'기본필터 통과: {len(filtered_basic)}개')

    # 기술적 조건 (60/120일 돌파 + 양봉 + 거래대금)
    bucket = {nd: [] for nd in CHECK_DAYS}
    tech_pass = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check_stock, c, n): (c,n) for c,n,_ in filtered_basic}
        for f in as_completed(futs):
            res = f.result()
            for nd, row in res.items():
                bucket[nd].append(row)
                tech_pass[row['종목코드']] = row['종목명']

    print(f'기술적 조건 통과: {len(tech_pass)}개 종목')

    # DART 공시 체크
    bad_dart = set()
    for code, name in tech_pass.items():
        if has_bad_dart(code, name):
            print(f'  DART 제외: {name}')
            bad_dart.add(code)
    for nd in CHECK_DAYS:
        bucket[nd] = [r for r in bucket[nd] if r['종목코드'] not in bad_dart]

    # 재무 필터
    fin_cache = {}
    excluded_fin = []
    for code, name in tech_pass.items():
        if code in bad_dart: continue
        status, reason = check_financials(code)
        fin_cache[code] = (status, reason)
        print(f'  재무: {name} → {status} | {reason}')
        if status == 'fail':
            excluded_fin.append({'name': name, 'reason': reason})

    # 재무 실패 종목 제거, fin_status 태그 추가
    for nd in CHECK_DAYS:
        passed = []
        for r in bucket[nd]:
            st, rsn = fin_cache.get(r['종목코드'], ('확인필요','미조회'))
            if st == 'fail': continue
            r['fin_status'] = st
            r['fin_reason'] = rsn
            passed.append(r)
        bucket[nd] = passed

    # 최종 결과 출력
    for nd in CHECK_DAYS:
        rows = sorted(bucket[nd], key=lambda x: -x['돌파율(%)'])
        print(f'\n▶ {nd}일 돌파 ({len(rows)}개)')
        for r in rows:
            mark = '✅' if r['fin_status']=='pass' else '⚠️'
            print(f"  {mark} {r['종목명']} +{r['돌파율(%)']}% | {r['거래대금(억)']}억 | {r['fin_reason']}")

    send_results(bucket, today_str, excluded_fin)

if __name__ == '__main__':
    main()
