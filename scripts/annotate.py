"""生词标注版(⑫):把每章选中的生词在原文章节文本中加粗高亮
→ 刷完卡片翻开书,"第一眼识别"的就是背过的词(与视频"看剧识别台词"同构)
用法: uv run python scripts/annotate.py --book little_women [--chapter N]
"""
import argparse, csv, glob, os, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))
try:
    from pipeline import split_chapters, clean_text
except ImportError:
    sys.path.insert(0, BASE)
    from scripts.pipeline import split_chapters, clean_text


def build_marks(rows):
    """返回 [(regex, word)] 高亮词形(含常见屈折)"""
    pats = []
    for r in rows:
        b = re.escape(r['word'])
        pats.append(re.compile(rf'\b{b}(?:s|es|ed|ing|d|ies|ied|t)\b', re.I))
    return pats


def highlight(body, pats):
    hl = []
    for p in pats:
        for m in p.finditer(body):
            hl.append((m.start(), m.end()))
    hl.sort()
    out, prev = [], 0
    for start, end in hl:
        if start < prev:
            continue
        out.append(body[prev:start])
        out.append(f'**{body[start:end]}**')
        prev = end
    out.append(body[prev:])
    return ''.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--chapter', type=int, default=0)
    args = ap.parse_args()

    md_path = os.path.join(BASE, 'data', 'books', '_md', f'{args.book}.md')
    out_dir = os.path.join(BASE, 'data', 'output', args.book, 'annotated')
    os.makedirs(out_dir, exist_ok=True)

    chapters = split_chapters(open(md_path, encoding='utf-8').read())
    files = sorted(glob.glob(os.path.join(
        BASE, 'data', 'output', args.book, 'chapter_*_raw.csv')))
    if args.chapter:
        files = [f for f in files if f.endswith(f'chapter_{args.chapter:02d}_raw.csv')]

    for fp in files:
        ch = int(os.path.basename(fp).split('_')[1])
        ch_meta = next((c for c in chapters if c['num'] == ch), None)
        if not ch_meta:
            continue
        with open(fp, encoding='utf-8') as f:
            rows = [r for r in csv.DictReader(f) if r.get('cn_mean')]
        body = clean_text(ch_meta['body']).replace('<AB>', '.')
        pats = build_marks(rows)
        text = f"# {ch_meta['title']}\n\n" + highlight(body, pats)
        internal = []
        ref = []
        for r in rows:
            ref.append(f"- **{r['word']}** · {r['cefr'].upper()} · {r.get('cn_mean', '')}")
            internal.append(r['word'])
        pre = f"> 本章生词卡片 {len(rows)} 个: {'、'.join(internal)}\n\n"
        with open(os.path.join(out_dir, f'chapter_{ch:02d}.md'), 'w',
                  encoding='utf-8') as f:
            f.write(text)
        with open(os.path.join(out_dir, f'chapter_{ch:02d}_词表.md'), 'w',
                  encoding='utf-8') as f:
            f.write(pre + '\n'.join(ref))
        print(f'[OK] ch{ch}: {len(rows)} 词标注 -> annotated/chapter_{ch:02d}.md', flush=True)
    print('DONE', flush=True)


if __name__ == '__main__':
    main()