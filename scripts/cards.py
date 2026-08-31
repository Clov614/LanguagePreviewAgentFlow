"""卡片与总库组装(阶段 8-11):
输入:  data/output/<book>/raw/chapter_XX_raw.csv(含模型润色列 cn_mean / cn_sent)
       vocabulary/master_wordlist.csv(生词总库)
流程:  → 每章 Anki TSV(UTF-8, 首行字段名)
       → 更新生词总库(去重合并:同词累积来源/频次,不重复推荐已 known 词)
用法:  uv run python scripts/cards.py --book little_women --chapter 1
"""
import argparse, csv, datetime, html, os, re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from tts_paths import audio_dir_for_book, sent_audio_name, word_audio_name
from wordforms import phrase_regex, token_regex
from hard_words import Difficulty, mark_sentence
import proper_names

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOCAB = os.path.join(BASE, 'vocabulary')
WORDLIST = os.path.join(VOCAB, 'master_wordlist.csv')
KNOWN = os.path.join(VOCAB, 'known_words.txt')

def load_known():
    if not os.path.exists(KNOWN):
        return set()
    return {w.strip().lower() for w in open(KNOWN, encoding='utf-8')
            if w.strip() and not w.startswith('#')}

def ensure_wordlist_schema():
    os.makedirs(VOCAB, exist_ok=True)
    if not os.path.exists(WORDLIST):
        with open(WORDLIST, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f, lineterminator='\n')
            w.writerow(['word', 'cefr', 'first_seen', 'status', 'book', 'chapters',
                        'freq_book', 'sources', 'example_en', 'example_cn',
                        'recommended_date', 'card_exported'])

def _src_key(e, key):
    """sources 列: 'book|chN|freq_book' 分号分隔 —— 跨书词频累积的幂等依据。
    key = 'book|chN',条目允许 'book|chN|freq' 尾缀,按前缀匹配幂等。"""
    for s in (e.get('sources') or '').split(';'):
        if s == key or s.startswith(key + '|'):
            return True
    return False

def merge_wordlist(rows, book, chapter):
    """把本章选中词并入总库;返回 (新词数, 已存在数, 跳过已知词数)
    幂等:同一 (书, 章) 重复执行不会重复累加 freq_book / 追加章节号。
    freq_book 增量 = 该章(本章新词为全书频次,复用词为准许的增量),见 _srcs_of。
    """
    ensure_wordlist_schema()
    known = load_known()
    existing = {}
    if os.path.exists(WORDLIST):
        with open(WORDLIST, encoding='utf-8', newline='') as f:
            for r in csv.DictReader(f):
                existing[r['word']] = r
    today = datetime.date.today().isoformat()
    key = f'{book}|ch{chapter}'
    new_cnt = exist_cnt = skip_cnt = 0
    for src in rows:
        w = src['word']
        if w in known:
            skip_cnt += 1
            continue
        freq_inc = int(src.get('freq_book') or 0)
        if w in existing:
            e = existing[w]
            if not _src_key(e, key):
                e['sources'] = (e.get('sources') or '').rstrip(';') + f';{key}|{freq_inc}'
                if freq_inc:
                    e['freq_book'] = str(int(e['freq_book'] or 0) + freq_inc)
            e['sources'] = e['sources'].lstrip(';')
            chs = [c for c in (e.get('chapters') or '').split(',') if c]
            if str(chapter) not in chs:
                chs.append(str(chapter))
            e['chapters'] = ','.join(chs)
            e['example_en'] = src.get('sent', '') or e['example_en']
            e['example_cn'] = src.get('cn_sent', '') or e['example_cn']
            e['recommended_date'] = e.get('recommended_date') or today
            e['card_exported'] = '1'
            if e.get('status') != 'known':
                e['status'] = 'active'   # 已掌握行保持 known,绝不翻转回 active
            exist_cnt += 1
        else:
            existing[w] = {
                'word': w, 'cefr': src['cefr'],
                'first_seen': src.get('date') or today, 'status': 'active',
                'book': book, 'chapters': str(chapter),
                'freq_book': src.get('freq_book', 0),
                'sources': f'{key}|{freq_inc}' if freq_inc else key,
                'example_en': src.get('sent', ''), 'example_cn': src.get('cn_sent', ''),
                'recommended_date': src.get('date') or today, 'card_exported': '1',
            }
            new_cnt += 1
    with open(WORDLIST, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(existing.values())[0].keys()
                           if existing else ['word'], lineterminator='\n')
        if existing:
            w.writeheader()
            w.writerows(existing.values())
    return new_cnt, exist_cnt, skip_cnt

