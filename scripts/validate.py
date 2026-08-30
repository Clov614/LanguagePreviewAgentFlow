"""管线缺口校验(阶段间健康检查):一键发现"缺什么"
检查项:
  1. raw CSV:每章选词数、缺润色(cn_mean)的词、缺例句(sent)的词;+ BOM 断言(必带)
  2. anki TSV:有无出卡、行数是否与润色词数一致、表头与**每行数据**均 10 列、无 BOM
  3. 生词总库:chapters 为空 / card_exported 非 1 / sources 为空的异常行;+ BOM 断言(必无)
  4. 卡片与总库一致性:总库词是否都出自某章 raw(孤儿词报告,信息性不阻塞)
用法:  uv run python scripts/validate.py --book little_women [--verbose] [--prune-orphans [--yes]]
退出码:有缺口 → 1;全绿 → 0(孤儿词只报告,不计入失败)
"""
import argparse, csv, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORDLIST = os.path.join(BASE, 'vocabulary', 'master_wordlist.csv')
KNOWN = os.path.join(BASE, 'vocabulary', 'known_words.txt')
BOM = b'\xef\xbb\xbf'


def load_known():
    """与 cards.py 同口径:known 词已被 cards 从 TSV 过滤,
    期望卡数按同口径对账,否则 known 流程一启用就误报行数不一致。"""
    if not os.path.exists(KNOWN):
        return set()
    return {w.strip().lower() for w in open(KNOWN, encoding='utf-8')
            if w.strip() and not w.startswith('#')}


