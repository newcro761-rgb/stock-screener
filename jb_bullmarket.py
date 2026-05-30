"""
JB_상승장 스크리너 (GitHub Actions 버전)
평일 16:10 KST 자동 실행 → 텔레그램 전송

[트리거] KOSPI 직전 60거래일 고점을 양봉 종가로 돌파한 날

[필터 조건]
기본 제외: ETF/ETN/스팩, 우선주, 관리종목, 투자경고/위험, 거래정지
           공시 워크아웃/횡령/배임 키워드
재무 제외: 4개 손익항목 중 하나라도 조회기간 전체 마이너스
           자본금 N/A 또는 자본잠식
기술 조건: +8% 이상 + 거래대금 2000억↑ + 5일대비 1.5배↑
"""
import requests, re, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT  = int(os.environ.get('TG_CHAT', '0'))

MIN_RISE_PCT  = 8.0
MIN_TRADE_AMT = 200_000_000_000
MIN_PRICE     = 1_000
VOL_RATIO     = 1.5
WORKERS       = 10
DART_DAYS     = 180

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
        for cell in cells[1: n_data+1]:
            txt = cell.get_text(strip=True).replace(',','').replace(' ','')
            if txt in ('','-','N/A','n/a'): vals.append(None)
            else:
                try: vals.append(float(txt))
                except: vals.append(None)
        if name not in result:
            result[name] = vals
    return result

def check_financials(code):
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

        new_ipo = len([v for v in ann.get('매출액',[]) if v is not None]) < 2

        for rn in FIN_ROWS:
            a3 = [v for v in ann.get(rn,[])[-3:] if v is not None]
            q3 = [v for v in qtr.get(rn,[])[-3:] if v is not None]
            vals = a3 + q3
            if vals and all(v < 0 for v in vals):
                rsn = f'{rn} 전기간 마이너스'
                return ('확인필요', f'신규상장+{rsn}') if new_ipo else ('fail', rsn)

        if not [v for v in bal.get('자본금',[]) if v is not None]:
            return 'fail', '자본금N/A(자본잠식)'
        eq_recent = [v for v in bal.get('자본',[]) if v is not None][-2:]
        if eq_recent and any(v <= 0 for v in eq_recent):
            return 'fail', '자본잠식(자본음수)'

        return ('확인필요','신규상장통과') if new_ipo else ('pass','재무이상없음')
    except Exception as e:
        return '확인필요', f'조회오류:{str(e)[:30]}'

# ══ KOSPI 60일 고점 체크 ═══════════════════════════════════════
def check_kospi():
    rows = []
    for page in range(1, 15):
        try:
            r = requests.get('https://finance.naver.com/sise/sise_index_day.nhn',
                params={'code':'KOSPI','page':page}, headers=H_NAVER, timeout=8)
            r.encoding = 'euc-kr'
            dates  = re.findall(r'(\d{4}\.\d{2}\.\d{2})', r.text)
            closes = re.findall(r'class="number_1">([\d,]+\.\d+)', r.text)
            rates  = re.findall(r'tah p11[^"]*">\s*[+-]?([\d.]+)%', r.text)
            if not dates: break
            for i, d in enumerate(dates):
                close = float(closes[i].replace(',','')) if i < len(closes) else 0
                rate  = float(rates[i]) if i < len(rates) else 0
                rows.append((d, close, rate))
            time.sleep(0.05)
        except: break
    if len(rows) < 62: return False, 0, 0
    today_close, today_rate = rows[0][1], rows[0][2]
    high_60 = max(r[1] for r in rows[1:62])
    broke   = today_close > high_60 and today_rate > 0
    rate    = round((today_close/high_60-1)*100, 2) if high_60 else 0
    return broke, today_close, rate

# ══ 급등 종목 수집 ══════════════════════════════════════════════
def parse_rise_page(html):
    items = []
    row_pat = re.compile(r'code=(\d{6})[^>]*class="tltle">([^<]+)</a>')
    num_pat = re.compile(r'class="number">([\d,]+)</td>')
    pct_pat = re.compile(r'tah p11[^"]*">\s*\+?([\d.]+)%')
    for m in row_pat.finditer(html):
        code, name = m.group(1), m.group(2).strip()
        seg = html[m.end(): m.end()+1200]
        nums = num_pat.findall(seg)
        pm   = pct_pat.search(seg)
        if len(nums) < 2 or not pm: continue
        try:
            price  = int(nums[0].replace(',',''))
            volume = int(nums[1].replace(',',''))
            pct    = float(pm.group(1))
            items.append({'code':code,'name':name,'price':price,
                          'pct':pct,'trade_amt':price*volume})
        except: continue
    return items

