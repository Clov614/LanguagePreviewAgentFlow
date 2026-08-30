"""把模型润色结果合并回 raw CSV:
- polish 数据(main): word -> {cn_mean, cn_sent, [ai_analysis], [memo]}
- ai_en 数据(可选): word -> {ai_en}  书中无例句的词,补充英文例句写入 sent
- explain 数据(可选): word -> {ai_analysis, memo}  AI 例句解析 + 词义概述(ai_explain.py 产物),
  增量合并:只补两新列,不覆盖 cn_mean/cn_sent,重复跑幂等
- phrases 数据(可选): ai_pick_phrases.py 产物(每章精选表达 + 释义/译文/讲解),
  生成/重写该章 chapter_XX_phrase_raw.csv(与单词 raw 同表头,word=表达短语),
  cards.py 读入后表达卡排同章 TSV 最前
用法: uv run python scripts/apply_polish.py --book little_women \
      --polish data/output/little_women/polish_ch01-02.json [--ai-en <json>] [--chapter 1]
      uv run python scripts/apply_polish.py --book little_women \
      --explain data/output/little_women/work/ai_explain_little_women_ch01.json
      uv run python scripts/apply_polish.py --book little_women \
      --phrases 'data/output/little_women/work/phrases_picked_*.json'
"""
import argparse, csv, glob, json, os, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPLAIN_KEYS = ('ai_analysis', 'memo')


def md_bold(s):
    """模型常把强调写成 Markdown 加粗 **x**(Anki 不渲染 Markdown,星号会裸露)。
    合并前统一转成 <b>x</b>(cards 渲染时白名单放行);幂等:无 ** 时原样返回。"""
    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s, flags=re.S)


# 表达卡 raw CSV 表头(与单词 raw 同构;word=表达短语,pos=phrase,音标留空,CEFR=块内最高)
PHRASE_HEADER = ['word', 'cefr', 'pos', 'score', 'freq_ch', 'freq_book', 'phon',
                 'trans', 'tags', 'bnc', 'frq', 'sids', 'tids', 'sent', 'sent_off',
                 'cn_mean', 'cn_sent', 'ai', 'ai_analysis', 'memo']


