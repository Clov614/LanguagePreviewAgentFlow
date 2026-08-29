"""一次性数据迁移(可重复执行,幂等):回填 P0 遗留的总库字段
背景:旧版 pipeline 写 raw CSV 时没有 chapter/date 列,cards.py 又把
      src['chapter'] 写进总库 —— 导致 Little Women 704 行 chapters /
      first_seen / recommended_date / sources 全为空。
本次迁移:从 data/output/<book>/raw/chapter_XX_raw.csv 反推每个词的章节,
     回填 chapters / sources / first_seen / recommended_date(迁移日)。
用法:  .venv/Scripts/python.exe scripts/migrate_wordlist.py [--book little_women]
"""
import argparse, csv, datetime, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORDLIST = os.path.join(BASE, 'vocabulary', 'master_wordlist.csv')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', default='little_women')
    args = ap.parse_args()
    raw_dir = os.path.join(BASE, 'data', 'output', args.book, 'raw')

    # 每章选中词章号 + 该书 freq_book 映射(以 raw CSV 为准,含未润色的词也记录)
    #   word_chs:  word -> [ch...]          freq_map: word -> 该书频次(全书)
    word_chs = {}
    freq_map = {}
    if os.path.isdir(raw_dir):
        for fn in sorted(os.listdir(raw_dir)):
            if not fn.endswith('_raw.csv'):
                continue
            ch = int(fn.split('_')[1])
            with open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline='') as f:
                for r in csv.DictReader(f):
                    w = r['word'].strip().lower()
                    word_chs.setdefault(w, []).append(ch)
                    freq_map[w] = int(r.get('freq_book') or 0)

    if not os.path.exists(WORDLIST):
        print('总库不存在,无需迁移', flush=True)
        return
    with open(WORDLIST, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    day = datetime.date.today().isoformat()
    n_fixed = 0
    for r in rows:
        w = r['word'].strip().lower()
        chs = word_chs.get(w, [])
        chs_str = ','.join(str(c) for c in sorted(set(chs)))
        if chs_str:
            if (r.get('chapters') or '') != chs_str:
                r['chapters'] = chs_str
                n_fixed += 1
            # freq_book 重建为该书频次(消除历史重复累计);sources 与 cards.py
            # 幂等格式一致: 'book|chN|freq' 分号分隔
            fb = freq_map.get(w, 0)
            r['freq_book'] = str(fb)
            r['sources'] = ';'.join(f'{args.book}|ch{c}|{fb}' for c in sorted(set(chs)))
        elif not r.get('sources'):
            r['sources'] = 'legacy'   # 早期版本遗留词,出处已不可考,保留资产但不误标
        if not r.get('first_seen'):
            r['first_seen'] = day
        if not r.get('recommended_date'):
            r['recommended_date'] = day
        if not r.get('card_exported'):
            r['card_exported'] = '1'
    with open(WORDLIST, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f'DONE: 回填 chapters/sources {n_fixed} 行,补齐 first_seen/recommended_date', flush=True)

if __name__ == '__main__':
    main()