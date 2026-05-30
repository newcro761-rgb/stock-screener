"""
JB_상승장 스크리너 (GitHub Actions 버전)
==========================================
트리거: KOSPI 직전 60거래일 고점을 양봉 종가로 돌파한 날

필터 조건:
  1. KOSPI 60일 고점 양봉 돌파 (트리거)
  2. 등락률 +8% 이상
  3. 거래대금 2,000억 이상
  4. 주가 1,000원 이상
  5. 돌파봉 거래대금 >= 5일 평균 1.5배
  6. ETF/ETN/스팩 제외
  7. 우선주 제외
  8. 관리종목/거래정지 제외

매일 16:10 KST 자동 실행 → 텔레그램 전송
"""
import requests, re, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# ── 설정 ──────────────────────────────────────────────────────
TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT  = int(os.environ.get('TG_CHAT', '0'))

MIN_RISE_PCT  = 8.0
MIN_TRADE_AMT = 200_000_000_000   # 2,000억
MIN_PRICE     = 1_000
VOL_RATIO     = 1.5
WORKERS       = 10

H = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com'}

EXCLUDE_KW = [
    'KODEX','TIGER','RISE','ACE','HANARO','KIWOOM','SOL','WON','PLUS',
    'TIME','FOCUS','TREX','DAISHIN','UNICORN','KoAct','IBK','HK',
    '마이티','파워','1Q','ETN','스팩','SPAC',
]

def is_excluded(name):
    return any(k.upper() in name.upper() for k in EXCLUDE_KW)

def is_preferred(name):
    return bool(re.search(r'[가-힣]\d?우[A-Z]?$', name))

# ── 텔레그램 ──────────────────────────────────────────────────
def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
    except Exception as e:
        print(f'텔레그램 오류: {e}')

# ── KOSPI 60일 고점 돌파 체크 ─────────────────────────────────
def check_kospi():
    rows = []
    for page in range(1, 15):
        try:
            r = requests.get('https://finance.naver.com/sise/sise_index_day.nhn',
                params={'code': 'KOSPI', 'page': page}, headers=H, timeout=8)
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
        except:
            break
    if len(rows) < 62:
        return False, 0, 0
    today_close, today_rate = rows[0][1], rows[0][2]
    high_60 = max(r[1] for r in rows[1:62])
    broke   = today_close > high_60 and today_rate > 0
    rate    = round((today_close / high_60 - 1) * 100, 2) if high_60 else 0
    return broke, today_close, rate

# ── 블랙리스트 ────────────────────────────────────────────────
def fetch_bl(url):
    try:
        r = requests.get(url, headers=H, timeout=10)
        r.encoding = 'euc-kr'
        return set(re.findall(r'code=(\d{6})', r.text))
    except:
        return set()

def blacklist():
    bl  = fetch_bl('https://finance.naver.com/sise/management.naver')
    bl |= fetch_bl('https://finance.naver.com/sise/trading_halt.naver')
    return bl

# ── 급등 종목 수집 ────────────────────────────────────────────
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
            items.append({'code': code, 'name': name,
                          'price': price, 'pct': pct,
                          'trade_amt': price * volume})
        except:
            continue
    return items

def get_surge(bl):
    stocks = []
    for sosok in [0, 1]:
        for page in range(1, 30):
            try:
                r = requests.get('https://finance.naver.com/sise/sise_rise.nhn',
                    params={'sosok': sosok, 'page': page}, headers=H, timeout=8)
                r.encoding = 'euc-kr'
                items = parse_rise_page(r.text)
                if not items: break
                stop = False
                for it in items:
                    if it['pct'] < MIN_RISE_PCT:
                        stop = True; break
                    if (it['code'] not in bl
                            and not is_excluded(it['name'])
                            and not is_preferred(it['name'])
                            and it['price'] >= MIN_PRICE):
                        stocks.append(it)
                if stop: break
                time.sleep(0.05)
            except:
                break
    seen, unique = set(), []
    for s in stocks:
        if s['code'] not in seen:
            seen.add(s['code']); unique.append(s)
    return unique

# ── 5일 평균 1.5배 체크 ───────────────────────────────────────
def check_vol(code, trade_amt):
    try:
        r = requests.get('https://finance.naver.com/item/sise_day.nhn',
            params={'code': code, 'page': 1}, headers=H, timeout=8)
        r.encoding = 'euc-kr'
        nums = re.findall(r'class="num"><span[^>]*>([\d,]+)</span>', r.text)
        rows = []
        for i in range(0, len(nums)-4, 5):
            try: rows.append([int(nums[i+j].replace(',','')) for j in range(5)])
            except: pass
        if len(rows) < 6: return True
        avg5 = sum(r[0]*r[4] for r in rows[1:6]) / 5
        return avg5 == 0 or trade_amt >= VOL_RATIO * avg5
    except:
        return True

# ── Main ─────────────────────────────────────────────────────
def main():
    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d (%a)')

    if today.weekday() >= 5:
        print('주말 스킵')
        return

    print(f'=== JB_상승장 | {today_str} ===')

    # KOSPI 체크
    broke, kospi_close, kospi_rate = check_kospi()
    if not broke:
        msg = (f'📊 <b>JB_상승장</b> | {today_str}\n'
               f'⚪ KOSPI 60일 고점 돌파 미충족\n'
               f'   (상승장 조건 해당 없음)')
        tg(msg)
        print('KOSPI 미돌파 — 전송 후 종료')
        return

    print(f'KOSPI 돌파! {kospi_close:,.2f} (+{kospi_rate}%)')
    tg(f'🔍 JB_상승장 스크리닝 시작... ({today_str})\n'
       f'✅ KOSPI 60일 고점 돌파 {kospi_close:,.2f} (+{kospi_rate}%)')

    # 블랙리스트
    bl = blacklist()
    print(f'블랙리스트: {len(bl)}개')

    # 급등 수집
    surge = get_surge(bl)
    print(f'+8% 기본 필터: {len(surge)}개')

    # 2000억 + 5일 1.5배
    filtered = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check_vol, s['code'], s['trade_amt']): s
                for s in surge if s['trade_amt'] >= MIN_TRADE_AMT}
        for f in as_completed(futs):
            if f.result():
                filtered.append(futs[f])

    filtered.sort(key=lambda x: -x['pct'])
    print(f'최종 통과: {len(filtered)}개')

    # 텔레그램 전송
    if not filtered:
        tg(f'📊 <b>JB_상승장</b> | {today_str}\n'
           f'✅ KOSPI 돌파 확인\n조건 충족 종목 없음')
        return

    lines = [
        f'📊 <b>JB_상승장</b> | {today_str}',
        f'✅ KOSPI 60일 고점 돌파 (+{kospi_rate}%)',
        f'📋 필터 통과 <b>{len(filtered)}종목</b> '
        f'(+8% / 2000억↑ / 5일대비1.5배↑)',
        '──────────────────────'
    ]
    for s in filtered:
        lines.append(
            f"<b>{s['name']}</b>  +{s['pct']:.2f}%\n"
            f"  {s['price']:,}원 | {s['trade_amt']/1e8:.0f}억"
        )
        if sum(len(l) for l in lines) > 3500:
            tg('\n'.join(lines)); lines = []

    if lines:
        tg('\n'.join(lines))
    tg('✅ 완료')

if __name__ == '__main__':
    main()