def tsv_cell(v):
    """TSV 单元格消毒:制表符/换行会破坏 10 列结构"""
    return str(v or '').replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')

def hl_sentence(esc_sent, word):
    """已转义例句上把目标词(含屈折形态,见 wordforms)裹成 <b class="hl">。
    调用前例句需已 html.escape(由 mark_sentence 完成),此处不再转义,防双重转义。
    导入 Anki 时「允许在字段中使用 HTML」需勾选,效果由模板 CSS 的 .sent b.hl 控制。"""
    if not esc_sent or not word:
        return esc_sent
    pat = token_regex([word])
    if pat is None:
        return esc_sent
    return pat.sub(lambda m: f'<b class="hl">{m.group()}</b>', esc_sent)


def hl_phrase(esc_sent, phrase):
    """表达卡例句高亮:整短语高亮(逐词屈折展开 + 首尾词边界,见 wordforms.phrase_regex)。
    例证句取自原书候选(表面形),短语按表面形逐词展开规则屈折匹配;
    注:词形表不做跨词换形(如 made 展开不出 making),例证句即原句时可命中。"""
    if not esc_sent or not phrase:
        return esc_sent
    pat = phrase_regex(phrase)
    return pat.sub(lambda m: f'<b class="hl">{m.group()}</b>', esc_sent)


# ---------------- AI 解析排版归一(渲染收敛) ----------------
# 模型输出排版随模型/批次漂移,实测历史数据有近十种:1.1 两级编号、①②③ 圆圈号、无段首行
# 且用 "# 整句解读#" 把段落拼进行内、「逐项解析」引号段标、段名带 (注释) 后缀、【2. 例句
# 逐词解析】别名段名、换行双转义成字面 \n、整句解读漏写段首行等。渲染前统一收敛为固定版式:
# 段首行 <b>N. 段名</b> / 条目 「• 」 / 词级 「– 」 / 行首成分加粗 + 半角冒号。

_SECTION_TITLES = ('逐项解析', '整句解读', '文化点')

# 段首行:容忍 "1. / 1、 / 一、 / # / 【】 / 「」 / **" 等修饰(序号可写在括号内,如
# 【2. 例句逐词解析】);标题后允许 "(注释)"(如 逐项解析（把例句拆成零件逐一讲解）),
# 其后必须紧跟冒号或行尾,否则视为正文(防误伤以段名开头的句子)
_SECTION_RE = re.compile(
    r'^(?:#{1,6}\s*)?[*【\[「]*\s*'
    r'(?:(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[.、)．]?\s*)?'
    r'(例句逐词解析|逐词解析|逐项解析|整句解读|整句理解|整句赏析|文化点|文化背景)'
    r'\s*[*\]】」]*\s*(?:（[^（）]{0,30}）)?\s*([:：]?)\s*(.*)$')

# 段名别名 → 规范段名(模型会换着叫,意思一样)
_TITLE_ALIAS = {'例句逐词解析': '逐项解析', '逐词解析': '逐项解析',
                '整句理解': '整句解读', '整句赏析': '整句解读',
                '文化背景': '文化点'}

# 条目行首编号:1.1 两级 / 1. 1、 1) / ①-⑳ / (1) (1) / 圆点列表符
_ITEM_RE = re.compile(
    r'^\s*(?:\d{1,2}\.\d{1,2}\s+|\d{1,2}\s*[.、)．]\s+|[①-⑳]\s*'
    r'|[(（]\d{1,2}[)）]\s*|[*•·-]\s+)')

_BULLET_LEAD_RE = re.compile(r'^\s*(?:\d|[①-⑳]|[(（]\d)')
_WORD_TAG_RE = re.compile(r'^(.{1,60}?)\s*([(（](?:目标词|超纲词)[)）])\s*(?:[:：]\s*)?(.*)$', re.S)


def _bold_head(text):
    """条目/词级行的行首成分统一加粗、分隔符归一为半角冒号:
    <b>x</b> —— / <b>x</b>: / x: / x—— / x(身份) 收敛为 <b>x</b>:… 或 <b>x</b>(身份):…。
    已有加粗且其后无分隔符、成分超长/含句读/含既有标签时一律不动:宁可漏加,不可错加。"""
    m = re.match(r'^(<b>.*?</b>)(?:\s*(?:[—─]{1,2}|[:：])\s*)(.*)$', text, re.S)
    if m:
        return m.group(1) + ':' + m.group(2)
    m = _WORD_TAG_RE.match(text)
    if m and '<b>' not in m.group(1) and m.group(3).strip():
        # 「词(身份):解析」前缀格式才重写;身份标记在句尾作注(无后文)时保持原样
        return f'<b>{m.group(1)}</b>{m.group(2)}:{m.group(3)}'
    m = re.match(r'^(.{1,60}?)\s*(?:[—─]{1,2}|[:：])\s*(.*)$', text, re.S)
    if m and m.group(1).strip() and '<b>' not in m.group(1) \
            and not re.search(r'[。！？!?;；]', m.group(1)):
        return f'<b>{m.group(1).strip()}</b>:{m.group(2)}'
    return text


