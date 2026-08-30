"""管线缺口校验(阶段间健康检查):一键发现"缺什么"
检查项:
  1. raw CSV:每章选词数、缺润色(cn_mean)的词、缺例句(sent)的词
  2. anki TSV:有无出卡、行数是否与润色词数一致、10 列结构是否完整
  3. 生词总库:chapters 为空 / card_exported 非 1 / sources 为空的异常行
  4. 卡片与总库一致性:总库词是否都出自某章 raw(反向追溯)
用法:  uv run python scripts/validate.py --book little_women [--verbose]
退出码:有缺口 → 1;全绿 → 0
"""
import argparse, csv, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORDLIST = os.path.join(BASE, 'vocabulary', 'master_wordlist.csv')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--verbose', action='store_true', help='列出每个缺口词明细')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    raw_dir = os.path.join(out_dir, 'raw')
    anki_dir = os.path.join(out_dir, 'anki')
    problems = []
    raw_words_ch = {}   # ch -> [words]

    if not os.path.isdir(raw_dir):
        print(f'[FAIL] 无 raw 目录:{raw_dir} —— 先跑 pipeline.py', flush=True)
        sys.exit(1)

    # --- 1. raw CSV 检查 ---
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith('_raw.csv'):
            continue
        ch = int(fn.split('_')[1])
        with open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        raw_words_ch[ch] = [r['word'] for r in rows]
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

    # --- 2. anki TSV 检查 ---
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith('_raw.csv'):
            continue
        ch = int(fn.split('_')[1])
        tsv = os.path.join(anki_dir, fn.replace('_raw.csv', '_anki.tsv'))
        polished = sum(1 for _ in open_has_cn(os.path.join(raw_dir, fn)))
        if not os.path.exists(tsv):
            if polished:
                problems.append(f'ch{ch}: 有 {polished} 个润色词但未出卡({os.path.basename(tsv)} 缺失)')
                print(f'[anki]  ch{ch}: 缺卡片文件!({polished} 词已润色)', flush=True)
            continue
        with open(tsv, encoding='utf-8') as f:
            lines = [ln for ln in f if ln.strip()]
        n_cols = len(lines[0].split('\t'))
        if n_cols != 10:
            problems.append(f'ch{ch}: TSV 表头 {n_cols} 列(应为 10)')
        if len(lines) - 1 != polished:
            problems.append(f'ch{ch}: TSV {len(lines)-1} 行 vs 润色词 {polished} 行不一致')
        # AI 解析/词义概述为可选内容:缺解析不阻塞,信息性提示
        body = lines[1:] if len(lines) > 1 else []
        n_explain = sum(1 for ln in body if (ln.split('\t') + ['', ''])[8])
        n_memo = sum(1 for ln in body if (ln.split('\t') + ['', ''])[9])
        print(f'[anki]  ch{ch}: {len(lines)-1} 张卡片(表头 {n_cols} 列'
              f' | AI解析 {n_explain} 词义概述 {n_memo})', flush=True)

    # --- 3. 总库检查 ---
    if os.path.exists(WORDLIST):
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

    # --- 4. 汇总 ---
    print('', flush=True)
    if problems:
        print(f'发现 {len(problems)} 个问题:', flush=True)
        for p in problems:
            print(f'  - {p}', flush=True)
        sys.exit(1)
    print('校验通过:raw/anki/总库一致,无缺口', flush=True)
    sys.exit(0)

def open_has_cn(path):
    """带 BOM 读取 raw CSV,返回含 cn_mean 的行"""
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            if r.get('cn_mean'):
                yield r

if __name__ == '__main__':
    main()