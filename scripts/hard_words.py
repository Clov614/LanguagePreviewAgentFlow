"""超纲词判定(例句中比目标词更难的词) — 本地规则,零模型调用。
供 cards.py(渲染 <b class="hard">)与 ai_explain.py(prompt 喂难度词列表)共用。

判定基准:
- 目标词级别 = raw CSV 的 cefr 列(pipeline 判定)
- 例句中某词的级别:oxford5000(词表内级别)→ ECDICT 考试标签粗分 → 均未命中=超纲(toe)
- "比目标词难" = 级别更高;同级别不标;toe 词需 bnc 低频佐证(防冷门常见词噪声)

排除:目标词自身(含屈折形态)、专名(句中非首词大写 或 书内人名表)、停用词、短词。
数量:每句至多 max_n 个(默认 2),宁缺勿滥。
"""
import html
import os
import re
import sqlite3
import sys
import threading

import simplemma
from wordforms import IRREGULAR

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(BASE, 'resources')

WORD_RE = re.compile(r"[A-Za-z]+(?:[’'-][A-Za-z]+)*")
LANG = 'en'

# 反向索引:屈折形态 → 不规则动词 lemma。
# simplemma 对 felt/saw/rose/laid 等歧义形会返回独立词典形(如 felt=毛毡),
# 而语料中它们几乎总是动词过去式 —— 反向表优先认作动词,防高频词误标成超纲。
_IRR_BY_FORM = {}
for _lem, _forms in IRREGULAR.items():
    for _f in _forms:
        _IRR_BY_FORM.setdefault(_f, _lem)


def _lemmatize(low):
    lem = simplemma.lemmatize(low, lang=LANG)
    return _IRR_BY_FORM.get(low, lem)


def lemma_of(w):
    """英文词/短语 → 首词 lemma(小写)。公开助手:供 ai_explain 组装时判定词身份。"""
    m = WORD_RE.search(w or '')
    return _lemmatize(m.group().lower()) if m else ''

# 书内专名:按书外置(见 scripts/proper_names.py),调用方设 diff.proper 后生效
# (cards.py / ai_explain.py 各自 load(book) 挂到 Difficulty 实例上)
PROPER_NAMES = frozenset()

STOPWORDS = {
    'oh', 'ah', 'eh', 'ha', 'huh', 'hmm', 'hallo', 'hullo', 'hey', 'ahem',
    "don't", "can't", "won't", "ain't", "it's", "that's", "i'm", "i've",
    "i'll", "i'd", "he's", "she's", "we're", 'tis', 'ye', 'thou', 'thee',
}

LEVEL_RANK = {'a1': 1, 'a2': 2, 'b1': 3, 'b2': 4, 'c1': 5, 'toe': 6}


def _guess_level(tags, frq):
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


class Difficulty:
    """加载 oxford5000 + ECDICT,带查询缓存。

    并发(ai_explain --workers 多章线程池)时**每线程一条独立 sqlite 连接**:
    同一连接跨线程并发 execute() 会错乱(InterfaceError),threading.local 懒建连接,
    首用线程各自持有,互不干扰;查询缓存为只读 dict,并发读写原子,安全。
    """

    def __init__(self):
        self.oxford = self._load_oxford()
        self._cache = {}
        self._local = threading.local()

    def _conn(self):
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(os.path.join(RES, 'ecdict.db'))
            self._local.conn = conn
        return conn

    @staticmethod
    def _load_oxford():
        d = {}
        with open(os.path.join(RES, 'oxford3000-5000', 'oxford-5000.csv'),
                  encoding='utf-8') as f:
            for row in __import__('csv').DictReader(f):
                w = row['word'].lower()
                lv = row['level'].lower().strip()
                if lv not in LEVEL_RANK:
                    continue
                if w not in d or LEVEL_RANK[lv] < LEVEL_RANK[d[w][1]]:
                    d[w] = (row['class'], lv)  # 同词多行取最低级
        return d

    def level_of(self, lem):
        """lemma(小写) → cefr 级别('a1'..'c1' 或 'toe')"""
        w = lem.lower()
        if w in self._cache:
            return self._cache[w]
        hit = self.oxford.get(w)
        if hit:
            lv = hit[1]
        else:
            r = self._conn().execute(
                'SELECT tags, frq FROM dict WHERE word=?', (w,)).fetchone()
            tags, frq = (r if r else (None, 0))
            lv = _guess_level(tags, frq) or 'toe'
        self._cache[w] = lv
        return lv

    def bnc_of(self, lem):
        """lemma → BNC 排名(越小越常见;0=未知)"""
        w = lem.lower()
        r = self._conn().execute('SELECT bnc FROM dict WHERE word=?', (w,)).fetchone()
        return (r[0] or 0) if r else 0


def is_harder(lev, target_lev, bnc):
    """级别更高即更难;同级不标;toe 需 bnc 低频佐证;目标已到顶(toe)不标"""
    if target_lev == 'toe' or lev == target_lev:
        return False
    if target_lev in ('b1', 'b2'):
        return lev in ('c1', 'toe')
    if target_lev == 'c1':
        return lev == 'toe' and (bnc or 0) > 5000
    return False


def hard_words_in(sent, target, target_cefr, diff, max_n=2):
    """返回例句中应标注的超纲词(句中原始词形,按出现顺序,至多 max_n 个)。
    target_cefr 来自 raw CSV(pipeline 判定);空或未知级别时视为无基准 → 空列表。"""
    tlev = (target_cefr or '').lower()
    if tlev not in LEVEL_RANK:
        return []
    tlem = _lemmatize(target.lower())
    proper = getattr(diff, 'proper', PROPER_NAMES)
    toks = WORD_RE.findall(sent)
    out, seen = [], set()
    for i, raw in enumerate(toks):
        low = raw.lower()
        if len(low) < 3 or low in STOPWORDS:
            continue
        lem = _lemmatize(low)
        if not re.fullmatch(r"[a-z]{2,}", lem):
            continue
        if lem == tlem:                     # 目标词自身(含屈折)不标
            continue
        if lem in proper:                   # 书内专名不标
            continue
        if i > 0 and raw[0].isupper():      # 句中大写疑似专名,不标
            continue
        lev = diff.level_of(lem)
        if not is_harder(lev, tlev, diff.bnc_of(lem)):
            continue
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
        if len(out) >= max_n:
            break
    return out


def mark_sentence(sent, target, target_cefr, diff, max_n=2):
    """返回 (esc_html, hard_words):
    - esc_html: 整句 HTML 转义后,把超纲词包成 <b class="hard">…</b>(供 Anki)
    - hard_words: 判定出的超纲词原词形列表(供 ai_explain prompt 与人工审阅)
    """
    if not sent:
        return '', []
    hard = hard_words_in(sent, target, target_cefr, diff, max_n)
    esc = html.escape(sent)
    for w in hard:
        esc = re.sub(r'(?<![A-Za-z])' + re.escape(w) + r'(?![A-Za-z])',
                     lambda m: f'<b class="hard">{m.group()}</b>', esc)
    return esc, hard