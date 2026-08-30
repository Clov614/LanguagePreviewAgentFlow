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


def ai_cell_html(v):
    """AI 解析 / 词义概述格:转义 + 换行→<br>。
    TSV 单元格保持单行(不破坏 10 列),Anki 内渲染为多行段落。

    排版规整(模型编号经常不一致,统一在此收敛):
    - 区块大标题 「N. 逐项解析 / 整句解读 / 文化点」→ 加粗,形成层级;
    - 逐项解析区间内行首编号 "N. " / "N.M"(模型可能输出 1.1 / 1.2,也可能 1. / 2.,两套都有)
      → 条目一律改成单点列表符 「• 」,不再两级编号,标题与条目一眼可分;
    - 逐项解析区间内行首 "- " 或 "• "(ai_explain 提示词约定的词级拆解行)→ 收敛为
      「– 」,比条目低半级,词级与条目清楚分层;
    - 每条目与各区块标题前自动插空行,独立成段;
    - 转义后仅白名单还原我们自己生成的 <b>(标题加粗 / Markdown 加粗转换),
      其余任何尖括号保持转义,防模型输出注入 HTML。"""
    if not v:
        return ''
    esc = html.escape(str(v))
    # 1) 逐项解析区间内:行首编号 "N. " 或 "N.M "(条目)→ "• ";行首 "- "/"• "(词级拆解)→ "– "
    #    必须先于标题加粗做:加粗会给标题行打上 <b> 前缀,状态机就认不出标题行了(历史 bug)
    lines = esc.split('\n')
    in_items = False
    for i, line in enumerate(lines):
        if re.match(r'\d+\.\s*(?:整句解读|文化点)', line):
            in_items = False
        elif re.match(r'\d+\.\s*逐项解析', line):
            in_items = True
        elif in_items and re.match(r'^\d+\.(?:\d+)?\s', line):
            lines[i] = re.sub(r'^\d+\.(?:\d+)?\s', '• ', line)
        elif in_items and re.match(r'^\s*[-•]\s', line):
            lines[i] = re.sub(r'^\s*[-•]\s', '– ', line)
    esc = '\n'.join(lines)
    # 2) 区块标题加粗("1. 逐项解析"、"2. 整句解读"、"3. 文化点…")
    esc = re.sub(r'(?m)^(\d+\.\s*(?:逐项解析|整句解读|文化点)[:：]?)', r'<b>\1</b>', esc)
    # 3) 换行 → <br>;每条目与区块标题前插空行
    esc = esc.replace('\n', '<br>')
    esc = re.sub(r'<br>(?=• )', '<br><br>', esc)
    esc = re.sub(r'<br>(?=<b>\d\. )', '<br><br>', esc)
    # 4) 白名单还原真正的 <b>(仅我们自己打的标题加粗 / md 加粗转换)
    return esc.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--chapter', type=int, help='只处理指定章(调试用)')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    chapters_dir = os.path.join(BASE, 'data', 'books', '_md')
    md_text = open(os.path.join(chapters_dir, f'{args.book}.md'), encoding='utf-8').read()

    diff = Difficulty()   # 超纲词判定资源(oxford5000 + ECDICT),加载一次全程复用
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
                    # 表达卡:多词短语无词级音频;例句转义后整短语高亮(不标超纲词)
                    esc_sent = html.escape(sent)
                    sent_cell = hl_phrase(esc_sent, word)
                    s_name = sent_audio_name(ch, word) if sent else None
                    if s_name and (audio_dir / s_name).exists():
                        sent_cell += f' [sound:{s_name}]'
                    word_cell = word
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
