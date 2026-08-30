"""表达精选(AI 挑选 + 释义,可选旁路,不入默认管线):
从 work/phrase_cands_<book>.json(每章表达候选)中由 AI 精选每章 3-4 条值得背诵的表达,
并生成中文释义 / 例句译文 / 讲解 / 记忆钩子,产物可手改后交给 apply_polish.py --phrases 合并出卡。

接入点(--provider)与 ai_explain.py 完全一致(claude-cli 默认 / anthropic / openai),
批量走章级 prompt(一章候选 40 条一次给全),多章并发 --workers。

产物:
  data/output/<book>/work/phrases_picked_<book>_ch<NN>.json
      [{phrase, cn_mean, cn_sent, ai_analysis, memo}, …] ← 可手改,改完重跑 apply 即生效
  data/output/<book>/work/phrases_picked_<book>_failed.json   最终失败章(不阻塞其余)
合并:uv run python scripts/apply_polish.py --book <b> --phrases 'work/phrases_picked_*.json'
断点:产物文件已存在的章自动跳过(重跑不重复花钱)。

用法:
  uv run python scripts/ai_pick_phrases.py --book little_women --dry-run
  uv run python scripts/ai_pick_phrases.py --book little_women --chapter 9
  uv run python scripts/ai_pick_phrases.py --book little_women --workers 4
  uv run python scripts/ai_pick_phrases.py --book little_women --pick-min 2 --pick-max 4
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from ai_explain import Provider, _lenient_load, md_bold_to_html, log, _load_failed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILED_LOCK = Lock()   # failed.json 全书共享:多章并发必须同步读改写


def _save_failed_chs(path, ch):
    """failed.json 读改写一体:加锁防多章线程互踩;按章去重合并(失败章重复跑
    不无限累加);临时文件 + os.replace 原子替换,读者不见半截 JSON。"""
    with FAILED_LOCK:
        merged = {}
        for p in _load_failed(path):
            merged[p.get('ch')] = p
        merged[ch] = {'ch': ch}
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(list(merged.values()), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

MAX_RETRY = 3               # 每章最多尝试次数(重试仍然失败 → failed.json)
DEFAULT_PICK_MIN = 1        # 每章精选下限(0 表示可以一章都不挑)
DEFAULT_PICK_MAX = 4        # 每章精选上限:与每章单词卡(18)均衡,表达卡不宜喧宾夺主
DEFAULT_WORKERS = 4         # 并发章数
TIMEOUT = 300               # 与 ai_explain 同步(长输出需要宽裕超时)

# 章级 PROMPT。占位符 __NUM__ / __CANDIDATES__ 运行时替换。
PICK_PROMPT = """你是英语学习卡的"表达精选"助手,从一本书每章的表达候选中精选值得做成卡片的表达,并撰写释义与讲解。请严格只输出一个 JSON 对象,不要任何多余文字、不要包代码块。

输出结构:
{
  "results": [
    {"phrase": "表达原文", "cn_mean": "中文释义", "cn_sent": "例证句完整中文译文", "ai_analysis": "讲解", "memo": "记忆钩子"},
    {"phrase": "…", "cn_mean": "…", "cn_sent": "…", "ai_analysis": "…", "memo": "…"}
  ]
}

