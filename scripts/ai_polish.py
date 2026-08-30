"""AI 润色阶段(一键管线的模型步骤):为 raw 中缺中文释义或缺原文例句的词批量补齐。

职责(例句齐全不变式的源头保证):
- 缺 cn_mean 的词 → 模型生成词典式中文释义(参考 raw 自带 ECDICT trans 凝练);
- 有原句 → cn_sent = 原句的完整中文翻译;
- 缺原句 → 模型额外生成 ai_en(贴合原著时代风格的例句)+ cn_sent 翻译,
  apply_polish --ai-en 合并后,出卡词例句 100% 齐全。

产物 work/polish_ai_<book>_ch<NN>.json:[{word, cn_mean, cn_sent, [ai_en]}]
  —— 与手工润色 JSON 同构,apply_polish.py --polish/--ai-en 直接消费;
断点:产物已有词自动跳过;失败词记 work/polish_ai_<book>_failed.json(--verbose 强试)。
Provider/重试/并发约定与 ai_explain.py 完全一致(直接复用其实现)。

用法:
  uv run python scripts/ai_polish.py --book <书名>                     # 全书,只补缺的
  uv run python scripts/ai_polish.py --book <书名> --chapter 2         # 单章
  uv run python scripts/ai_polish.py --book <书名> --dry-run           # 预览,不调模型
"""
import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from ai_explain import Provider, _lenient_load, log, TIMEOUT, MAX_RETRY

DEFAULT_BATCH_SIZE = 10     # 词/批:本阶段每词输出很短,批次可以比 ai_explain 大
DEFAULT_WORKERS = 8

BATCH_PROMPT = """你是英语学习卡的"润色"助手:为生词补全中文释义与例句翻译。请严格只输出一个 JSON 对象,不要任何多余文字、不要包代码块。

输出结构:
{"results": [
  {"word": "目标词原文",
   "cn_mean": "词典式中文释义",
   "cn_sent": "例句的完整中文翻译",
   "ai_en": "仅当该词标注【例句缺失】时:生成一个能自然用上该词的英文例句;否则省略此字段"}
]}

【硬性规则】
- results 必须包含下方列出的【每一个】词,顺序与输入一致,一个都不能少。
- 有【原句】的词:cn_sent 就是这个原句的忠实中文翻译,通顺自然,不得改写原句;省略 ai_en。
- 标注【例句缺失】的词:生成 ai_en——一句话,8 到 20 个单词,自然使用该词(可用其屈折形式,
  如过去式/复数),风格贴近给出的小说时代背景;cn_sent 翻译你生成的这句。
- cn_mean:参考给出的词典释义凝练成学习卡释义,同词性多义项保留最常用的两三个,用中文分号分隔。
- 所有文本字段一律单行,不得换行;任何引号用中文引号 "" 或「」,严禁英文双引号 "
  (它会被当成 JSON 定界符,导致整段报废)。

【待处理的词】(每个词一块,块间空行,按顺序回答):
__ITEMS__

请输出 JSON:"""


def build_info(r):
    """单词语料块:有原句给原句求翻译;缺失则要求模型生成例句。"""
    if r.get('sent'):
        ex = f"【原句】{r['sent']}"
    else:
        ex = "【原句】(缺失——请生成 ai_en 例句,并在 cn_sent 给出其翻译)"
    return (
        f"【目标词】{r['word']}({(r.get('pos') or '—')}, CEFR {(r.get('cefr') or '').upper() or '—'})\n"
        f"【词典释义】{(r.get('trans') or '—')[:120]}\n"
        f"【小说时代】{r.get('era') or '19 世纪英语家庭小说'}\n"
        f"{ex}"
    )


def _clean(s):
    return str(s or '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ') \
        .replace('\\n', ' ').strip()


def parse_batch(text, need_ai_en):
    """解析并结构校验:word/cn_mean/cn_sent 必填;原句缺失的词必须给出 ai_en。
    返回 [{word, cn_mean, cn_sent, ai_en}](不合格条目剔除,调用方重试)。"""
    if not text:
        return []
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        obj = _lenient_load(m.group(0))
    if not isinstance(obj, dict):
        return []
    out = []
    for p in (obj.get('results') or []):
        if not isinstance(p, dict):
            continue
        w = _clean(p.get('word'))
        mean = _clean(p.get('cn_mean'))
        sent_cn = _clean(p.get('cn_sent'))
        ai_en = _clean(p.get('ai_en'))
        if need_ai_en.get(w) and not ai_en:
            continue                       # 缺原句的词没生成例句 = 不合格
        if not need_ai_en.get(w):
            ai_en = ''
        if w and mean and sent_cn and '�' not in mean + sent_cn + ai_en:
            item = {'word': w, 'cn_mean': mean, 'cn_sent': sent_cn}
            if ai_en:
                item['ai_en'] = ai_en
            out.append(item)
    return out


