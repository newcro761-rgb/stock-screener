"""
보합+박스장 돌파매매 스크리너 (GitHub Actions 버전)
평일 16:10 KST 자동 실행 → 텔레그램 전송
"""
import requests, re, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pandas as pd

# ── 설정 (GitHub Secrets에서 읽음) ───────────────────────────
TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT  = int(os.environ.get('TG_CHAT', '0'))

CHECK_DAYS  = [60, 120]
MIN_PRICE   = 1_000
MIN_TRADING = 3_000_000_000
VOL_RATIO   = 1.5
WORKERS     = 10          # GitHub Actions 환경에 맞게 조정
DART_DAYS   = 180

H_NAVER = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com'}
H_DART  = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://dart.fss.or.kr'}

EXCLUDE_KW = [
    'KODEX','TIGER','RISE','ACE','HANARO','KIWOOM','SOL','WON','PLUS',
    'TIME','FOCUS','TREX','DAISHIN','UNICORN','KoAct','IBK','HK',
    '마이티','파워','1Q','ETN','스팩','SPAC',
]

# ── 텔레그램 ──────────────────────────────────────────────────
def tg_send(text):
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
    except Exception as e:
        print(f'텔레그램 전송 실패: {e}')

def tg_send_results(bucket, today_str):
    tg_send(
        f'📊 <b>보합+박스장 돌파매매</b>\n'
        f'📅 {today_str}\n'
        f'──────────────────────'
    )
    for nd in CHECK_DAYS:
        rows = sorted(bucket.get(nd, []), key=lambda x: -x['돌파율(%)'])
        header = f'🔵 <b>{nd}일 고점 돌파 ({len(rows)}개)</b>'
        if not rows:
            tg_send(f'{header}\n없음')
            continue
        lines = [header]
        for r in rows:
            line = (
                f"\n<b>{r['종목명']}</b>  +{r['돌파율(%)']}%\n"
                f"  종가 {r['오늘종가']:,}원 | "
                f"거래대금 {r['거래대금(억)']}억 | "
                f"5일대비 {r['5일평균대비']}배"
            )
            lines.append(line)
            if sum(len(l) for l in lines) > 3500:
                tg_send('\n'.join(lines))
                lines = []
        if lines:
            tg_send('\n'.join(lines))
    tg_send('✅ 스크리닝 완료')

# ── 블랙리스트 ────────────────────────────────────────────────
def fetch_codes(url):
    try:
        r = requests.get(url, headers=H_NAVER, timeout=10)
        r.encoding = 'euc-kr'
        return set(re.findall(r'code=(\d{6})', r.text))
    except:
        return set()

def build_blacklist():
    bl  = fetch_codes('https://finance.naver.com/sise/management.naver')
    bl |= fetch_codes('https://finance.naver.com/sise/investment_alert.naver?type=warning')
    bl |= fetch_codes('https://finance.naver.com/sise/investment_alert.naver?type=danger')
    bl |= fetch_codes('https://finance.naver.com/sise/trading_halt.naver')
    return bl

def is_excluded(name):
    n = name.upper()
    return any(kw.upper() in n for kw in EXCLUDE_KW)

def is_preferred(name):
    return bool(re.search(r'[가-힣]\d?우[A-Z]?$', name))

# ── 전종목 수집 ───────────────────────────────────────────────
def get_all_codes():
    all_stocks = []
    for sosok, pages, market in [(0, 50, 'KOSPI'), (1, 37, 'KOSDAQ')]:
        print(f'  {market} 수집중...')
        for page in range(1, pages + 1):
            try:
                r = requests.get('https://finance.naver.com/sise/sise_market_sum.nhn',
                                 params={'sosok': sosok, 'page': page},
                                 headers=H_NAVER, timeout=8)
                r.encoding = 'euc-kr'
                for m in re.finditer(
                    r'href="[^"]*code=(\d{6})"[^>]*class="tltle">([^<]+)</a>.*?'
                    r'class="number">([\d,]+)</td>',
                    r.text, re.S
                ):
                    code, name = m.group(1), m.group(2).strip()
                    try:
                        close = int(m.group(3).replace(',', ''))
                    except:
                        continue
                    all_stocks.append((code, name, close))
                time.sleep(0.05)
            except:
                pass
    seen, unique = set(), []
    for s in all_stocks:
        if s[0] not in seen:
            seen.add(s[0])
            unique.append(s)
    return unique

# ── sise_day 파싱 → [Close, Open, High, Low, Volume] ─────────
def parse_sise_day(code, page):
    r = requests.get('https://finance.naver.com/item/sise_day.nhn',
                     params={'code': code, 'page': page},
                     headers=H_NAVER, timeout=8)
    r.encoding = 'euc-kr'
    nums = re.findall(r'class="num"><span[^>]*>([\d,]+)</span>', r.text)
    rows = []
    for i in range(0, len(nums) - 4, 5):
        try:
            rows.append([int(nums[i+j].replace(',', '')) for j in range(5)])
        except:
            pass
    return rows