def _normalize_ai_structure(raw):
    """把任意模型排版重排为规范结构(纯文本操作,输出里的 <b> 均为本函数/模型合法加粗):
    1. 段首行归一:三种段名统一为顶行 <b>N. 段名</b>(N 按固定顺序 1/2/3,不依赖模型编号),
       模型把段落用 "# 整句解读#" / 【整句解读】 拼进行内的,拆成独立段首行;
       逐项解析段首行整体缺失但存在条目时,自动补 <b>1. 逐项解析</b>。
    2. 条目归一:逐项解析区间内 1.1 / 1. / ① / (1) / 圆点 等行首编号统一为 「• 」;
       区内既有编号条目又有圆点行时,圆点行是词级拆解 → 「– 」;全区只有圆点则圆点即条目。
    3. 条目/词级行首成分加粗(_bold_head)。
    无段无条目的纯文本(词义概述、表达卡讲解)原样通过,不做任何加工。"""
    raw = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', raw, flags=re.S)   # 手改 JSON 兜底
    raw = raw.replace('\\n', '\n')   # 个别模型把换行双转义成字面 \n,先还原为真换行
    # 整段挤成一行的(模型偶尔把三段写成一段):按 "N. 段名:" 行内段标切行
    # (?<![*)\d] 防误切 md 加粗残留/两级编号
    raw = re.sub(r'\s*(?<![*)\d])(\d{1,2})\.\s*(逐项解析|整句解读|文化点)\s*[:：]\s*',
                 r'\n\n\1. \2\n', raw)
    for t in _SECTION_TITLES:   # 行内卡住的段标记(如 「。# 整句解读# …」)拆为独立行
        raw = re.sub(rf'\s*#{{1,6}}\s*{t}\s*#*\s*', '\n\n' + t + '\n', raw)
        raw = re.sub(rf'\s*【{t}】\s*', '\n\n' + t + '\n', raw)

    records, cur = [], None
    n_numbered = n_bullets = 0      # 逐项解析区内:编号条目数 / 圆点行数(决定圆点角色)
    first_loose_item = None         # 段首行缺失时首个悬空条目的下标(补段首行用)
    prev_blank = False              # 当前行之前是否有空行(补「整句解读」段首行的依据)
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        s = line.strip()
        if not s:
            prev_blank = True
            continue
        m = _SECTION_RE.match(s)
        if m and (m.group(2) or not m.group(3).strip()):
            cur = _TITLE_ALIAS.get(m.group(1), m.group(1))
            if cur == '逐项解析':
                n_numbered = n_bullets = 0
            records.append(['header', cur, False])
            if m.group(3).strip():
                records.append(['line', m.group(3).strip(), False])
            prev_blank = False
            continue
        if cur in (None, '逐项解析'):
            im = _ITEM_RE.match(line)
            if im:
                kind = 'item' if _BULLET_LEAD_RE.match(im.group(0)) else 'bullet'
                if kind == 'item':
                    n_numbered += 1
                else:
                    n_bullets += 1
                records.append([kind, line[im.end():].strip(), False])
                if cur is None and first_loose_item is None:
                    first_loose_item = len(records) - 1
                prev_blank = False
                continue
            if cur == '逐项解析' and s.count('；') >= 2:
                # 单行分号连排的逐词讲解(ch47 实测):按 「；+英文词」 边界拆成条目
                parts = [p.strip() for p in re.split(r'；(?=[A-Za-z“「\'"])', s) if p.strip()]
                if len(parts) >= 3:
                    records.extend(['item', p, False] for p in parts)
                    prev_blank = False
                    continue
        elif cur in ('整句解读', '文化点') and re.match(r'^[①-⑳]\s', s):
            records.append(['olist', re.sub(r'^[①-⑳]\s*', '', s), prev_blank])
            prev_blank = False
            continue
        records.append(['line', s, prev_blank])
        prev_blank = False

    if first_loose_item is not None and not any(
            k == 'header' and v == '逐项解析' for k, v, _ in records):
        records.insert(first_loose_item, ['header', '逐项解析', False])
    # 条目区之后跟着空行隔开的无段首行长段(模型漏写「2. 整句解读」段首行)→ 补段首行
    if not any(k == 'header' and v == '整句解读' for k, v, _ in records):
        item_idx = [i for i, (k, _, _) in enumerate(records) if k in ('item', 'bullet')]
        for i in range(item_idx[-1] + 1, len(records)) if item_idx else ():
            k, v, was_blank = records[i]
            if k == 'line' and was_blank and len(v) >= 20:
                records.insert(i, ['header', '整句解读', False])
                break
    if n_numbered == 0 and n_bullets > 0:   # 全区只有圆点:圆点本身就是条目,不是词级
        records = [['item', v, w] if k == 'bullet' else [k, v, w] for k, v, w in records]
    elif n_bullets > 0:
        records = [['word', v, w] if k == 'bullet' else [k, v, w] for k, v, w in records]

    out = []
    for kind, val, _ in records:
        if out and kind in ('header', 'item', 'olist'):   # 条目与段首行前插空行,独立成段
            out.append('')
        if kind == 'header':
            out.append(f'<b>{_SECTION_TITLES.index(val) + 1}. {val}</b>')
        elif kind == 'item':
            out.append('• ' + _bold_head(val))
        elif kind == 'olist':
            out.append('• ' + val)      # 整句解读/文化点里的 ①② 枚举段 → 圆点(不加粗)
        elif kind == 'word':
            out.append('– ' + _bold_head(val))
        else:
            out.append(val)
    return '\n'.join(out)


