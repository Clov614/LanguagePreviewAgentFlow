"""AI 专名裁决(可选旁路,借鉴 tools/wenyi 的术语抽取 Agent):
对 scan_proper 扫出的疑似专名逐词判定「是否书内专名」,高质量填充
data/books/proper_names/<book>.txt。

与 ai_explain 同一套约定:provider 缺省 claude-cli(本机 headless,零配置零 key);
缺 key/不想用时管线照跑 —— 零模型 scan_proper 的统计建议 + 人工确认一样能闭环,
本脚本只是把「逐条人工裁决」升级为「AI 预裁决 + 人工复核」。
断点:裁决结果缓存 work/proper_ai_<book>.json,已裁决词自动跳过;失败词记
proper_ai_<book>_failed.json(≤3 次重试);重跑会把缓存里的生效裁决重新落盘
(误删建议行后重跑即可恢复)。

用法:uv run python scripts/ai_pick_proper.py --book daddy_long_legs [--apply] [--dry-run]
  默认:把判定为专名的词以**注释建议行**写入 proper_names/<book>.txt(确认后去注释)
  --apply:判定为专名的直接写**生效行**(信 AI 裁决时用;aliases 变体仍为注释建议)
"""
import argparse, json, os, re, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))

from ai_explain import Provider, MAX_RETRY, log, _load_failed, _save_failed
import proper_names
from pipeline import load_oxford

DEFAULT_BATCH_SIZE = 20     # 疑似词/批:候选总量小(几十),批内给足上下文
DEFAULT_WORKERS = 2         # 批间并发:总调用次数少,无需大开

PRINT_LOCK = Lock()

SYSTEM_PROMPT = """\
你是英语学习生词卡项目的「书内专名判定器」。候选词来自对一本英语小说的统计扫描:
不在常用词表(Oxford 5000)、且几乎总以大写形式出现的词。请逐个判定它是否为书内专名。
判定为专名的(人物/地名/组织/称谓头衔/报刊作品/事件等专有指称)会被加入排除表,
不再做成生词卡;判定为普通词的保留为生词候选(如 schoolmaster、embarrass)。
判据:综合大写占比统计与上下文例句;地名、街名、宅名、农场名、报刊名都算专名;
拿不准时倾向判专名 —— 误排除一个普通词的代价远低于把人名做成生词卡。
仅输出 JSON(每个候选词必须各有一条,word 原样返回):
{"items":[{"word":"候选词","proper":true或false,"type":"人物/地名/组织/称谓/报刊作品/其他专名/普通词","reason":"不超过15字的依据","aliases":["同一实体的其它原文拼写变体,没有则空数组"]}]}\
"""


def build_user_prompt(batch):
    lines = []
    for s in batch:
        ctx = '  '.join(s['contexts'][:2]) if s['contexts'] else '—'
        lines.append(f'- {s["word"]}(出现{s["freq"]}次,大写占比{s["cap"] * 100 // s["freq"]}%):{ctx}')
    return '【候选词与上下文】\n' + '\n'.join(lines) + '\n\n请逐词判定,输出 JSON:{"items":[...]}'