# ── 개별 종목 체크 ────────────────────────────────────────────
MAX_DAYS = max(CHECK_DAYS)

def check_stock(code, name):
    try:
        rows1 = parse_sise_day(code, 1)
        if len(rows1) < 6:
            return {}
        today = rows1[0]
        close, open_, _, _, vol = today
        if open_ == 0 or close == 0 or vol == 0:
            return {}
        if close < MIN_PRICE or close <= open_:
            return {}
        trading_val = close * vol
        if trading_val < MIN_TRADING:
            return {}
        prev5 = rows1[1:6]
        if len(prev5) < 5:
            return {}
        avg5 = sum(r[0] * r[4] for r in prev5) / 5
        if avg5 > 0 and trading_val < VOL_RATIO * avg5:
            return {}
        all_highs = [r[2] for r in rows1[1:]]
        if not all_highs or close <= max(all_highs):
            return {}
        pages_needed = (MAX_DAYS // 10) + 2
        for pg in range(2, pages_needed + 1):
            rows = parse_sise_day(code, pg)
            if not rows:
                break
            if any(r[4] == 0 for r in rows):
                return {}
            all_highs.extend([r[2] for r in rows])
            if len(all_highs) >= MAX_DAYS:
                break
        if len(all_highs) < MAX_DAYS // 2:
            return {}
        base = {
            '종목코드': code, '종목명': name,
            '오늘시가': open_, '오늘종가': close,
            '거래대금(억)': round(trading_val / 1e8, 1),
            '5일평균대비': round(trading_val / avg5, 2) if avg5 > 0 else 0,
        }
        results = {}
        for nd in CHECK_DAYS:
            highs = all_highs[:nd]
            if not highs:
                continue
            high_n = max(highs)
            if high_n > 0 and close > high_n:
                results[nd] = {**base, f'{nd}일고점': high_n,
                               '돌파율(%)': round((close / high_n - 1) * 100, 2)}
        return results
    except:
        return {}

# ── DART ─────────────────────────────────────────────────────
def has_bad_dart(code, name):
    try:
        start = (datetime.today() - timedelta(days=DART_DAYS)).strftime('%Y%m%d')
        r = requests.get('https://dart.fss.or.kr/dsab001/search.ax',
            params={'textCrpNm': name, 'startDt': start, 'selectKey': 'ALL'},
            headers=H_DART, timeout=10)
        text = r.content.decode('utf-8', errors='replace')
        return any(kw in text for kw in ['워크아웃', '횡령', '배임'])
    except:
        return False

# ── Main ─────────────────────────────────────────────────────
def main():
    today_str = datetime.today().strftime('%Y-%m-%d (%a)')
    print(f'=== 보합+박스장 돌파매매 | {today_str} ===')
    t0 = time.time()

    if datetime.today().weekday() >= 5:
        print('주말 — 스킵')
        return

    tg_send(f'🔍 스크리닝 시작... ({today_str})')

    blacklist = build_blacklist()
    print(f'블랙리스트: {len(blacklist)}개')

    all_stocks = get_all_codes()
    print(f'전체: {len(all_stocks)}개')

    filtered = [
        (c, n, p) for c, n, p in all_stocks
        if p >= MIN_PRICE and not is_excluded(n)
        and not is_preferred(n) and c not in blacklist
    ]
    print(f'1차 필터: {len(filtered)}개')

    bucket = {nd: [] for nd in CHECK_DAYS}
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(check_stock, c, n): (c, n) for c, n, _ in filtered}
        for f in as_completed(futures):
            done += 1
            if done % 500 == 0:
                print(f'  진행: {done}/{len(filtered)}')
            res = f.result()
            for nd, row in res.items():
                bucket[nd].append(row)

    all_cands = {r['종목코드']: r['종목명']
                 for rows in bucket.values() for r in rows}
    if all_cands:
        print(f'DART 검색: {len(all_cands)}개')
        bad = {c for c, n in all_cands.items() if has_bad_dart(c, n)}
        for nd in CHECK_DAYS:
            bucket[nd] = [r for r in bucket[nd] if r['종목코드'] not in bad]

    print(f'완료: {time.time()-t0:.0f}초')
    for nd in CHECK_DAYS:
        rows = sorted(bucket[nd], key=lambda x: -x['돌파율(%)'])
        print(f'\n▶ {nd}일 돌파 ({len(rows)}개)')
        for r in rows:
            print(f"  {r['종목명']} +{r['돌파율(%)']}% | {r['거래대금(억)']}억")

    tg_send_results(bucket, today_str)

if __name__ == '__main__':
    main()
