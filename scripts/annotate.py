"""生词标注版(⑫):把每章选中的生词在原文章节文本中加粗高亮
→ 刷完卡片翻开书,"第一眼识别"的就是背过的词(与视频"看剧识别台词"同构)
用法: uv run python scripts/annotate.py --book little_women [--chapter N]
"""
import argparse, csv, glob, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))
try:
    from pipeline import split_chapters, clean_text
    from wordforms import token_regex
except ImportError:
    sys.path.insert(0, BASE)
    from scripts.pipeline import split_chapters, clean_text
    from scripts.wordforms import token_regex


def build_marks(rows):
    """整章所有目标词合成一个词边界正则(含屈折形态,见 wordforms),单遍匹配不重叠;
    空词表返回 None(此时正文不做高亮,防空正则全文乱标 —— 历史 bug)"""
    if not rows:
        return None
    return token_regex([r['word'] for r in rows])


def highlight(body, pat):
    if pat is None:
        return body
    return pat.sub(lambda m: f'**{m.group()}**', body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--chapter', type=int, default=0)
    args = ap.parse_args()

    md_path = os.path.join(BASE, 'data', 'books', '_md', f'{args.book}.md')
    if not os.path.exists(md_path):
        sys.exit(f'[STOP] 找不到管线输入 {md_path} —— 先跑 scripts/epub_to_md.py(EPUB 转换)')
    out_dir = os.path.join(BASE, 'data', 'output', args.book, 'annotated')
    os.makedirs(out_dir, exist_ok=True)

    chapters = split_chapters(open(md_path, encoding='utf-8').read())
    # 单词 raw 才参与标注(表达卡不进标注版);排除 _phrase_raw.csv 防撞名重复处理
    files = sorted(f for f in glob.glob(os.path.join(
        BASE, 'data', 'output', args.book, 'raw', 'chapter_*_raw.csv'))
        if not f.endswith('_phrase_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.endswith(f'chapter_{args.chapter:02d}_raw.csv')]

    for fp in files:
        ch = int(os.path.basename(fp).split('_')[1])
        ch_meta = next((c for c in chapters if c['num'] == ch), None)
        if not ch_meta:
            continue
        with open(fp, encoding='utf-8-sig') as f:
            rows = [r for r in csv.DictReader(f) if r.get('cn_mean')]
        body = clean_text(ch_meta['body']).replace('<AB>', '.')
        pats = build_marks(rows)
        if pats is None:
            print(f'[WARN] ch{ch}: 本章无已润色词,标注版不做高亮,仅出词表', flush=True)
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