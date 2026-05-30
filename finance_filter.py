"""
재무제표 필터 모듈
==================
FnGuide SVD_Finance.asp 기반

조건 A: 매출액/영업이익/영업이익(발표기준)/당기순이익 중
        하나라도 존재하는 값이 전부 마이너스면 제외

조건 B: 자본금 N/A 또는 자본(순자산) ≤ 0 → 자본잠식 → 제외

Returns: (status, reason)
  status: 'pass' | 'fail' | '확인필요'
  reason: 제외 사유 문자열
"""

import requests, re
from bs4 import BeautifulSoup

FNGUIDE_H = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://comp.fnguide.com',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

TARGET_ROWS = ['매출액', '영업이익', '영업이익(발표기준)', '당기순이익']


def _parse_table(tbl):
    """table → { row_name: [val1, val2, ...] }
    빈칸 = None, 숫자면 float 변환
    마지막 2컬럼('전년동기', '전년동기(%)') 자동 제외
    """
    result = {}
    rows = tbl.find_all('tr')
    if not rows:
        return result

    # 헤더로 데이터 컬럼 수 파악
    header = [c.get_text(strip=True) for c in rows[0].find_all(['th', 'td'])]
    # 마지막 2개 = '전년동기', '전년동기(%)' → 제외
    # 첫 번째 = 항목명 → 제외
    # 데이터 컬럼 = 1 ~ len-3
    n_data = max(len(header) - 3, 1)

    for row in rows[1:]:
        cells = row.find_all(['th', 'td'])
        if not cells:
            continue
        # 항목명: '계산에 참여한 계정 펼치기' 같은 노이즈 제거
        name = re.sub(r'계산에 참여한 계정 펼치기', '', cells[0].get_text(strip=True)).strip()
        if not name:
            continue

        vals = []
        for cell in cells[1: n_data + 1]:
            txt = cell.get_text(strip=True).replace(',', '').replace(' ', '')
            if txt in ('', '-', 'N/A', 'n/a'):
                vals.append(None)
            else:
                try:
                    vals.append(float(txt))
                except ValueError:
                    vals.append(None)

        if name not in result:  # 첫 등장만 저장 (소계 중복 방지)
            result[name] = vals

    return result


def check_financials(code: str):
    """
    code: 6자리 종목코드 (e.g. '005930')
    Returns: (status: str, reason: str)
    """
    url = 'https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp'
    params = {
        'pGB': '1', 'gicode': f'A{code}', 'cID': '',
        'MenuYn': 'Y', 'ReportGB': 'D', 'NewMenuID': '134', 'stkGb': '701'
    }

    try:
        r = requests.get(url, params=params, headers=FNGUIDE_H, timeout=12)
        r.encoding = 'utf-8'
        if r.status_code != 200:
            return '확인필요', f'HTTP {r.status_code}'

        soup = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table')

        if len(tables) < 3:
            return '확인필요', '재무데이터없음'

        annual_data  = _parse_table(tables[0])   # 연간 손익
        quarter_data = _parse_table(tables[1])   # 분기 손익
        balance_data = _parse_table(tables[2])   # 재무상태표

        # ── 신규상장 체크 (연간 데이터 2개 미만) ──────────────
        rev_annual = [v for v in annual_data.get('매출액', []) if v is not None]
        is_new_ipo = len(rev_annual) < 2

        # ── 조건 A: 존재하는 값이 전부 마이너스인 행 있으면 제외 ─
        for row_name in TARGET_ROWS:
            a_vals = annual_data.get(row_name, [])
            q_vals = quarter_data.get(row_name, [])

            # 최근 3개만 사용
            a3 = [v for v in a_vals[-3:] if v is not None]
            q3 = [v for v in q_vals[-3:] if v is not None]
            all_vals = a3 + q3

            if not all_vals:
                continue  # 데이터 없으면 무시

            if all(v < 0 for v in all_vals):
                reason = f'{row_name} 조회기간 전체 마이너스'
                if is_new_ipo:
                    return '확인필요', f'신규상장+{reason}'
                return 'fail', reason

        # ── 조건 B: 자본잠식 체크 ─────────────────────────────
        # 자본금 N/A
        cap_vals = [v for v in balance_data.get('자본금', []) if v is not None]
        if not cap_vals:
            return 'fail', '자본금N/A(자본잠식)'

        # 자본(순자산) 음수
        eq_vals = balance_data.get('자본', [])
        eq_numeric = [v for v in eq_vals if v is not None]
        if eq_numeric:
            recent = eq_numeric[-2:]
            if any(v <= 0 for v in recent):
                return 'fail', '자본잠식(자본음수)'

        # 통과
        if is_new_ipo:
            return '확인필요', '신규상장(가용데이터기준통과)'
        return 'pass', '재무이상없음'

    except Exception as e:
        return '확인필요', f'조회오류:{str(e)[:40]}'
