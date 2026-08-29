"""卡片与总库组装(阶段 8-11):
输入:  data/output/<book>/chapter_XX_raw.csv(含模型润色列 cn_mean / cn_sent)
       vocabulary/master_wordlist.csv(生词总库)
流程:  → 每章 Anki TSV(UTF-8, 首行字段名)
       → 更新生词总库(去重合并:同词累积来源/频次,不重复推荐已 known 词)
用法:  uv run python scripts/cards.py --book little_women --chapter 1
"""
import argparse, csv, datetime, html, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from tts_paths import audio_dir_for_book, sent_audio_name, word_audio_name
from wordforms import token_regex

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
            w = csv.writer(f)
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
            e['status'] = 'active'
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
                           if existing else ['word'])
        if existing:
            w.writeheader()
            w.writerows(existing.values())
    return new_cnt, exist_cnt, skip_cnt

def tsv_cell(v):
    """TSV 单元格消毒:制表符/换行会破坏 8 列结构"""
    return str(v or '').replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')

def hl_sentence(sent, word):
    """例句中高亮目标词:HTML 转义后把目标词(含屈折形态,见 wordforms)裹成 <b class="hl">。
    转义不影响单词字符;导入 Anki 时「允许在字段中使用 HTML」需勾选,
    显示效果由模板 CSS 的 .sent b.hl 控制。"""
    if not sent:
        return sent
    esc = html.escape(sent)
    pat = token_regex([word])
    return pat.sub(lambda m: f'<b class="hl">{m.group()}</b>', esc)

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
    # gen_audio.py 预生成的 mp3 缓存;存在则把 [sound:] 内嵌进 单词/原文例句 单元格
    audio_dir = audio_dir_for_book(BASE, args.book)
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
                src = f'{args.book} Ch{ch}'
                if r.get('ai') == '1':
                    src += '(AI 补句)'
                word = r['word']
                sent = r['sent'] or ''
                w_name = word_audio_name(word)
                s_name = sent_audio_name(ch, word) if sent else None
                word_cell = word + (f' [sound:{w_name}]'
                                    if (audio_dir / w_name).exists() else '')
                sent_cell = (hl_sentence(sent, word) +
                             (f' [sound:{s_name}]'
                              if s_name and (audio_dir / s_name).exists() else ''))
                f.write('\t'.join([
                    tsv_cell(word_cell), tsv_cell(r['phon']), tsv_cell(r['pos'] or '—'),
                    tsv_cell(r['cn_mean']), tsv_cell(r['cefr'].upper()), tsv_cell(sent_cell),
                    tsv_cell(r.get('cn_sent', '')), tsv_cell(src),
                ]) + '\n')
        n, e, s = merge_wordlist(rows, args.book, ch)
        total_new += n; total_exist += e; total_skip += s
        print(f'[OK]  ch{ch}: 卡片 {len(rows)} 词 | 总库 +{n} 复用{e} 跳过已知{s}', flush=True)
    print(f'DONE 总库更新: 新增{total_new} 复用{total_exist} 跳过{total_skip}', flush=True)

if __name__ == '__main__':
    main()