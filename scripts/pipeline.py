"""语言预习管线 · 核心(阶段 1-6):MD → 章节切分 → 词形还原 → CEFR 判定 → 打分选词 → 例句抽取
输出:data/output/<book>/raw/chapter_XX_raw.csv(每章候选,供模型润色释义/译文)
     data/output/<book>/meta.json(全书统计)
     data/output/<book>/work/phrase_cands_<book>.json(--phrases 表达候选模式,不写 raw)
用法:uv run python scripts/pipeline.py --book little_women [--limit N] [--per-chapter 18]
     uv run python scripts/pipeline.py --book little_women --from-chapter 20   # 从第 20 章续跑
     uv run python scripts/pipeline.py --book little_women --include-exported  # 连已出过卡的词也重选
     uv run python scripts/pipeline.py --book little_women --phrases --limit 1  # 只出表达候选(不动 raw)
"""
import argparse, csv, datetime, json, os, re, sqlite3, sys
import simplemma

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proper_names

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(BASE, 'resources')
LANG = 'en'

WORD_RE = re.compile(r"[A-Za-z]+(?:[’'-][A-Za-z]+)*")
CHAPTER_RE = re.compile(r'\*\*Chapter\s+(\d+)\s+(.*?)\*\*')
SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=["“‘\'A-Z0-9])')
MDJUNK_RE = re.compile(r'\*+|#+|_+')
ANCHOR_RE = re.compile(r'<a\s+[^>]*>.*?</a>|</?a[^>]*>')
ABBR_RE = re.compile(r'\b(Mr|Mrs|Ms|Dr|St|Messrs|Prof|Rev)\.')
ABBR_PH = '<AB>'

# 小说专有名词(直接排除,不参与候选):按书外置,main() 里从
# data/books/proper_names/<book>.txt 载入(扫描/确认流程见 scripts/scan_proper.py)
NOVEL_PROPER = frozenset()

LEVEL_RANK = {'a1': 1, 'a2': 2, 'b1': 3, 'b2': 4, 'c1': 5}
CEFR_VALUE = {'b1': 0.35, 'b2': 1.0, 'c1': 1.2, 'toe': 0.8}

STOPWORDS = {
    'o', 'oh', 'ah', 'eh', 'ha', 'huh', 'hmm', 'hallo', 'hullo', 'hey', 'ahem',
    'mrs', 'mr', 'ms', 'dr', 'st', 'etc', 'thou', 'thee', 'thy', 'thine', 'ye',
    "'d", "'ll", "'re", "'ve", 'tis', "don't", "can't", "won't", "ain't",
    "it's", "that's", "i'm", "i've", "i'll", "i'd", "he's", "she's", "we're",
    'th', 'nd', 'rd',   # 序数词后缀(书信日期标题 17th October 等被切成独立 token)
}

# ---- 表达收录(Phase 2):语块切分边界词与常用小品词 ----
# 边界词当语块分隔符:感叹/应答、说话动词、从句连接词 —— 切掉叙述噪音,
# 让块内保留的实词集团(可含 her/in/of 等功能词)成为表达候选。
PHRASE_BOUNDS = {
    'o', 'oh', 'ah', 'eh', 'ha', 'huh', 'hmm', 'hallo', 'hullo', 'hey', 'ahem',
    'hush', 'why', 'well', 'now', 'indeed', 'yes', 'no',
    'and', 'but', 'or', 'so', 'because', 'when', 'while', 'though', 'although',
    'if', 'then', 'than', 'that',
    'said', 'asked', 'replied', 'cried', 'exclaimed', 'answered', 'whispered',
    'murmured', 'added', 'began', 'thought',
}
# 动词短语常用小品词(末词),如 take off / give up / look after
PARTICLES = {
    'off', 'up', 'out', 'down', 'away', 'back', 'over', 'along', 'through',
    'round', 'around', 'behind', 'forth', 'ahead', 'apart', 'about', 'after',
}
# 块首词黑名单:代词 / be 动词 / there / this that 等 —— 块以这些词开头时
# 几乎都是句子残片(she was / there was / is it / you are),无背诵价值,整块滤掉。
WEAK_LEAD = {
    'she', 'he', 'it', 'they', 'you', 'we', 'i', 'me', 'him', 'her', 'them',
    'us', 'my', 'your', 'his', 'their', 'our', 'its',
    'there', 'that', 'this', 'these', 'those',
    'was', 'were', 'is', 'are', 'am', 'be', 'been', 'being',
    'had', 'has', 'have', 'do', 'did', 'does',
}
# 介词开头且仅 2 词的块(to gentlemen / with dust)基本是介词短语残片,滤掉;
# 3-4 词介词开头(on the other hand)保留。
PREP_LEAD = {
    'in', 'on', 'at', 'to', 'with', 'by', 'from', 'of', 'for', 'about',
    'into', 'upon', 'under', 'over', 'through', 'after', 'before',
    'without', 'against', 'between',
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

def analyze_chapter(body):
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

# ============ 表达候选提取(Phase 2,--phrases 模式) ============

def _tok_breaks(s):
    """句内 token 序列,标注 break:两 token 间有标点(非字母非空白)= 语块边界;
    或 token 本身是 PHRASE_BOUNDS 边界词。返回 [(tok, token序号, is_break)]。"""
    out = []
    last = 0
    idx = 0
    for m in WORD_RE.finditer(s):
        gap = s[last:m.start()]
        brk = bool(re.search(r'[^A-Za-z\s]', gap)) or m.group(0).lower() in PHRASE_BOUNDS
        out.append((m.group(0), idx, brk))
        last = m.end()
        idx += 1
    return out


def _ec_level(w, db):
    """未知词(不在 oxford)用 ECDICT 考试标签粗分级;返回 cefr 或 'toe'(超纲/未知)"""
    r = db.get(w)
    if not r or not r[2]:
        return 'toe'
    return guess_level(r[2], r[4] or 0) or 'toe'


def _is_good(low, oxford, db):
    """语块是否值得做候选:全 A1–A2 基础词组合过滤;
    例外:动词+小品词构式(take off / give up)——全基础词也保留。"""
    if len(low) == 2 and low[1] in PARTICLES:
        hit = oxford.get(low[0])
        if hit and str(hit[0]).startswith('v'):
            return True
    for w in low:
        hit = oxford.get(w)
        lv = hit[1] if hit else _ec_level(w, db)
        if lv not in ('a1', 'a2'):
            return True
    return False


def chapter_chunks(sents, oxford, db):
    """每句按语块边界切块(块长 2–4),过滤后 yield (key, sent);
    key = 小写原形序列(空格连接),同句去重(防单句刷频)。"""
    for s in sents:
        parts, cur = [], []
        for t, i, brk in _tok_breaks(s):
            if brk:
                parts.append(cur)
                cur = []
            else:
                cur.append((t, i))
        parts.append(cur)
        seen = set()
        for chunk in parts:
            if len(chunk) not in (2, 3, 4):
                continue
            if any(re.search(r"[’'-]", t) for t, _ in chunk):
                continue            # 撇号/连字符组合不参与(高亮/音频难处理)
            if any(i > 0 and t[0].isupper() for t, i in chunk):
                continue            # 句内大写 = 专名嫌疑
            low = [t.lower() for t, _ in chunk]
            if any(w in NOVEL_PROPER for w in low):
                continue
            if low[0] in WEAK_LEAD:
                continue            # 代词/be/there 开头 = 句子残片
            if len(low) == 2 and low[0] in PREP_LEAD:
                continue            # 介词开头 2 词 = 短语残片
            if len(low) == 2 and low[0] in ('a', 'an', 'the'):
                continue            # 冠词+名词基础组合(the girls / a kiss)无背诵价值
            if not _is_good(low, oxford, db):
                continue
            key = ' '.join(low)
            if key not in seen:
                seen.add(key)
                yield key, s


def extract_phrases(sents, oxford, db, book_counts, top, seen=None):
    """章内表达候选:块(全书出现 ≥2 次)→ 打分 → top N;seen 为跨章已推荐的 phrase 集合,
    已推荐过的跳过(同表达只在最早出现的章节推荐一次)。
    score = 动词短语 +2.0 · 任一词 C1/toe +1.5 · B2 +1.0 · B1 +0.5 · 章内频次小加分"""
    seen = set() if seen is None else seen
    cnt = {}
    for key, s in chapter_chunks(sents, oxford, db):
        d = cnt.setdefault(key, [0, ''])
        d[0] += 1
        if not d[1] or len(s) < len(d[1]):
            d[1] = s
    scored = []
    for key, (n, ex) in cnt.items():
        fb = book_counts.get(key, 0)
        low = key.split()
        phrasal = len(low) == 2 and low[1] in PARTICLES
        if fb <= 1 and not phrasal:
            continue                # PLAN 12.2.1:全书仅 1 次的组合不候选(无重复即无搭配价值);
                                    # 例外:动词短语(take off / walked off)一次也算稳定构式,保留
        sc = 2.0 if phrasal else 0.0
        max_r, max_lv = 0, ''
        for w in low:
            hit = oxford.get(w)
            lv = hit[1] if hit else _ec_level(w, db)
            if lv in ('c1', 'toe'):
                sc += 1.5
            elif lv == 'b2':
                sc += 1.0
            elif lv == 'b1':
                sc += 0.5
            r = LEVEL_RANK.get(lv) or (6 if lv == 'toe' else 0)
            if r > max_r:
                max_r, max_lv = r, lv
        sc += min(n, 5) * 0.2
        scored.append({
            'phrase': key, 'freq_ch': n,
            'freq_book': fb,
            'score': round(sc, 2), 'cefr_max': max_lv or 'a2',
            'example': ex,
        })
    scored.sort(key=lambda x: -x['score'])
    out = []
    for c in scored:
        if c['phrase'] in seen:
            continue
        if len(out) >= top:
            break
        seen.add(c['phrase'])
        out.append(c)
    return out


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
    ap.add_argument('--phrases', action='store_true',
                    help='表达候选模式:只输出 work/phrase_cands_<book>.json,不写 raw CSV')
    ap.add_argument('--phrase-top', type=int, default=40,
                    help='每章表达候选数(配合 --phrases,默认 40)')
    args = ap.parse_args()

    global NOVEL_PROPER
    NOVEL_PROPER = proper_names.load(args.book)
    if NOVEL_PROPER:
        print(f'proper: {args.book} 书内专名 {len(NOVEL_PROPER)} 个'
              f'({proper_names.path_for(args.book)})', flush=True)

    md_path = os.path.join(BASE, 'data', 'books', '_md', f'{args.book}.md')
    if not os.path.exists(md_path):
        sys.exit(f'[STOP] 找不到管线输入 {md_path} —— 新书先跑 scripts/epub_to_md.py'
                 f'(EPUB 转换)与 scripts/scan_proper.py(专名扫描),完整流程见 README.agent.md')
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

    # 全书词频(用于全局加权):断点续跑时仍需全部章节的词频统计(只算不重出卡)。
    # 分析结果按章缓存,主循环/--phrases 复用,全书只 analyze 一遍(避免重复全量分析)
    book_freq = {}
    book_phrase_counts = {}   # 全书表达块频次(--phrases 模式用,口径与 extract_phrases 一致)
    chapter_data = {}         # ch_num -> (sents, stats, sent_recs)
    for ch in split_chapters(md_text):
        sents, stats, sent_recs = analyze_chapter(ch['body'])
        chapter_data[ch['num']] = (sents, stats, sent_recs)
        for lem, st in stats.items():
            book_freq[lem] = book_freq.get(lem, 0) + st['freq']
        if args.phrases:
            for key, _ in chapter_chunks(sents, oxford, db):
                book_phrase_counts[key] = book_phrase_counts.get(key, 0) + 1

    b1_max = max(1, round(args.per_chapter * args.b1_quota))
    toe_max = max(1, round(args.per_chapter * args.toe_quota))
    print(f'quotas: b1<={b1_max} toe<={toe_max} of {args.per_chapter}', flush=True)

    # 跨书去重:总库中已出过卡(card_exported=1)或已掌握的词不再推荐,
    # 除非 --include-exported 显式要求重选(如调整参数后有意重新出卡)
    seen = set()   # 章内/书内去重 + 跨书已出卡词
    # 已掌握词(known_words.txt)绝不重复推荐:用户声明认识,重选也不豁免
    kpath = os.path.join(BASE, 'vocabulary', 'known_words.txt')
    if os.path.exists(kpath):
        with open(kpath, encoding='utf-8') as f:
            known = {w.strip().lower() for w in f
                     if w.strip() and not w.startswith('#')}
        if known:
            seen |= known
            print(f'known: 跳过已掌握词 {len(known)} 个', flush=True)
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
    # 表达候选模式:只出候选 JSON,不写 raw CSV(保护已有润色)
    if args.phrases:
        work_dir = os.path.join(out_dir, 'work')
        os.makedirs(work_dir, exist_ok=True)
        all_phrase_cands = {}
        phrase_seen = set()   # 跨章:同一表达只在最早出现的章节推荐
        for ch in chapters:
            sents, stats, _ = chapter_data[ch['num']]
            cands = extract_phrases(sents, oxford, db, book_phrase_counts,
                                    args.phrase_top, seen=phrase_seen)
            all_phrase_cands[str(ch['num'])] = cands
            print(f'ch {ch["num"]}: {len(cands)} 个表达候选 (top {args.phrase_top})', flush=True)
        out_json = os.path.join(work_dir, f'phrase_cands_{args.book}.json')
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump({
                'book': args.book,
                'per_chapter_top': args.phrase_top,
                'note': '表达候选(AI/人挑选后写 phrases_picked JSON,apply_polish.py --phrases 合并出卡)',
                'chapters': all_phrase_cands,
            }, f, ensure_ascii=False, indent=1)
        db.close()
        print('DONE ->', out_json, flush=True)
        return
    for ci, ch in enumerate(chapters):
        sents, stats, sent_recs = chapter_data[ch['num']]
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
            # 补位只允许取配额溢出的候选(b1/toe 超配额);seen 词(已出卡/已掌握)
            # 绝不补位重选,宁缺毋滥
            for c in cands:
                if c['word'] in picked_words or c['word'] in seen:
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
                                     'chapter', 'date'],
                                    lineterminator='\n')
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