【挑选要求】从候选清单里挑出值得收藏的表达。**数量纪律:硬上限 {NUM} 条,宁缺毋滥**——
拿不准的不要;好表达太多也要忍痛舍掉最弱的(一章的单词卡 18 张,表达卡必须明显更少,
重点才突出)。通常 2-4 条就好;一章实在没有值得的,只挑 1 条甚至空 results 都可以。判断标准:
1. 有固定搭配/习语性质的(如 take off、make one's plans、in an altered tone);
2. 表达地道、英语学习者不容易自己造出来的;
3. 例证句意思完整、可独立理解。
排除:句子残片(Had he been…)、过于直白的简单组合(white hands)、人名地名、纯描述性临时组合。

【字段要求】(全部中文):
- cn_mean:这个表达的中文释义(2-8 字为主,可带简短括注说明用法)。
- cn_sent:按例证句在书中的语境给出完整、通顺的中文译文,不得跳句。
- ai_analysis:2-4 句讲解,讲清两件事:① 为什么这个表达值得背(词组成分、字面与引申义的关系、常见搭配);
  ② 什么场景用得上。不要逐词拆语法,不要超过 6 行。
- memo:一句"画面感"记忆钩子——一个能在脑海中看到/感觉到的小画面或联想,一句话,绝不写成词典释义。
【引号铁律】:任何引号一律用中文引号 "" 或「」,严禁英文双引号 " (它会被当成 JSON 定界符,导致整段输出报废)。

【候选清单】(共 N 条,按推荐度从高到低;格式:序号. 表达 [全书出现X次] —— 例证句):
__CANDIDATES__

请输出 JSON:"""


def build_cand_block(c):
    return (f"{c.get('seq', '?')}. {c['phrase']} [全书出现{c.get('freq_book', '?')}次]"
            f" —— {c.get('example', '')[:160]}")


def extract_pick(text, allowed):
    """章级解析:剥多余文字取 {..},校验 results;每条 phrase 必须命中该章候选(防模型编造),
    字段缺一或含 U+FFFD 视为坏项剔除。返回 [{phrase, cn_mean, cn_sent, ai_analysis, memo}, …]"""
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
        ph = str(p.get('phrase') or '').strip().lower()
        if ph not in allowed:
            continue
        rec = {k: md_bold_to_html(str(p.get(k) or '').strip())
               for k in ('cn_mean', 'cn_sent', 'ai_analysis', 'memo')}
        if all(rec.values()) and all('�' not in v for v in rec.values()):
            rec['phrase'] = ph
            out.append(rec)
    return out


def ask_chapter(prov, cands, pick_min, pick_max, verbose):
    """单章问询:候选列表 → AI 精选 + 释义;重试 MAX_RETRY 次。
    下限 pick_min 默认 1(可设 0 = 允许一章都不挑);上限防刷屏。
    返回 (结果列表, 是否全部成功)。"""
    lo = min(pick_min, len(cands))
    hi = min(pick_max, len(cands))
    numbered = [dict(c, seq=i + 1) for i, c in enumerate(cands)]
    allowed = {c['phrase'] for c in cands}
    prompt = PICK_PROMPT.replace('{NUM}', str(hi)).replace(
        '__CANDIDATES__', '\n'.join(build_cand_block(c) for c in numbered))
    for attempt in range(MAX_RETRY):
        if verbose:
            log(f'  [pick] 尝试 {attempt + 1}/{MAX_RETRY} ...')
        picked = extract_pick(prov.ask(prompt), allowed)
        if lo <= len(picked) <= hi:
            return picked, True
        if verbose and picked:
            log(f'  [pick] 挑了 {len(picked)} 条(目标 {lo}-{hi}),重试')
    return picked if lo <= len(picked) <= hi else [], False


def process_chapter(ch, cands, prov, args, work_dir):
    """处理单章:产物已存在则跳过(断点)。返回 (生成数, 章号/失败标记, 跳过标记)。"""
    out_json = os.path.join(work_dir, f'phrases_picked_{args.book}_ch{ch:02d}.json')
    failed_json = os.path.join(work_dir, f'phrases_picked_{args.book}_failed.json')

    if os.path.exists(out_json) and not args.verbose:
        log(f'[ch{ch:02d}] 产物已存在,跳过(断点;--verbose 强制重做)')
        return 0, False, True

    failed_chs = {p['ch'] for p in _load_failed(failed_json)}
    if ch in failed_chs and not args.verbose:
        log(f'[ch{ch:02d}] 已知失败章,跳过(--verbose 强试)')
        return 0, True, False

    if args.dry_run:
        log(f'[ch{ch:02d}] dry-run: {len(cands)} 条候选 → AI 精选 {args.pick_min}-{args.pick_max} 条')
        return 0, False, False

    picked, ok = ask_chapter(prov, cands, args.pick_min, args.pick_max, args.verbose)
    if ok:
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(picked, f, ensure_ascii=False, indent=1)
        log(f'[ch{ch:02d}] 写入 {len(picked)} 条精选表达')
        return len(picked), False, False
    else:
        _save_failed_chs(failed_json, ch)
        log(f'[ch{ch:02d}] 精选失败(多次尝试未达标),记入 failed.json(--verbose 可强试)')
        return 0, True, False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--book', required=True)
    ap.add_argument('--provider', default='claude-cli',
                    choices=['claude-cli', 'anthropic', 'openai'])
    ap.add_argument('--chapter', type=int, default=0, help='只处理指定章(0=全部)')
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'并发处理章数(默认 {DEFAULT_WORKERS};--workers 1 关闭并发)')
    ap.add_argument('--pick-min', type=int, default=DEFAULT_PICK_MIN,
                    help=f'每章精选下限(默认 {DEFAULT_PICK_MIN};候选不足时自然少挑)')
    ap.add_argument('--pick-max', type=int, default=DEFAULT_PICK_MAX,
                    help=f'每章精选上限(默认 {DEFAULT_PICK_MAX})')
    ap.add_argument('--dry-run', action='store_true', help='只预览,不调用任何模型')
    ap.add_argument('--model', default='')
    ap.add_argument('--base-url', default='')
    ap.add_argument('--api-key', default='')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()
    if args.workers < 1:
        sys.exit('--workers 必须 ≥ 1')
    if args.pick_min < 0 or args.pick_max < args.pick_min:
        sys.exit('要求 0 ≤ --pick-min ≤ --pick-max')

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    work_dir = os.path.join(out_dir, 'work')
    os.makedirs(work_dir, exist_ok=True)
    cands_path = os.path.join(work_dir, f'phrase_cands_{args.book}.json')
    if not os.path.exists(cands_path):
        sys.exit(f'缺候选文件: {cands_path}\n先跑: uv run python scripts/pipeline.py'
                 f' --book {args.book} --phrases')

    cands_all = json.load(open(cands_path, encoding='utf-8'))['chapters']
    chapters = [(int(ch), cands) for ch, cands in cands_all.items()]
    if args.chapter:
        chapters = [c for c in chapters if c[0] == args.chapter]
    chapters = [(ch, cs) for ch, cs in chapters if cs]
    if not chapters:
        # 候选文件在但该章候选为空(书里没有 ≥2 次的稳定表达)= 正常无事可做:
        # 跳过且不算失败("宁缺毋滥"的正确结果不应打断管线阶段)
        print('无候选章可处理(该章候选为空或均已完成)—— 跳过,不算失败', flush=True)
        sys.exit(0)

    prov = Provider.create(args.provider, args)
    n_workers = min(args.workers, len(chapters)) if len(chapters) else 1
    total_ok = total_fail = total_skip = 0

    def run_one(item):
        ch, cands = item
        return process_chapter(ch, cands, prov, args, work_dir)

    if n_workers > 1 and not args.dry_run:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(run_one, c): c[0] for c in chapters}
            for fut in as_completed(futs):
                ok, fail, skip = fut.result()
                total_ok += ok
                total_fail += 1 if fail else 0
                total_skip += 1 if skip else 0
    else:
        for c in chapters:
            ok, fail, skip = run_one(c)
            total_ok += ok
            total_fail += 1 if fail else 0
            total_skip += 1 if skip else 0

    if args.dry_run:
        print(f'DRY-RUN [provider={args.provider}] 待处理 {len(chapters)} 章'
              f'(workers={n_workers}, 每章 {args.pick_min}-{args.pick_max} 条)', flush=True)
    else:
        print(f'DONE [provider={args.provider}] 精选 {total_ok} 条 失败 {total_fail} 章'
              f' 跳过 {total_skip} 章', flush=True)
        print('下一步: uv run python scripts/apply_polish.py --book '
              f'{args.book} --phrases data/output/{args.book}/work/phrases_picked_{args.book}_ch01.json',
              flush=True)


if __name__ == '__main__':
    main()