def _card_expected(rows, known):
    """raw 行 → 期望卡数:known 词(已声明掌握)不出卡,对账口径必须排除"""
    return sum(1 for r in rows if r.get('cn_mean')
               and r['word'].strip().lower() not in known)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--verbose', action='store_true', help='列出每个缺口词/孤儿词明细')
    ap.add_argument('--prune-orphans', action='store_true',
                    help='删除孤儿词(--yes 确认才真正删除;legacy 历史词不删)')
    ap.add_argument('--yes', action='store_true',
                    help='配合 --prune-orphans:跳过交互确认直接删除')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    raw_dir = os.path.join(out_dir, 'raw')
    anki_dir = os.path.join(out_dir, 'anki')
    problems = []
    raw_words_ch = {}   # ch -> [words]
    polished_ch = {}    # ch -> 单词 raw 中已润色词数

    if not os.path.isdir(raw_dir):
        print(f'[FAIL] 无 raw 目录:{raw_dir} —— 先跑 pipeline.py', flush=True)
        sys.exit(1)

    known = load_known()   # P2 对账:期望卡数须与 cards 的 known 过滤同口径

    # --- 1. raw CSV 检查(单词卡 + 表达卡;BOM 断言 2:raw 必带 BOM) ---
    phrase_chs = set()   # 有表达 raw 的章(并入 TSV 检查驱动)
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith('_raw.csv'):
            continue
        is_phrase = fn.endswith('_phrase_raw.csv')
        ch = int(fn.split('_')[1])
        with open(os.path.join(raw_dir, fn), 'rb') as f:
            if not f.read(3).startswith(BOM):
                problems.append(f'ch{ch}: {fn} raw CSV 缺 BOM(不变式 1)')
        with open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        if is_phrase:
            phrase_chs.add(ch)
            n_card = _card_expected(rows, known)
            print(f'[raw]   ch{ch}: {len(rows)} 表达候选 / {n_card} 出卡', flush=True)
            continue
        raw_words_ch[ch] = [r['word'] for r in rows]
        n_polished = _card_expected(rows, known)
        polished_ch[ch] = n_polished
        no_mean = [r['word'] for r in rows if not r.get('cn_mean')]
        no_sent = [r['word'] for r in rows if not r.get('sent')]
        flag = ''
        if len(rows) < 15:
            problems.append(f'ch{ch}: 选词仅 {len(rows)} 个(<15)')
        if no_mean:
            flag += f' 缺润色 {len(no_mean)}'
        if no_sent:
            flag += f' 缺例句 {len(no_sent)}'
        print(f'[raw]   ch{ch}: {len(rows)} 词{flag}', flush=True)
        if flag:
            problems.append(f'ch{ch}{flag}')
        if args.verbose:
            for w in no_mean:
                print(f'        - 缺润色: {w}', flush=True)
            for w in no_sent:
                print(f'        - 缺例句: {w}', flush=True)

    # --- 2. anki TSV 检查:按章号并集(单词 raw ∪ 表达 raw)驱动,不会漏纯表达章 ---
    for ch in sorted(set(raw_words_ch) | phrase_chs):
        tsv = os.path.join(anki_dir, f'chapter_{ch:02d}_anki.tsv')
        n_phrase = 0
        pfn = os.path.join(raw_dir, f'chapter_{ch:02d}_phrase_raw.csv')
        if os.path.exists(pfn):
            with open(pfn, encoding='utf-8-sig', newline='') as f:
                n_phrase = _card_expected(list(csv.DictReader(f)), known)
        polished = polished_ch.get(ch, 0)
        if not os.path.exists(tsv):
            if polished + n_phrase:
                problems.append(f'ch{ch}: 有 {polished + n_phrase} 条润色数据但未出卡'
                                f'({os.path.basename(tsv)} 缺失)')
                print(f'[anki]  ch{ch}: 缺卡片文件!({polished} 词 + {n_phrase} 表达)', flush=True)
            continue
        # BOM 断言 1:TSV 必须无 BOM(Anki 对 BOM 敏感)
        with open(tsv, 'rb') as f:
            if f.read(3).startswith(BOM):
                problems.append(f'ch{ch}: TSV 带 BOM(不变式 1)')
        with open(tsv, encoding='utf-8') as f:
            lines = [ln for ln in f if ln.strip()]
        if not lines:
            problems.append(f'ch{ch}: TSV 为空文件')
            continue
        n_cols = len(lines[0].split('\t'))
        if n_cols != 10:
            problems.append(f'ch{ch}: TSV 表头 {n_cols} 列(应为 10)')
        # 数据行逐行列数校验(padding 取列,畸形行报缺口而非 IndexError)
        bad_rows = [i for i, ln in enumerate(lines[1:], 1)
                    if len(ln.split('\t')) != 10]
        if bad_rows:
            problems.append(f'ch{ch}: TSV {len(bad_rows)} 行列数≠10'
                            f'(如第 {bad_rows[0]} 行)')
        if len(lines) - 1 != polished + n_phrase:
            problems.append(f'ch{ch}: TSV {len(lines)-1} 行 vs 润色词 {polished}'
                            f' + 表达 {n_phrase} 行不一致')
        # AI 解析/词义概述为可选内容:缺解析不阻塞,信息性提示
        body = lines[1:] if len(lines) > 1 else []
        row_cells = [ln.split('\t') + [''] * 12 for ln in body]  # padding 到 ≥10
        n_explain = sum(1 for c in row_cells if c[8])
        n_memo = sum(1 for c in row_cells if c[9])
        print(f'[anki]  ch{ch}: {len(lines)-1} 张卡片(表头 {n_cols} 列'
              f' | AI解析 {n_explain} 词义概述 {n_memo}'
              + (f' | 表达卡 {n_phrase}' if n_phrase else '') + ')', flush=True)

    # --- 3. 总库检查(BOM 断言 3:总库无 BOM) ---
    if os.path.exists(WORDLIST):
        with open(WORDLIST, 'rb') as f:
            if f.read(3).startswith(BOM):
                problems.append('总库 master_wordlist.csv 带 BOM(不变式 1)')
        with open(WORDLIST, encoding='utf-8', newline='') as f:
            wl = list(csv.DictReader(f))
        bad_ch = [r['word'] for r in wl
                  if not r.get('chapters') and r.get('sources') != 'legacy']
        legacy = [r['word'] for r in wl if r.get('sources') == 'legacy']
        bad_exp = [r['word'] for r in wl if r.get('card_exported') != '1']
        if bad_ch:
            problems.append(f'总库 {len(bad_ch)} 词缺 chapters: {", ".join(bad_ch[:5])}')
        if bad_exp:
            problems.append(f'总库 {len(bad_exp)} 词未标记已出卡')
        print(f'[wl]    总库 {len(wl)} 词 | 缺chapter {len(bad_ch)} | 历史遗留 {len(legacy)} | 未出卡 {len(bad_exp)}', flush=True)
        if args.verbose:
            for w in bad_ch:
                print(f'        - 缺chapters: {w}', flush=True)

        # --- 4. 孤儿词报告:总库词 ↔ 当书 raw(出卡源)反向一致性 ---
        raw_words = set()
        for ws in raw_words_ch.values():
            raw_words.update(w.strip().lower() for w in ws)
        for fn in sorted(os.listdir(raw_dir)):
            if fn.endswith('_phrase_raw.csv'):
                with open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline='') as f:
                    raw_words.update(r['word'].strip().lower()
                                     for r in csv.DictReader(f) if r.get('cn_mean'))
        orphans = [r for r in wl
                   if (r.get('book') == args.book or not r.get('book'))
                   and r['word'].strip().lower() not in raw_words]
        legacy_orphans = [r for r in orphans if r.get('sources') == 'legacy']
        if orphans:
            print(f'[wl]    孤儿词 {len(orphans)} 个(总库有、当前该书 raw 无;'
                  f'其中 legacy 历史词 {len(legacy_orphans)} 个)'
                  f'—— 信息性报告,不阻塞校验', flush=True)
            if args.verbose:
                for r in orphans:
                    print(f'        - {r["word"]}(sources={r.get("sources")})', flush=True)
            # --prune-orphans:删除前打印清单,"不静默覆盖",需 --yes/交互确认;
            # legacy 历史词视为资产,绝不自动删
            if args.prune_orphans:
                non_legacy = [r for r in orphans if r.get('sources') != 'legacy']
                print(f'[prune] 待删除 {len(non_legacy)} 个孤儿词(legacy {len(legacy_orphans)} 个保留):',
                      flush=True)
                for r in non_legacy:
                    print(f'        - {r["word"]}(sources={r.get("sources")})', flush=True)
                if not non_legacy:
                    print('[prune] 无待删除词,跳过', flush=True)
                elif args.yes or input('确认删除上述词?(y/N) ').strip().lower() in ('y', 'yes'):
                    drop = {r['word'] for r in non_legacy}
                    kept = [r for r in wl if r['word'] not in drop]
                    # 无条件写规范表头:全删场景也不留下 0 字节无表头文件
                    fieldnames = list(kept[0].keys()) if kept else list(wl[0].keys())
                    with open(WORDLIST, 'w', encoding='utf-8', newline='') as f:
                        w = csv.DictWriter(f, fieldnames=fieldnames,
                                           lineterminator='\n')
                        w.writeheader()
                        if kept:
                            w.writerows(kept)
                    print(f'[prune] 已删除 {len(non_legacy)} 个孤儿词,总库 {len(wl)} → {len(kept)} 行',
                          flush=True)
                else:
                    print('[prune] 已取消(--yes 跳过确认)', flush=True)

    # --- 5. 汇总 ---
    print('', flush=True)
    if problems:
        print(f'发现 {len(problems)} 个问题:', flush=True)
        for p in problems:
            print(f'  - {p}', flush=True)
        sys.exit(1)
    print('校验通过:raw/anki/总库一致,无缺口', flush=True)
    sys.exit(0)


if __name__ == '__main__':
    main()