def ai_cell_html(v):
    """AI 解析 / 词义概述格:排版归一 + 转义 + 换行→<br>。
    TSV 单元格保持单行(不破坏 10 列),Anki 内渲染为多行段落。

    - 结构收敛见 _normalize_ai_structure(段首行/条目/词级/行首成分加粗);
    - 每条目与各段首行前自动插空行,独立成段;
    - 身份标记上色:(目标词) → <b class="hl">(例句高亮红),(超纲词) →
      <b class="hard">(超纲绿,同例句超纲词配色),模板 .ai 需有对应 CSS;
    - 转义后仅白名单还原我们自己生成的 <b>(三种形态),其余尖括号保持转义防注入。"""
    if not v:
        return ''
    esc = html.escape(_normalize_ai_structure(str(v)))
    esc = esc.replace('\n', '<br>')
    esc = (esc.replace('&lt;b&gt;', '<b>')
              .replace('&lt;/b&gt;', '</b>')
              .replace('&lt;b class=&quot;hl&quot;&gt;', '<b class="hl">')
              .replace('&lt;b class=&quot;hard&quot;&gt;', '<b class="hard">'))
    esc = re.sub(r'[(（](目标词|超纲词)[)）]',
                 lambda m: '<b class="%s">(%s)</b>' % (
                     'hl' if m.group(1) == '目标词' else 'hard', m.group(1)),
                 esc)
    return esc

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--chapter', type=int, help='只处理指定章(调试用)')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    chapters_dir = os.path.join(BASE, 'data', 'books', '_md')
    md_path = os.path.join(chapters_dir, f'{args.book}.md')
    if not os.path.exists(md_path):
        sys.exit(f'[STOP] 找不到管线输入 {md_path} —— 先跑 scripts/epub_to_md.py(EPUB 转换)')
    with open(md_path, encoding='utf-8') as f:
        md_text = f.read()

    diff = Difficulty()   # 超纲词判定资源(oxford5000 + ECDICT),加载一次全程复用
    diff.proper = proper_names.load(args.book)   # 书内专名不标超纲(scripts/proper_names.py)
    stack = os.path.join(out_dir, 'raw')
    anki_dir = os.path.join(out_dir, 'anki')
    os.makedirs(anki_dir, exist_ok=True)
    # gen_audio.py 预生成的 mp3 缓存;存在则把 [sound:] 内嵌进 单词/原文例句 单元格
    audio_dir = audio_dir_for_book(BASE, args.book)
    # 单词 raw 与表达 raw(chapter_XX_phrase_raw.csv,apply_polish --phrases 产物)分列收集
    word_files = sorted(f for f in os.listdir(stack)
                        if f.endswith('_raw.csv') and not f.endswith('_phrase_raw.csv'))
    phrase_files = sorted(f for f in os.listdir(stack) if f.endswith('_phrase_raw.csv'))
    if args.chapter:
        word_files = [f for f in word_files if f.startswith(f'chapter_{args.chapter:02d}_')]
        phrase_files = [f for f in phrase_files if f.startswith(f'chapter_{args.chapter:02d}_')]
    total_new = total_exist = total_skip = 0
    total_ph = 0
    total_known_skip = 0
    known = load_known()   # 已掌握词绝不写入 TSV(重跑 cards 也不重新出卡)
    chs = sorted({int(f.split('_')[1]) for f in word_files}
                 | {int(f.split('_')[1]) for f in phrase_files})
    for ch in chs:
        rows = []
        # 表达卡在前(同章 TSV 表达排最前,先刷表达再刷单词)
        pfn = f'chapter_{ch:02d}_phrase_raw.csv'
        if pfn in phrase_files:
            with open(os.path.join(stack, pfn), encoding='utf-8-sig', newline='') as f:
                rows += [r for r in csv.DictReader(f) if r.get('cn_mean')]
        wfn = f'chapter_{ch:02d}_raw.csv'
        if wfn in word_files:
            with open(os.path.join(stack, wfn), encoding='utf-8-sig', newline='') as f:
                rows += [r for r in csv.DictReader(f) if r.get('cn_mean')]
        if not rows:
            print(f'[SKIP] {wfn}: 无可出卡词(全部缺润色)', flush=True)
            continue
        # known 过滤必须在写 TSV 之前:gen_audio 后重跑 cards 是常规操作,
        # 不能把已标记掌握的词重新写回卡片
        n_known = sum(1 for r in rows if r['word'].lower() in known)
        rows = [r for r in rows if r['word'].lower() not in known]
        total_known_skip += n_known
        if n_known:
            print(f'[SKIP] known: {n_known} 词不出卡', flush=True)
        if not rows:
            print(f'[SKIP] {wfn}: 本章全部为 known 词,跳过出卡', flush=True)
            continue
        # 表达卡计数在 known 过滤之后统计:若被过滤的是短语,不虚报汇总数
        n_ph = sum(1 for r in rows if r.get('pos') == 'phrase')
        # 每章 TSV(无 BOM 的 UTF-8:Anki 对 BOM 敏感)
        tsv_path = os.path.join(anki_dir, f'chapter_{ch:02d}_anki.tsv')
        with open(tsv_path, 'w', encoding='utf-8', newline='') as f:
            f.write('单词\t音标\t词性\t中文释义\tCEFR\t原文例句\t例句译文\t来源\tAI解析\t词义概述\n')
            for r in rows:
                src = f'{args.book} Ch{ch}'
                if r.get('ai') == '1':
                    src += '(AI 补句)'
                word = r['word']
                sent = r['sent'] or ''
                cefr = (r['cefr'] or '').upper()
                if r.get('pos') == 'phrase':
                    # 表达卡:词级音频 w_<短语>.mp3(gen_audio 同单词规则生成);例句转义后整短语高亮(不标超纲词)
                    esc_sent = html.escape(sent)
                    sent_cell = hl_phrase(esc_sent, word)
                    s_name = sent_audio_name(ch, word) if sent else None
                    if s_name and (audio_dir / s_name).exists():
                        sent_cell += f' [sound:{s_name}]'
                    w_name = word_audio_name(word)
                    word_cell = word + (f' [sound:{w_name}]'
                                        if (audio_dir / w_name).exists() else '')
                else:
                    w_name = word_audio_name(word)
                    s_name = sent_audio_name(ch, word) if sent else None
                    word_cell = word + (f' [sound:{w_name}]'
                                        if (audio_dir / w_name).exists() else '')
                    esc_sent, _ = mark_sentence(sent, word, cefr, diff)  # 转义 + 包超纲词 hard
                    sent_cell = (hl_sentence(esc_sent, word) +
                                 (f' [sound:{s_name}]'
                                  if s_name and (audio_dir / s_name).exists() else ''))
                f.write('\t'.join([
                    tsv_cell(word_cell), tsv_cell(r['phon']), tsv_cell(r['pos'] or '—'),
                    tsv_cell(r['cn_mean']), tsv_cell(cefr), tsv_cell(sent_cell),
                    tsv_cell(r.get('cn_sent', '')), tsv_cell(src),
                    tsv_cell(ai_cell_html(r.get('ai_analysis'))),
                    tsv_cell(ai_cell_html(r.get('memo'))),
                ]) + '\n')
        n, e, s = merge_wordlist(rows, args.book, ch)
        total_new += n; total_exist += e; total_skip += s
        total_ph += n_ph
        print(f'[OK]  ch{ch}: 卡片 {len(rows) - n_ph} 词 + {n_ph} 表达 | '
              f'总库 +{n} 复用{e} 跳过已知{s}', flush=True)
    print(f'DONE 总库更新: 新增{total_new} 复用{total_exist} 跳过{total_skip}'
          f'(known {total_known_skip}) | 表达卡共 {total_ph} 条', flush=True)

if __name__ == '__main__':
    main()