def get_surge(bl):
    stocks = []
    for sosok in [0, 1]:
        for page in range(1, 30):
            try:
                r = requests.get('https://finance.naver.com/sise/sise_rise.nhn',
                    params={'sosok':sosok,'page':page}, headers=H_NAVER, timeout=8)
                r.encoding = 'euc-kr'
                items = parse_rise_page(r.text)
                if not items: break
                stop = False
                for it in items:
                    if it['pct'] < MIN_RISE_PCT: stop=True; break
                    if (it['code'] not in bl and not is_excluded(it['name'])
                            and not is_preferred(it['name'])
                            and it['price'] >= MIN_PRICE):
                        stocks.append(it)
                if stop: break
                time.sleep(0.05)
            except: break
    seen, unique = set(), []
    for s in stocks:
        if s['code'] not in seen:
            seen.add(s['code']); unique.append(s)
    return unique

def check_vol(code, trade_amt):
    try:
        r = requests.get('https://finance.naver.com/item/sise_day.nhn',
            params={'code':code,'page':1}, headers=H_NAVER, timeout=8)
        r.encoding = 'euc-kr'
        nums = re.findall(r'class="num"><span[^>]*>([\d,]+)</span>', r.text)
        rows = []
        for i in range(0, len(nums)-4, 5):
            try: rows.append([int(nums[i+j].replace(',','')) for j in range(5)])
            except: pass
        if len(rows) < 6: return True
        avg5 = sum(r[0]*r[4] for r in rows[1:6]) / 5
        return avg5==0 or trade_amt >= VOL_RATIO * avg5
    except: return True

# ══ Main ════════════════════════════════════════════════════════
def main():
    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d (%a)')
    if today.weekday() >= 5: print('주말 스킵'); return

    print(f'=== JB_상승장 | {today_str} ===')

    broke, kospi_close, kospi_rate = check_kospi()
    if not broke:
        tg(f'📊 <b>JB_상승장</b> | {today_str}\n'
           f'⚪ KOSPI 60일 고점 돌파 미충족 (상승장 조건 해당없음)')
        return

    print(f'KOSPI 돌파: {kospi_close:,.2f} (+{kospi_rate}%)')
    tg(f'🔍 JB_상승장 스크리닝 시작 ({today_str})\n'
       f'✅ KOSPI 60일 고점 돌파 {kospi_close:,.2f} (+{kospi_rate}%)')

    bl = build_blacklist()
    surge = get_surge(bl)
    print(f'+8% 기본필터: {len(surge)}개')

    # 거래대금 2000억 + 5일 1.5배
    filtered = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check_vol, s['code'], s['trade_amt']): s
                for s in surge if s['trade_amt'] >= MIN_TRADE_AMT}
        for f in as_completed(futs):
            if f.result(): filtered.append(futs[f])
    filtered.sort(key=lambda x: -x['pct'])
    print(f'거래대금+5일대비 통과: {len(filtered)}개')

    # DART 체크
    bad_dart = {s['code'] for s in filtered if has_bad_dart(s['code'], s['name'])}
    filtered = [s for s in filtered if s['code'] not in bad_dart]

    # 재무 필터
    excluded_fin = []
    final = []
    for s in filtered:
        status, reason = check_financials(s['code'])
        print(f'  재무: {s["name"]} → {status} | {reason}')
        if status == 'fail':
            excluded_fin.append(s['name'] + ' | ' + reason)
            continue
        s['fin_status'] = status
        s['fin_reason']  = reason
        final.append(s)

    print(f'최종 통과: {len(final)}개')

    # 텔레그램 전송
    if not final:
        tg(f'📊 <b>JB_상승장</b> | {today_str}\n✅ KOSPI 돌파 확인\n조건 충족 종목 없음')
    else:
        lines = [
            f'📊 <b>JB_상승장</b> | {today_str}',
            f'✅ KOSPI 60일 고점 돌파 (+{kospi_rate}%)',
            f'📋 필터 통과 <b>{len(final)}종목</b>',
            '──────────────────────'
        ]
        for s in final:
            mark = '✅' if s['fin_status']=='pass' else '⚠️'
            lines.append(
                f"{mark} <b>{s['name']}</b>  +{s['pct']:.2f}%\n"
                f"  {s['price']:,}원 | {s['trade_amt']/1e8:.0f}억 | {s['fin_reason']}"
            )
            if sum(len(l) for l in lines) > 3200:
                tg('\n'.join(lines)); lines = []
        if lines: tg('\n'.join(lines))

    if excluded_fin:
        tg('❌ <b>재무필터 제외</b>\n' + '\n'.join(f'  {e}' for e in excluded_fin[:10]))
    tg('✅ 완료')

if __name__ == '__main__':
    main()
