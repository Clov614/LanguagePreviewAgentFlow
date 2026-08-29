"""卡片与总库组装(阶段 8-11):
输入:  data/output/<book>/chapter_XX_raw.csv(含模型润色列 cn_mean / cn_sent)
       vocabulary/master_wordlist.csv(生词总库)
流程:  → 每章 Anki TSV(UTF-8, 首行字段名)
       → 更新生词总库(去重合并:同词累积来源/频次,不重复推荐已 known 词)
       → 生词标注版章节文本(高亮选中词)
用法:  uv run python scripts/cards.py --book little_women --chapter 1
"""
import argparse, csv, json, os, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOCAB = os.path.join(BASE, 'vocabulary')
WORDLIST = os.path.join(VOCAB, 'master_wordlist.csv')
KNOWN = os.path.join(VOCAB, 'known_words.txt')

ANNOT_FIELDS = ['word', 'cefr', 'pos', 'score', 'freq_ch', 'freq_book', 'phon',
                'trans', 'tags', 'bnc', 'frq', 'sent', 'sent_off',
                'cn_mean', 'cn_sent']

def load_known():
    if not os.path.exists(KNOWN):
        return set()
    return {w.strip().lower() for w in open(KNOWN, encoding='utf-8')
            if w.strip() and not w.startswith('#')}

def ensure_wordlist_schema():
    os.makedirs(VOCAB, exist_ok=True)
    if not os.path.exists(WORDLIST):
        with open(WORDLIST, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['word', 'cefr', 'first_seen', 'status', 'book', 'chapters',
                        'freq_book', 'sources', 'example_en', 'example_cn',
                        'recommended_date', 'card_exported'])

def merge_wordlist(rows, book):
    """把本章选中词并入总库;返回 (新词数, 已存在数, 跳过已知词数)"""
    ensure_wordlist_schema()
    known = load_known()
    existing = {}
    if os.path.exists(WORDLIST):
        with open(WORDLIST, encoding='utf-8', newline='') as f:
            for r in csv.DictReader(f):
                existing[r['word']] = r
    new_cnt = exist_cnt = skip_cnt = 0
    for src in rows:
        w = src['word']
        if w in known:
            skip_cnt += 1
            continue
        if w in existing:
            e = existing[w]
            e['freq_book'] = str(int(e['freq_book'] or 0) + int(src['freq_book'] or 0))
            e['chapters'] = e['chapters'] + ',' + str(src.get('chapter', ''))
            e['example_en'] = src.get('sent', '') or e['example_en']
            e['example_cn'] = src.get('cn_sent', '') or e['example_cn']
            e['card_exported'] = '1'
            e['status'] = 'active'
            exist_cnt += 1
        else:
            existing[w] = {
                'word': w, 'cefr': src['cefr'],
                'first_seen': src.get('date', ''), 'status': 'active',
                'book': book, 'chapters': str(src.get('chapter', '')),
                'freq_book': src['freq_book'], 'sources': '',
                'example_en': src.get('sent', ''), 'example_cn': src.get('cn_sent', ''),
                'recommended_date': src.get('date', ''), 'card_exported': '1',
            }
            new_cnt += 1
    with open(WORDLIST, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=existing[next(iter(existing))].keys()
                           if existing else ['word'])
        if existing:
            w.writeheader()
            w.writerows(existing.values())
    return new_cnt, exist_cnt, skip_cnt

def build_annotated(rows, chapter_text, title):
    """生词标注版:把选中词(含常见屈折形)在原文章节文本中加粗高亮"""
    pats = []
    for r in rows:
        b = re.escape(r['word'])
        pats.append(re.compile(rf'\b{b}(?:s|es|ed|ing|d|ies|ied|t)\b', re.I))
    hl = []   # (start, end)
    for p in pats:
        for m in p.finditer(chapter_text):
            hl.append((m.start(), m.end()))
    hl.sort()
    out, prev = [], 0
    for start, end in hl:
        if start < prev:
            continue   # 重叠区间跳过
        out.append(chapter_text[prev:start])
        out.append(f'**{chapter_text[start:end]}**')
        prev = end
    out.append(chapter_text[prev:])
    return title + '\n\n' + ''.join(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--chapter', type=int, help='只处理指定章(调试用)')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    chapters_dir = os.path.join(BASE, 'data', 'books', '_md')
    md_text = open(os.path.join(chapters_dir, f'{args.book}.md'), encoding='utf-8').read()

    stack = os.path.join(out_dir, 'raw')
    anki_dir = os.path.join(out_dir, 'anki')
    os.makedirs(anki_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(stack) if f.endswith('_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.startswith(f'chapter_{args.chapter:02d}_')]
    total_new = total_exist = total_skip = 0
    for fn in files:
        ch = int(fn.split('_')[1])
        with open(os.path.join(stack, fn), encoding='utf-8-sig', newline='') as f:
            rows = [r for r in csv.DictReader(f) if r.get('cn_mean')]
        if not rows:
            print(f'[SKIP] {fn}: 无可出卡词(全部缺润色)', flush=True)
            continue
        # 每章 TSV(无 BOM 的 UTF-8:Anki 对 BOM 敏感)
        tsv_path = os.path.join(anki_dir, f'chapter_{ch:02d}_anki.tsv')
        with open(tsv_path, 'w', encoding='utf-8', newline='') as f:
            f.write('单词\t音标\t词性\t中文释义\tCEFR\t原文例句\t例句译文\t来源\n')
            for r in rows:
                f.write('\t'.join([
                    r['word'], r['phon'], r['pos'] or '—', r['cn_mean'],
                    r['cefr'].upper(), r['sent'], r.get('cn_sent', ''),
                    f'{args.book} Ch{ch}',
                ]) + '\n')
        n, e, s = merge_wordlist(rows, args.book)
        total_new += n; total_exist += e; total_skip += s
        print(f'[OK]  ch{ch}: 卡片 {len(rows)} 词 | 总库 +{n} 复用{e} 跳过已知{s}', flush=True)
    print(f'DONE 总库更新: 新增{total_new} 复用{total_exist} 跳过{total_skip}', flush=True)

if __name__ == '__main__':
    main()