def phrases_main(book, phrases_glob, only_chapter=0):
    """表达精选 JSON → 每章 chapter_XX_phrase_raw.csv(整章重写,以 picked 为准,幂等)。
    例证句/频次/难度(cefr_max)从 work/phrase_cands_<book>.json 补;不在候选里的条目跳过。
    行顺序 = picked JSON 内顺序(cards 输出时表达卡排在单词卡前面)。"""
    out_dir = os.path.join(BASE, 'data', 'output', book)
    work_dir = os.path.join(out_dir, 'work')
    cands_path = os.path.join(work_dir, f'phrase_cands_{book}.json')
    cands_all = json.load(open(cands_path, encoding='utf-8'))['chapters'] \
        if os.path.exists(cands_path) else {}

    files = sorted(glob.glob(phrases_glob))
    if not files:
        sys.exit(f'--phrases 未匹配到文件: {phrases_glob}')
    by_ch = {}
    for fp in files:
        m = re.search(r'_ch(\d+)\.json$', fp)
        if not m:
            # glob 可能顺带匹配 *_failed.json 等非产物文件:跳过不炸,仍在提示里说明
            print(f'[SKIP] 非产物文件(需 <书>_ch<NN>.json 命名): {os.path.basename(fp)}',
                  flush=True)
            continue
        ch = int(m.group(1))
        if only_chapter and ch != only_chapter:
            continue
        by_ch.setdefault(ch, []).extend(json.load(open(fp, encoding='utf-8')))

    n_total = 0
    for ch in sorted(by_ch):
        cands = {c['phrase']: c for c in cands_all.get(str(ch), [])}
        rows = []
        for p in by_ch[ch]:
            ph = str(p.get('phrase') or '').strip().lower()
            c = cands.get(ph)
            if not c:
                print(f'[SKIP] ch{ch} 表达 {ph!r} 不在该章候选(候选文件过期?'
                      f'先重跑 pipeline --phrases)', flush=True)
                continue
            rows.append({
                'word': ph, 'cefr': c.get('cefr_max', ''),
                'pos': 'phrase', 'score': c.get('score', ''),
                'freq_ch': c.get('freq_ch', ''), 'freq_book': c.get('freq_book', ''),
                'phon': '', 'trans': '', 'tags': '', 'bnc': '', 'frq': '',
                'sids': '', 'tids': '',
                # 与单词路径同口径:句中换行归一为空格(cards 高亮/音频按单行处理)
                'sent': (c.get('example') or '').replace('\n', ' '),
                'sent_off': '',
                'cn_mean': str(p.get('cn_mean', '')).replace('\n', '；'),
                'cn_sent': str(p.get('cn_sent', '')).replace('\n', '；'),
                'ai': '', 'ai_analysis': p.get('ai_analysis', ''),
                'memo': p.get('memo', ''),
            })
        fp_out = os.path.join(out_dir, 'raw', f'chapter_{ch:02d}_phrase_raw.csv')
        with open(fp_out, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=PHRASE_HEADER, lineterminator='\n')
            w.writeheader()
            w.writerows(rows)
        n_total += len(rows)
        print(f'[OK] {os.path.basename(fp_out)}: {len(rows)} 条表达', flush=True)
    print(f'DONE 表达卡数据 {n_total} 条', flush=True)
    if n_total:
        print(f'下一步: uv run python scripts/cards.py --book {book}', flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--polish', default='', help='润色 JSON 文件(word -> cn_mean/cn_sent/...)')
    ap.add_argument('--ai-en', default='', help='补句 JSON 文件')
    ap.add_argument('--explain', default='', help='AI 解析 JSON 文件(word -> ai_analysis/memo)')
    ap.add_argument('--phrases', default='',
                    help='表达精选 JSON 文件(ai_pick_phrases.py 产物,支持 glob 多章)')
    ap.add_argument('--chapter', type=int, default=0, help='只处理指定章')
    args = ap.parse_args()

    if args.phrases:
        phrases_main(args.book, args.phrases, args.chapter)
        return

    polish = {p['word']: p for p in json.load(open(args.polish, encoding='utf-8'))} \
        if args.polish else {}
    ai_en = {p['word']: p['ai_en'] for p in polish.values() if p.get('ai_en')}
    if args.ai_en:
        ai_en.update({p['word']: p['ai_en'] for p in json.load(open(args.ai_en, encoding='utf-8'))})
    # explain 数据:独立文件 + polish 内自带的 ai_analysis/memo 都收进来
    explain = {p['word']: {k: md_bold(p[k]) for k in EXPLAIN_KEYS if p.get(k)}
               for p in polish.values() if any(p.get(k) for k in EXPLAIN_KEYS)}
    if args.explain:
        # 支持 glob 通配一次合并多章(--explain 'work/ai_explain_*.json');无通配符时即单文件
        for fp in sorted(glob.glob(args.explain)):
            for p in json.load(open(fp, encoding='utf-8')):
                explain[p['word']] = {k: md_bold(p[k]) for k in EXPLAIN_KEYS if p.get(k)}

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    files = sorted(f for f in glob.glob(os.path.join(out_dir, 'raw', 'chapter_*_raw.csv'))
                   if f.endswith('_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.endswith(f'chapter_{args.chapter:02d}_raw.csv')]

    n_all = n_ai = n_ex = 0
    for fp in files:
        # utf-8-sig 读取:无 BOM 文件照常读,带 BOM 文件剥掉 BOM(防字段名被 ﻿ 污染)
        with open(fp, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            p = polish.get(r['word'])
            if p:
                # 中文释义/译文里的换行→'；',避免 Excel 里整行竖排撑高
                r['cn_mean'] = p.get('cn_mean', '').replace('\n', '；')
                r['cn_sent'] = p.get('cn_sent', '').replace('\n', '；')
                n_all += 1
            # AI 补句独立于 polish 匹配:缺原句的词即使不在 polish 文件里也能补(仅靠 --ai-en)
            if not r['sent'] and r['word'] in ai_en:
                r['sent'] = ai_en[r['word']].replace('\n', ' ')
                r['ai'] = '1'
                n_ai += 1
            # AI 解析增量合并:只补 ai_analysis/memo 两列,保留换行(cards 里转 <br>),幂等
            e = explain.get(r['word'])
            if e:
                for k in EXPLAIN_KEYS:
                    if e.get(k):
                        r[k] = e[k]
                n_ex += 1
        fields = []
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
        with open(fp, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
            w.writeheader()
            w.writerows(rows)
        print(f'[OK] {os.path.basename(fp)}: 润色 {sum(1 for r in rows if r.get("cn_mean"))} 词'
              f' (补句 {n_ai} 解析 {n_ex})', flush=True)
    summary = f'DONE 共润色 {n_all} 词' + (f' | AI 解析 {n_ex} 词' if n_ex else '') + \
              (f' | 补句 {n_ai}' if n_ai else '')
    print(summary, flush=True)

if __name__ == '__main__':
    main()