def ask_batch(prov, rows, need_ai_en, verbose):
    """批量问询 + 缺词单词回退。返回 ({word: 条目}, [失败 word])。"""
    items = '\n\n'.join(build_info(r) for r in rows)
    prompt = BATCH_PROMPT.replace('__ITEMS__', items)

    parsed = []
    for attempt in range(MAX_RETRY):
        if verbose:
            log(f'  {len(rows)} 词/批 尝试 {attempt + 1}/{MAX_RETRY} ...')
        raw = prov.ask(prompt)
        parsed = parse_batch(raw, need_ai_en)
        if parsed:
            break
        if verbose:
            log(f'  批次无有效结果: {(getattr(prov, "last_error", "") or (raw or "")[:120])[:160]}')

    got, fails = {}, []
    for p in parsed:
        got.setdefault(p['word'], p)
    for r in rows:
        if r['word'] in got:
            continue
        one = None
        for attempt in range(MAX_RETRY):
            if verbose:
                log(f'  {r["word"]}(回退) 尝试 {attempt + 1}/{MAX_RETRY} ...')
            single = BATCH_PROMPT.replace('__ITEMS__', build_info(r))
            raw_one = prov.ask(single)
            for p in parse_batch(raw_one, need_ai_en):
                if p['word'] == r['word']:
                    one = p
                    break
            if one:
                break
            if verbose:
                log(f'  单词无有效结果: {(getattr(prov, "last_error", "") or (raw_one or "")[:120])[:160]}')
        if one:
            got[r['word']] = one
        else:
            fails.append(r['word'])
    return got, fails


def process_chapter(ch, rows, prov, args, work_dir):
    """处理单章(只补缺失字段,产物按章独立可断点)。返回 (生成数, 跳过数)。"""
    out_json = os.path.join(work_dir, f'polish_ai_{args.book}_ch{ch:02d}.json')
    failed_json = os.path.join(work_dir, f'polish_ai_{args.book}_failed.json')

    done = {}
    if os.path.exists(out_json):
        for p in json.load(open(out_json, encoding='utf-8')):
            done[p['word']] = p

    # 断点:已补齐的词不再重做(释义有了;句子原本就有或已生成 ai_en)
    todo = [r for r in rows
            if not (done.get(r['word'], {}).get('cn_mean')
                    and (r.get('sent') or done[r['word']].get('ai_en')))]
    if args.limit:
        todo = todo[:args.limit]
    n_skip = len(rows) - len(todo)
    if args.dry_run:
        log(f'[ch{ch:02d}] dry-run: 待补 {len(todo)} 词(缺释义 '
            f"{sum(1 for r in todo if not r.get('cn_mean'))} / 缺例句 "
            f"{sum(1 for r in todo if not r.get('sent'))})")
        return 0, n_skip
    if not todo:
        log(f'[ch{ch:02d}] 无缺失,跳过')
        return 0, n_skip

    need_ai_en = {r['word']: not bool(r.get('sent')) for r in rows}
    results, fails = [], []
    for i in range(0, len(todo), args.batch_size):
        batch = todo[i:i + args.batch_size]
        got, bad = ask_batch(prov, batch, need_ai_en, args.verbose)
        results += list(got.values())
        fails += bad
        if fails:
            from ai_explain import _save_failed
            _save_failed(failed_json, fails)

    if results:
        merged = dict(done)
        for p in results:
            merged.setdefault(p['word'], p)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(list(merged.values()), f, ensure_ascii=False, indent=1)
        log(f'[ch{ch:02d}] 写入 {len(merged)} 词(新生成 {len(results)},'
            f'其中生成例句 {sum(1 for p in results if p.get("ai_en"))})')
    return len(results), n_skip


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--book', required=True)
    ap.add_argument('--provider', default='claude-cli',
                    choices=['claude-cli', 'anthropic', 'openai'])
    ap.add_argument('--chapter', type=int, default=0, help='只处理指定章(0=全部)')
    ap.add_argument('--limit', type=int, default=0, help='每章最多处理 N 词(0=不限)')
    ap.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'并发章数(默认 {DEFAULT_WORKERS})')
    ap.add_argument('--dry-run', action='store_true', help='只预览缺失,不调模型')
    ap.add_argument('--model', default='')
    ap.add_argument('--base-url', default='')
    ap.add_argument('--api-key', default='')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    raw_dir = os.path.join(out_dir, 'raw')
    work_dir = os.path.join(out_dir, 'work')
    os.makedirs(work_dir, exist_ok=True)
    if not os.path.isdir(raw_dir):
        sys.exit(f'[FAIL] 无 raw 目录:{raw_dir} —— 先跑 pipeline')

    files = sorted(f for f in os.listdir(raw_dir)
                   if f.endswith('_raw.csv') and not f.endswith('_phrase_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.startswith(f'chapter_{args.chapter:02d}_')]
    chapters = []
    for fn in files:
        ch = int(fn.split('_')[1])
        # 只收真正缺东西的行:缺释义或缺例句;两样齐全的词不再进模型阶段
        rows = [r for r in csv.DictReader(
            open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline=''))
            if not (r.get('cn_mean') and r.get('sent'))]
        chapters.append((ch, rows))

    prov = None if args.dry_run else Provider.create(args.provider, args)
    n_workers = max(1, min(args.workers, len(chapters))) if chapters else 1
    total = 0
    if n_workers > 1:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(process_chapter, ch, rows, prov, args, work_dir)
                    for ch, rows in chapters]
            for fut in as_completed(futs):
                total += fut.result()[0]
    else:
        for ch, rows in chapters:
            total += process_chapter(ch, rows, prov, args, work_dir)[0]

    if args.dry_run:
        print(f'DRY-RUN [provider={args.provider}] 共 {len(chapters)} 章', flush=True)
    else:
        print(f'DONE [provider={args.provider}] 新补 {total} 词 → '
              f'下一步: apply_polish --polish/--ai-en 消费 work/polish_ai_*.json', flush=True)


if __name__ == '__main__':
    main()
