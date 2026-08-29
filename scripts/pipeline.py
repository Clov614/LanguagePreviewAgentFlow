"""语言预习管线 · 核心(阶段 1-6):MD → 章节切分 → 词形还原 → CEFR 判定 → 打分选词 → 例句抽取
输出:data/output/<book>/raw/chapter_XX_raw.csv(每章候选,供模型润色释义/译文)
     data/output/<book>/meta.json(全书统计)
用法:uv run python scripts/pipeline.py --book little_women [--limit N] [--per-chapter 18]
     uv run python scripts/pipeline.py --book little_women --from-chapter 20   # 从第 20 章续跑
     uv run python scripts/pipeline.py --book little_women --include-exported  # 连已出过卡的词也重选
"""
import argparse, csv, datetime, json, os, re, sqlite3, sys
import simplemma

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(BASE, 'resources')
LANG = 'en'

WORD_RE = re.compile(r"[A-Za-z]+(?:[’'-][A-Za-z]+)*")
CHAPTER_RE = re.compile(r'\*\*Chapter\s+(\d+)\s+(.*?)\*\*')
SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=["“‘\'“A-Z0-9])')
MDJUNK_RE = re.compile(r'\*+|#+|_+')
ANCHOR_RE = re.compile(r'<a\s+[^>]*>.*?</a>|</?a[^>]*>')
ABBR_RE = re.compile(r'\b(Mr|Mrs|Ms|Dr|St|Messrs|Prof|Rev)\.')
ABBR_PH = '<AB>'

# 小说专有名词(直接排除,不参与候选)
NOVEL_PROPER = {
    'meg', 'jo', 'joe', 'beth', 'amy', 'marmee', 'laurie', 'hannah', 'march',
    'trotty', 'daisy', 'demi', 'bhaer', 'brooke', 'marches',
}

LEVEL_RANK = {'a1': 1, 'a2': 2, 'b1': 3, 'b2': 4, 'c1': 5}
CEFR_VALUE = {'b1': 0.35, 'b2': 1.0, 'c1': 1.2, 'toe': 0.8}

STOPWORDS = {
    'o', 'oh', 'ah', 'eh', 'ha', 'huh', 'hmm', 'hallo', 'hullo', 'hey', 'ahem',
    'mrs', 'mr', 'ms', 'dr', 'st', 'etc', 'thou', 'thee', 'thy', 'thine', 'ye',
    "'d", "'ll", "'re", "'ve", 'tis', "don't", "can't", "won't", "ain't",
    "it's", "that's", "i'm", "i've", "i'll", "i'd", "he's", "she's", "we're",
}

def load_oxford():
    """oxford-5000.csv 含 A1-C1 全部级别(是 3000 的超集)"""
    d = {}
    with open(os.path.join(RES, 'oxford3000-5000', 'oxford-5000.csv'), encoding='utf-8') as f:
        r = csv.DictReader(f)
        for row in r:
            w = row['word'].lower()
            lv = row['level'].lower().strip()
            if lv not in LEVEL_RANK:
                continue
            if w not in d or LEVEL_RANK[lv] < LEVEL_RANK[d[w][1]]:
                d[w] = (row['class'], lv)   # 同词多行取最低级(保守:基础词按基础难度)
    return d

class Ecdict:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
    def get(self, word):
        row = self.con.execute(
            'SELECT phonetic, translation, tags, bnc, frq FROM dict WHERE word=?',
            (word,)).fetchone()
        return row
    def close(self):
        self.con.close()

def guess_level(tags, frq):
    """未知词(不在 Oxford 5000)用 ECDICT 考试标签粗分级;返回 cefr 或 None(超纲)"""
    if not tags:
        return None
    if '研' in tags or '六' in tags:
        return 'c1' if frq > 8000 else 'b2'
    if '四' in tags:
        return 'b2' if frq > 8000 else 'b1'
    if '高' in tags:
        return 'b1'
    return None

def calibrate(cefr, bnc, frq):
    """词频校准:镜像词表把部分 B2 标成 C1;BNC 排名<5000 的高频词不可能是 C1
    (让级别骨架更贴近真实难度,也对准考研水平:高频=易)
    """
    r = (bnc or 0) or (frq or 0)
    if not r:
        return cefr
    if cefr == 'c1' and r < 5000:
        return 'b2'
    if cefr == 'b2' and r < 1500:
        return 'b1'
    return cefr

def split_chapters(md_text):
    """全局匹配 **Chapter N 标题**,Part2 等非数字粗体自动跳过"""
    chapters = []
    pos = 0
    for m in CHAPTER_RE.finditer(md_text):
        num, title = int(m.group(1)), m.group(2).strip()
        body = md_text[pos:m.start()]     # 上一章正文(第一次匹配前为前置页,丢弃)
        if chapters:
            chapters[-1]['body'] += body
        chapters.append({'num': num, 'title': title, 'body': ''})
        pos = m.end()
    if chapters:
        chapters[-1]['body'] += md_text[pos:]
    return chapters

def clean_text(t):
    t = ANCHOR_RE.sub('', t)
    t = ABBR_RE.sub(r'\1' + ABBR_PH, t)   # 保护 Mr. Mrs. 等缩写,防误切句
    t = MDJUNK_RE.sub('', t)
    return t

def analyze_chapter(body, oxford, db):
    """返回 (sents, stats, sent_lemmas)
    sents: 原始句子列表
    stats: lemma -> {freq, nonfirst_upper, sids: [句号], toks:[原始词在句中序号]}
    """
    body = clean_text(body)
    sents = [s.replace(ABBR_PH, '.').strip()
             for s in SENT_SPLIT.split(body) if 2 <= len(s) <= 500]
    stats = {}
    sent_recs = []
    for sid, s in enumerate(sents):
        toks = WORD_RE.findall(s)
        if not toks:
            sent_recs.append([])
            continue
        lem_toks = [simplemma.lemmatize(t.lower(), lang=LANG) for t in toks]
        for ti, (raw, lem) in enumerate(zip(toks, lem_toks)):
            lem = lem.lower()
            if lem in STOPWORDS or not re.fullmatch(r"[a-z]{2,}", lem):
                continue
            st = stats.setdefault(lem, {'freq': 0, 'nonfirst_upper': 0, 'sids': [], 'tids': []})
            st['freq'] += 1
            if ti > 0 and raw[0].isupper():
                st['nonfirst_upper'] += 1
            st['sids'].append(sid)
            st['tids'].append(ti)
        sent_recs.append(lem_toks)
    return sents, stats, sent_recs

def is_proper(lemma, st, oxford):
    if lemma in oxford:
        return False
    f = st['freq']
    nu = st['nonfirst_upper']
    if nu >= 2 and nu >= f * 0.35 and f <= 40:
        return True
    if f <= 6 and nu >= 1:
        return True
    return False

def pick_example(lem, st, sents, sent_recs):
    """在章内为本词找最短且真实含词句子(校验 lemma 匹配,防位置错位);
    返回 (sentence, 词在句中字符偏移, 词的原形, 句号)"""
    cand = []
    for sid, tid in zip(st['sids'], st['tids']):
        s = sents[sid]
        if not (8 <= len(s) <= 200):
            continue
        toks = WORD_RE.findall(s)
        if tid >= len(toks):
            continue
        if simplemma.lemmatize(toks[tid].lower(), lang=LANG) != lem:
            continue
        cand.append((len(s), s, sid, tid))
    if not cand:
        return None
    _, s, sid, tid = min(cand, key=lambda x: x[0])
    off = 0
    for _ in range(tid):
        j = WORD_RE.search(s, off)
        if not j:
            return None
        off = j.end()
    m = WORD_RE.search(s, off)
    if m:
        return s, m.start(), m.group(0), sid
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 章(0=全部)')
    ap.add_argument('--from-chapter', type=int, default=0, help='从第 N 章开始处理(断点续跑,0=从头)')
    ap.add_argument('--per-chapter', type=int, default=18)
    ap.add_argument('--b1-quota', type=float, default=0.10)
    ap.add_argument('--toe-quota', type=float, default=0.30)
    ap.add_argument('--include-exported', action='store_true',
                    help='连总库中已出过卡的词也重新纳入候选(默认跳过)')
    args = ap.parse_args()

    md_path = os.path.join(BASE, 'data', 'books', '_md', f'{args.book}.md')
    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    os.makedirs(out_dir, exist_ok=True)

    oxford = load_oxford()
    db = Ecdict(os.path.join(RES, 'ecdict.db'))

    md_text = open(md_path, encoding='utf-8').read()
    chapters = split_chapters(md_text)
    if args.limit:
        chapters = chapters[:args.limit]
    if args.from_chapter:
        chapters = [c for c in chapters if c['num'] >= args.from_chapter]
    print(f'chapters: {len(chapters)} (from {chapters[0]["num"] if chapters else "-"})', flush=True)

    # 全书词频(用于全局加权):断点续跑时仍需全部章节的词频统计(只算不重出卡)
    book_freq = {}
    all_chapter_stats = []
    for ch in split_chapters(md_text):
        _, stats, _ = analyze_chapter(ch['body'], oxford, db)
        all_chapter_stats.append(stats)
        for lem, st in stats.items():
            book_freq[lem] = book_freq.get(lem, 0) + st['freq']

    b1_max = max(1, round(args.per_chapter * args.b1_quota))
    toe_max = max(1, round(args.per_chapter * args.toe_quota))
    print(f'quotas: b1<={b1_max} toe<={toe_max} of {args.per_chapter}', flush=True)

    # 跨书去重:总库中已出过卡(card_exported=1)或已掌握的词不再推荐,
    # 除非 --include-exported 显式要求重选(如调整参数后有意重新出卡)
    seen = set()   # 章内/书内去重 + 跨书已出卡词
    if not args.include_exported:
        wl = os.path.join(BASE, 'vocabulary', 'master_wordlist.csv')
        if os.path.exists(wl):
            with open(wl, encoding='utf-8', newline='') as f:
                for r in csv.DictReader(f):
                    if r.get('card_exported') == '1' or r.get('status') == 'known':
                        seen.add(r['word'].strip().lower())
            print(f'seen: 跳过总库已出卡词 {len(seen)} 个', flush=True)

    date_today = datetime.date.today().isoformat()
    all_selected = {}
    for ci, ch in enumerate(chapters):
        sents, stats, sent_recs = analyze_chapter(ch['body'], oxford, db)
        cands = []
        for lem, st in stats.items():
            if lem in oxford:
                cls, cefr = oxford[lem]
            else:
                cls, cefr = '', None
            if cefr in ('a1', 'a2'):
                continue
            if lem in NOVEL_PROPER or is_proper(lem, st, oxford):
                continue
            ec = db.get(lem) or (None, None, None, 0, 0)
            phon, trans, tags, bnc, frq = ec
            if (bnc and bnc < 1000) or (frq and frq < 800):
                continue   # 英语前 1000 高频词:考研者必知,排除
            phon, trans, tags, bnc, frq = ec
            if cefr is None:
                cefr = guess_level(tags, frq) or 'toe'
            cefr = calibrate(cefr, bnc, frq)
            cf = book_freq.get(lem, 0)
            score = (CEFR_VALUE.get(cefr, 0) * 2.0
                     + min(st['freq'], 20) / 20.0 * 1.2
                     + min(cf, 30) / 30.0 * 0.8
                     + (0.15 if bnc and bnc < 5000 else 0))
            cands.append({
                'word': lem, 'cefr': cefr, 'pos': cls or '',
                'score': round(score, 4), 'freq_ch': st['freq'], 'freq_book': cf,
                'phon': phon or '', 'trans': (trans or '').replace('\n', ' / '),
                'tags': tags or '', 'bnc': bnc or 0, 'frq': frq or 0,
                'sids': st['sids'], 'tids': st['tids'],
            })
        cands.sort(key=lambda c: -c['score'])

        # 配额挑选(新词优先;本词候选不足时允许已选词复习补位)
        picked, picked_words = [], set()
        n_b1 = n_toe = 0
        for c in cands:
            if c['word'] in seen:
                continue
            if c['cefr'] == 'b1':
                if n_b1 >= b1_max:
                    continue
                n_b1 += 1
            elif c['cefr'] == 'toe':
                if n_toe >= toe_max:
                    continue
                n_toe += 1
            picked.append(c)
            picked_words.add(c['word'])
            if len(picked) >= args.per_chapter:
                break
        if len(picked) < args.per_chapter:
            for c in cands:
                if c['word'] in picked_words:
                    continue
                picked.append(c)
                picked_words.add(c['word'])
                if len(picked) >= args.per_chapter:
                    break
        for c in picked:
            seen.add(c['word'])

        rows = []
        for c in picked:
            ex = pick_example(c['word'], stats[c['word']], sents, sent_recs)
            sent = ex[0] if ex else ''
            off = ex[1] if ex else -1
            # replace('\n',' ') 不改变字符数,保证 sent_off 偏移不变
            rows.append({**c, 'sent': sent.replace('\n', ' '), 'sent_off': off,
                         'chapter': ch['num'], 'date': date_today})

        raw_dir = os.path.join(out_dir, 'raw')
        os.makedirs(raw_dir, exist_ok=True)
        out_csv = os.path.join(raw_dir, f'chapter_{ch["num"]:02d}_raw.csv')
        # utf-8-sig:带 BOM,Excel 双击即可正确显示中文(无 BOM 会被当 GBK 解析)
        with open(out_csv, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                                    ['word', 'cefr', 'pos', 'score', 'freq_ch', 'freq_book',
                                     'phon', 'trans', 'tags', 'bnc', 'frq', 'sent', 'sent_off',
                                     'chapter', 'date'])
            writer.writeheader()
            writer.writerows(rows)
        for c in picked:
            all_selected[c['word']] = all_selected.get(c['word'], 0) + 1
        print(f'ch {ch["num"]}: {len(cands)} cands -> {len(picked)} picked '
              f'(b1={n_b1} toe={n_toe})', flush=True)

    with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'book': args.book, 'chapters': len(chapters),
            'total_tokens_lower': sum(book_freq.values()),
            'selected_lemma_total': len(all_selected),
            'selected_occurrences': all_selected,
            'quota': {'per_chapter': args.per_chapter, 'b1': b1_max, 'toe': toe_max},
        }, f, ensure_ascii=False, indent=1)
    db.close()
    print('DONE ->', out_dir, flush=True)

if __name__ == '__main__':
    main()