def parse_items(text, wanted):
    """宽松提取模型 JSON;返回 {word: item}(只收本批候选词,越权输出丢弃)"""
    if not text:
        return {}
    m = re.search(r'\{.*\}|\[.*\]', text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    items = data.get('items') if isinstance(data, dict) else data
    out = {}
    for d in items if isinstance(items, list) else []:
        if isinstance(d, dict) and d.get('word') in wanted:
            out[d['word']] = d
    return out


def ask_batch(prov, batch, verbose):
    """一批疑似词一次调用,MAX_RETRY 内要求覆盖全批;返回 ({word: item}, [失败词])"""
    prompt = SYSTEM_PROMPT + '\n\n' + build_user_prompt(batch)
    wanted = {s['word'] for s in batch}
    got = {}
    for attempt in range(MAX_RETRY):
        if verbose:
            log(f'  {len(batch)} 词/批 尝试 {attempt + 1}/{MAX_RETRY} ...')
        got = parse_items(prov.ask(prompt), wanted)
        if wanted <= got.keys():
            return got, []
    return got, sorted(wanted - got.keys())


def main():
    ap = argparse.ArgumentParser(description='AI 裁决疑似专名,写 proper_names/<book>.txt')
    ap.add_argument('--book', required=True)
    ap.add_argument('--provider', default='claude-cli',
                    help='claude-cli(默认)/ anthropic / openai')
    ap.add_argument('--api-key', default='')
    ap.add_argument('--base-url', default='')
    ap.add_argument('--model', default='')
    ap.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    ap.add_argument('--limit', type=int, default=0, help='只裁决前 N 个疑似(试跑)')
    ap.add_argument('--apply', action='store_true', help='专名直接写生效行(默认注释建议)')
    ap.add_argument('--dry-run', action='store_true', help='只预览批次,不调用模型')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    md_path = os.path.join(BASE, 'data', 'books', '_md', f'{args.book}.md')
    if not os.path.exists(md_path):
        sys.exit(f'[STOP] 找不到 {md_path} —— 先跑 scripts/epub_to_md.py')
    ppath = proper_names.path_for(args.book)
    active = proper_names.load(args.book)
    work_dir = os.path.join(BASE, 'data', 'output', args.book, 'work')
    os.makedirs(work_dir, exist_ok=True)
    cache_path = os.path.join(work_dir, f'proper_ai_{args.book}.json')
    failed_path = os.path.join(work_dir, f'proper_ai_{args.book}_failed.json')
    cache = {d['word']: d for d in _load_failed(cache_path)}   # 容错读取,同 failed.json

    with open(md_path, encoding='utf-8') as f:
        md_text = f.read()
    oxford = load_oxford()
    if args.dry_run:
        sus = [s for s in proper_names.suspects(md_text, oxford, exclude=active)]
        print(f'[dry-run] 待裁决 {len(sus)} 个疑似词(默认频次≥2 才调模型)')
        for i in range(0, len(sus), args.batch_size):
            print(f'  批{i // args.batch_size + 1}: '
                  + ', '.join(s['word'] for s in sus[i:i + args.batch_size]))
        return

    sus = [s for s in proper_names.suspects(md_text, oxford, exclude=active | set(cache),
                                            want_contexts=True)
           if s['freq'] >= 2]          # 单次出现的大写词多为噪声,不值得一次模型调用
    if args.limit:
        sus = sus[:args.limit]
    if not sus:
        print('无待裁决疑似词(全部已裁决/已生效/频次不足)')
        sus = []
    else:
        batches = [sus[i:i + args.batch_size] for i in range(0, len(sus), args.batch_size)]
        print(f'待裁决 {len(sus)} 个疑似词,{len(batches)} 批'
              f'({args.batch_size} 词/批,workers={args.workers},provider={args.provider})')
        prov = Provider.create(args.provider, args)
        results, fails = {}, []
        lock = Lock()

        def run(b):
            items, miss = ask_batch(prov, b, args.verbose)
            with lock:
                results.update(items)
                fails.extend(miss)
            return len(items)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            done = [f.result() for f in as_completed([ex.submit(run, b) for b in batches])]
        print(f'裁决完成:{sum(done)}/{len(sus)} 词' +
              (f',失败 {len(fails)}(记入 {os.path.basename(failed_path)})' if fails else ''))
        _save_failed(cache_path, [dict(v, word=w) for w, v in results.items()])
        if fails:
            _save_failed(failed_path, fails)

    # 落盘:本次新裁决 + 缓存中此前判为专名的(重跑可恢复被误删的建议行)
    cache_proper = {w: v for w, v in cache.items() if v.get('proper') and w not in active}
    proper_hits = {w: v for w, v in results.items() if v.get('proper')}
    scores = {s['word']: s['score'] for s in sus}
    alias_sugs = set()
    for w, v in list(proper_hits.items()) + list(cache_proper.items()):
        for a in v.get('aliases') or []:
            a = (a or '').strip().lower()
            if a and re.fullmatch(r'[a-z]{2,}', a) and a not in oxford \
                    and a not in active and a not in proper_hits:
                alias_sugs.add(a)
    lines = []
    for w, v in sorted({**cache_proper, **proper_hits}.items(),
                       key=lambda kv: -scores.get(kv[0], 0)):
        fresh = w in proper_hits
        mark = '' if args.apply else '# '
        src = 'AI裁决' if fresh else 'AI裁决(缓存恢复)'
        lines.append(f'{mark}{w}  # {v.get("type", "")} · {v.get("reason", "")} · {src}')
    lines += [f'# {a}  # 变体建议(AI,关联上方专名;确认后生效)' for a in sorted(alias_sugs)]
    if lines:
        existing = ''
        if os.path.exists(ppath):
            with open(ppath, encoding='utf-8') as f:
                existing = f.read()
        if existing and not existing.endswith('\n'):
            existing += '\n'
        with open(ppath, 'a', encoding='utf-8', newline='\n') as f:
            if not existing:
                f.write(f'# {args.book} 书内专名(pipeline/hard_words/validate 共用)\n'
                        f'# AI 裁决 + 人工复核;去掉行首 # 生效\n')
            f.write('\n'.join(lines) + '\n')
    print(f'专名表: +{len(proper_hits) + len(cache_proper)}'
          + ('(生效)' if args.apply else '(注释建议,确认后去注释)') +
          (f' +{len(alias_sugs)} 变体建议' if alias_sugs else '') + f' -> {ppath}')
    for w in sorted({**cache_proper, **proper_hits}):
        v = proper_hits.get(w) or cache_proper[w]
        print(f'  + {w:<16} {v.get("type", ""):<6} {v.get("reason", "")}')


if __name__ == '__main__':
    main()
