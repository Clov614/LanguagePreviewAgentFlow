"""书内专名表(人名/地名等)的加载与疑似扫描 —— pipeline / hard_words / validate 共用。

机制(2026-08-31 起,替代 pipeline.py 硬编码 NOVEL_PROPER 与 hard_words.py 的
PROPER_NAMES 副本 —— 每本书手改代码不可持续,且 hard_words 里的副本只认识旧书人物):
  - 每本书一份 data/books/proper_names/<book>.txt:一行一个小写 lemma,`#` 注释,
    空行跳过;文件缺省 = 该书无书内专名
  - scripts/scan_proper.py 负责发现候选(本模块 suspects()),人工/agent 确认后写入文件
  - 消费方:pipeline.py(选词+语块过滤)、hard_words.py(超纲词标注豁免,经 diff.proper)、
    validate.py(兜底:入选词撞专名表 → FAIL;疑似专名漏网 → 警告)

疑似判定(零模型,与 pipeline.is_proper 同源但更稳):
  不在 Oxford 5000、长度 ≥2 的纯小写 lemma,且满足任一:
  - 几乎总以大写形出现:cap_ratio ≥ 0.5 且 freq ≥ 3(能抓住「常作句首主语」的人名,
    这类词 is_proper 的非首大写占比会漏,如 Jerusha)
  - 句中非首大写 ≥2 且占比 ≥ 0.35(is_proper 的原判据)
  - 低频(≤6)且句中非首大写 ≥1(is_proper 的原判据)
"""
import os, re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROPER_DIR = os.path.join(BASE, 'data', 'books', 'proper_names')
WORD_RE = re.compile(r"[A-Za-z]+(?:[’'-][A-Za-z]+)*")

_cache = {}


def path_for(book):
    return os.path.join(PROPER_DIR, f'{book}.txt')


def load(book):
    """书内专名集合(小写 lemma);文件不存在 = 空集;结果按书缓存"""
    if book in _cache:
        return _cache[book]
    words = set()
    p = path_for(book)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            for ln in f:
                w = ln.split('#', 1)[0].strip().lower()
                if w:
                    words.add(w)
    _cache[book] = frozenset(words)
    return _cache[book]


def suspects(md_text, oxford, exclude=frozenset(), want_contexts=False):
    """全书疑似专名扫描,返回 [{word, freq, cap, nu, score[, contexts]}](score 降序)。
    md_text: 管线输入 MD 全文;oxford: load_oxford() 产物(词表内不判);
    exclude: 已确认专名(不重复报);want_contexts: 顺带每词至多 2 个含词例句
    (ai_pick_proper 用,同一次分析不重复 lemmatize)。统计口径与 pipeline 一致。"""
    import simplemma
    from pipeline import split_chapters, analyze_chapter
    freq, nu, cap, ctx = {}, {}, {}, {}
    for ch in split_chapters(md_text):
        sents, stats, _ = analyze_chapter(ch['body'])
        for lem, st in stats.items():
            freq[lem] = freq.get(lem, 0) + st['freq']
            nu[lem] = nu.get(lem, 0) + st['nonfirst_upper']
        for s in sents:
            clean = None
            for raw in WORD_RE.findall(s):
                if raw[0].isupper():
                    lem = simplemma.lemmatize(raw.lower(), lang='en').lower()
                    cap[lem] = cap.get(lem, 0) + 1
                    if want_contexts:
                        buf = ctx.setdefault(lem, [])
                        if len(buf) < 2:
                            if clean is None:
                                clean = s.replace('\n', ' ').strip()
                            buf.append(clean)
    out = []
    for lem, f in freq.items():
        if lem in oxford or lem in exclude:
            continue
        if not re.fullmatch(r"[a-z]{2,}", lem):
            continue
        c, n = cap.get(lem, 0), nu.get(lem, 0)
        cr, nr = c / f, n / f
        if ((cr >= 0.5 and f >= 3) or (n >= 2 and nr >= 0.35)
                or (f <= 6 and n >= 1)):
            item = {'word': lem, 'freq': f, 'cap': c, 'nu': n,
                    'score': round(cr * 2 + nr + min(f, 30) / 30, 3)}
            if want_contexts:
                item['contexts'] = ctx.get(lem, [])
            out.append(item)
    out.sort(key=lambda x: -x['score'])
    return out
