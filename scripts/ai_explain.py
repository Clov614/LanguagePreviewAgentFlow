"""AI 例句解析生成模块(可选旁路,不入默认管线):
对 raw CSV 中已润色的词批量生成 ai_analysis(三段式整句解析)+ memo(画面感词义钩子)。
生成端为结构化 schema:模型只产出 JSON 字段(items/reading/culture/memo),
排版文本由 compose_analysis 本地确定性拼装,词身份(目标词/超纲词)由本地 lemma 规则
判定 —— 格式在源头即唯一,不依赖模型遵守排版约定;cards.py 的渲染归一器仅作为
手改 JSON / 历史数据的兜底。产物结构不变:[{word, ai_analysis, memo}]。

接入点(--provider):
  claude-cli : 本机 Claude Code `claude -p` headless 模式,零配置零 key(走已有登录)【默认】
  anthropic  : Anthropic 官方 API(环境变量 ANTHROPIC_API_KEY,可 --api-key 覆盖)
  openai     : 任意 OpenAI 兼容端点(GPT / Gemini / DeepSeek / Ollama / LM Studio …),
               环境变量 OPENAI_BASE_URL + OPENAI_API_KEY,CLI 参数可覆盖

环境变量(全部可选;key 只读环境变量,不落盘):
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL                  (anthropic provider)
  OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL      (openai provider)
  OPENAI_MODEL 示例: gpt-4o-mini / gemini-2.5-flash / deepseek-chat / qwen3
  OPENAI_BASE_URL 模板:
    DeepSeek   https://api.deepseek.com/v1
    Gemini     https://generativelanguage.googleapis.com/v1beta/openai/  (key=GEMINI_API_KEY)
    Ollama     http://localhost:11434/v1               (本地无 key,留空即可)
    LM Studio  http://localhost:1234/v1                (本地无 key,留空即可)

批处理(--batch-size,默认 6):一次调用批量生成多个词的解析,冷启动/请求开销均摊;
  批内个别词失败时回退单词 prompt 单独重试(最多 MAX_RETRY 次),仍失败记入 failed.json。

多章并发(--workers,默认 4):各章产物文件独立、天然无冲突,可并行;
  --workers 1 关闭并发;单章(--chapter)时并发自然退化为 1。

产物:
  data/output/<book>/work/ai_explain_<book>_ch<NN>.json
      [{word, ai_analysis, memo}, …] ← 可手改,改完重跑 apply_polish 即生效(结构不变,断点兼容)
  data/output/<book>/work/ai_explain_<book>_failed.json   最终失败词(不阻塞其余)
合并:uv run python scripts/apply_polish.py --book <b> --explain <上面的 json>
断点:产物文件中已出现的词本次自动跳过(中断后重跑不重复花钱)。

用法:
  uv run python scripts/ai_explain.py --book little_women --chapter 1
  uv run python scripts/ai_explain.py --book little_women --dry-run          # 不调用,预览批次
  uv run python scripts/ai_explain.py --book little_women --workers 4 --batch-size 6
  uv run python scripts/ai_explain.py --book <b> --provider openai \
      --model deepseek-chat          # 需环境变量 OPENAI_BASE_URL=https://api.deepseek.com/v1 OPENAI_API_KEY=sk-...
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from hard_words import Difficulty, hard_words_in, lemma_of
import proper_names

MAX_RETRY = 3               # 每个词/每批最多尝试次数
DEFAULT_OPENAI_MODEL = 'gpt-4o-mini'
DEFAULT_ANTHROPIC_MODEL = 'claude-sonnet-5'
DEFAULT_BATCH_SIZE = 6      # 词/批:调大省调用但单次输出变长(注意 max_tokens 截断)
DEFAULT_WORKERS = 4         # 并发章数
TIMEOUT = 300               # 单次请求超时(秒):批量长输出需要更宽裕

# 批量版 PROMPT。占位符 __ITEMS__ 由运行时替换(词信息块),不用 .format 以免大括号冲突。
# 模型只产出结构化 JSON(成分/讲解/整句/文化点),排版由 compose_analysis 本地确定性拼装,
# 格式在源头即唯一;模型不再自由发挥任何版面(历史实测的 ①②③ / #段标 / 缺段首行 /
# 「段名」 / 段名带注释 / 单行连排 / 字面 \n 等十来种排版漂移全部根治)。
# 词身份标注(目标词/超纲词)同样由本地规则判定(hard_words.lemma_of),不依赖模型自觉。
BATCH_PROMPT = """你是英语学习卡的"例句解析"助手,为生词卡片批量生成解析。请严格只输出一个 JSON 对象,不要任何多余文字、不要包代码块。

输出结构:
{
  "results": [
    {
      "word": "目标词原文",
      "items": [
        {"seg": "例句成分(英文原文)", "note": "该成分的中文讲解",
         "words": [{"w": "成分内值得单独讲的英文词/短语", "note": "该词的中文讲解"}]}
      ],
      "reading": "整句解读,一个连贯的中文段落",
      "culture": "文化/时代背景,一个连贯的中文段落;没有可写内容就填空字符串",
      "memo": "一句画面感记忆钩子"
    }
  ]
}

【硬性规则】
- results 必须包含下方列出的【每一个】词,顺序与输入一致,一个都不能少。
- items 把例句从左到右拆成成分,每个成分一个对象:seg 原样摘录英文成分(保留原文大小写与标点),
  note 用中文讲清它的语法身份(主语/谓语/状语/从句等)和含义,以及为什么中文译文这么处理。
- words 只放值得单独讲的词:目标词、超纲词、重要搭配,每个一条;w 原样摘录英文,
  note 讲词性、词形变化(如不规则动词三态)、本义与引申义;普通词不要凑数。
  无须拆词讲解的成分省略 words 字段。
- seg/w 内不得夹中文;所有文本字段一律单行,不得出现换行;
  任何引号用中文引号 "" 或「」,严禁英文双引号 " (它会被当成 JSON 定界符,导致整段解析报废)。
- 全部中文,不吝啬字数,讲透为止。

【memo 要求】:对目标词的一句"画面感"记忆钩子(imagine)——一个能在脑海中看到/感觉到的小画面或联想,一句话,留白不展开,绝不写成词典释义。

【待解析的词】(每个词一块,块间空行,按顺序回答):
__ITEMS__

请输出 JSON:"""

PRINT_LOCK = Lock()
FAILED_LOCK = Lock()   # failed.json 全书共享:多章并发必须同步读改写


def log(*a):
    with PRINT_LOCK:
        print(*a, flush=True)


def _load_failed(path):
    """容错读取共享失败记录:并发写期间读到的是完整旧/新文件(原子替换);
    损坏/半截文件(历史遗留)丢弃重建,不抛异常、不崩进程。"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _save_failed(path, fails):
    """failed.json 读改写一体:加锁防多章线程互踩;按 word 去重合并(失败条目
    重复跑不再无限累加);临时文件 + os.replace 原子替换,读者不见半截 JSON。"""
    with FAILED_LOCK:
        merged = {}
        for p in _load_failed(path):
            merged.setdefault(p.get('word'), p)
        for w in fails:
            merged.setdefault(w, {'word': w})
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(list(merged.values()), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def build_info(r, hard):
    return (
        f"【目标词】{r['word']}\n"
        f"【音标】{r.get('phon') or '—'} 【词性】{r.get('pos') or '—'} "
        f"【CEFR】{(r.get('cefr') or '').upper() or '—'}\n"
        f"【现有释义】{r.get('cn_mean') or '—'}\n"
        f"【例句】{r.get('sent') or '—'}\n"
        f"【例句译文】{r.get('cn_sent') or '—'}\n"
        f"【例句中超纲词】{', '.join(f'{w}(难度高于目标词)' for w in hard) if hard else '无'}"
    )


class Provider:
    """三接入点抽象:ask(prompt) -> 文本 或 None(失败)。线程安全(每次调起独立请求)。"""

    @classmethod
    def create(cls, name, args):
        name = (name or 'claude-cli').lower()
        if name == 'claude-cli':
            return ClaudeCli(args)
        if name == 'anthropic':
            return Anthropic(args)
        if name == 'openai':
            return OpenAI(args)
        sys.exit(f'未知 provider: {name}(可选 claude-cli / anthropic / openai)')


class ClaudeCli(Provider):
    def __init__(self, args):
        self.cli = shutil.which('claude')
        if not self.cli:
            sys.exit('[claude-cli] 本机未找到 claude 命令(需安装 Claude Code)。'
                     '可换 --provider anthropic 或 openai。')

    def ask(self, prompt):
        """prompt 走 stdin 而非命令行参数:Windows 下 claude.CMD(批处理包装)
        传递换行/超长参数会被破坏(曾致 3 次全部失败);stdin 字节直通无损。
        stdout 仍 bytes 模式读取,依次尝试 utf-8 / gbk 解码,兜底 replace(由
        parse_batch 的 U+FFFD 校验拦截,触发重试)。"""
        try:
            p = subprocess.run([self.cli, '-p'], input=prompt.encode('utf-8'),
                               capture_output=True, timeout=TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            return None
        raw = p.stdout or b''
        for enc in ('utf-8', 'gbk'):
            try:
                return raw.decode(enc).strip() or None
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', errors='replace').strip() or None


class Anthropic(Provider):
    URL = 'https://api.anthropic.com/v1/messages'

    def __init__(self, args):
        self.key = args.api_key or os.environ.get('ANTHROPIC_API_KEY', '')
        self.model = args.model or os.environ.get('ANTHROPIC_MODEL') \
            or DEFAULT_ANTHROPIC_MODEL
        if not self.key:
            sys.exit('[anthropic] 缺 ANTHROPIC_API_KEY(环境变量或 --api-key)')

    def ask(self, prompt):
        body = json.dumps({
            'model': self.model, 'max_tokens': 8192,
            'messages': [{'role': 'user', 'content': prompt}],
        }).encode('utf-8')
        req = urllib.request.Request(self.URL, data=body, method='POST', headers={
            'x-api-key': self.key, 'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return ''.join(b.get('text', '') for b in data.get('content', [])) or None
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None


class OpenAI(Provider):
    def __init__(self, args):
        self.key = args.api_key or os.environ.get('OPENAI_API_KEY', '')
        self.base = (args.base_url or os.environ.get('OPENAI_BASE_URL', '')).rstrip('/')
        self.model = args.model or os.environ.get('OPENAI_MODEL') \
            or DEFAULT_OPENAI_MODEL
        if not self.base:
            self.base = 'https://api.openai.com/v1'
        if not self.key:
            log('[openai] 未配置 OPENAI_API_KEY:本地端点(Ollama/LM Studio)无需密钥,继续尝试;'
                '云端端点请设置环境变量')

    def ask(self, prompt):
        body = json.dumps({
            'model': self.model, 'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.7, 'max_tokens': 8192,
        }).encode('utf-8')
        req = urllib.request.Request(self.base + '/chat/completions', data=body,
                                     method='POST', headers={
            'Authorization': f'Bearer {self.key}',
            'Content-Type': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return data['choices'][0]['message']['content'] or None
        except (urllib.error.URLError, OSError, KeyError, IndexError, json.JSONDecodeError):
            return None


def _lenient_load(s):
    """容错 JSON:模型常把英文双引号裸写在字符串里("它是被当成 JSON 定界符")。
    策略:json.loads 失败时,把报错位置的裸 '"' 换成中文引号后重试,最多修 3 处。
    注:实测报错位置极少恰好落在引号上(多为引号后的下一个非结构字符),该分支
    修复能力有限 —— 修不动时返回 None,调用方走回退/重试路径,不会崩。"""
    cur = s
    for _ in range(3):
        try:
            return json.loads(cur)
        except json.JSONDecodeError as e:
            if e.pos >= len(cur) or cur[e.pos] != '"':
                return None
            cur = cur[:e.pos] + '“' + cur[e.pos + 1:]
    return None


def md_bold_to_html(s):
    """模型偶尔把强调写成 Markdown 加粗 **x**(Anki 不渲染 Markdown,星号会裸露)。
    组装前统一转成 <b>x</b>;幂等:无 ** 时原样返回。"""
    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s, flags=re.S)


def _clean(s):
    """字段消毒:单行化(模型被禁止换行,防御性兜底)、去首尾空白、md 加粗转 <b>。"""
    s = str(s or '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    s = s.replace('\\n', ' ').strip()
    return md_bold_to_html(s)


def make_tag_fn(word, sent, cefr, diff):
    """构造词身份判定函数(本地规则,不依赖模型):
    lemma 命中目标词 → 「目标词」;命中例句超纲词(hard_words_in 判定)→ 「超纲词」;
    其余 → 不标注。diff=None(单测注入)时恒不标注。"""
    if diff is None:
        return lambda w: ''
    tlem = lemma_of(word)
    hard_lemmas = {lemma_of(h) for h in
                   hard_words_in(sent or '', word, cefr or '', diff)}

    def tag_of(w):
        lems = {lemma_of(t) for t in re.findall(r"[A-Za-z]+(?:[’'-][A-Za-z]+)*", w)}
        if tlem and tlem in lems:
            return '目标词'
        return '超纲词' if lems & hard_lemmas else ''
    return tag_of


def compose_analysis(items, reading, culture, tag_of):
    """把模型返回的结构化字段确定性拼装成规范排版文本(源头即唯一格式,零正则矫正):
    1. 逐项解析
    • <b>成分</b>:讲解          ← items[].seg/note
    – <b>词</b>(身份):讲解      ← items[].words[];身份由 tag_of 本地规则判定
    2. 整句解读 / 3. 文化点(文化点为空则整段省略)"""
    lines = ['1. 逐项解析']
    for it in items:
        lines.append(f'• <b>{_clean(it["seg"])}</b>:{_clean(it["note"])}')
        for wd in it.get('words') or ():
            w_txt = _clean(wd['w'])
            tag = tag_of(wd['w'])
            suffix = f'({tag})' if tag else ''
            lines.append(f'– <b>{w_txt}</b>{suffix}:{_clean(wd["note"])}')
    lines.append('2. 整句解读')
    lines.append(_clean(reading))
    culture = _clean(culture)
    if culture:
        lines.append('3. 文化点')
        lines.append(culture)
    return '\n'.join(lines)


def parse_batch(text):
    """批量版解析:剥 markdown 代码块/多余文字,取第一个 { 到最后一个 } 后解析 JSON,
    按结构化 schema 严格校验,返回合法的结构化条目
    [{word, items:[{seg, note, words:[{w, note}]}], reading, culture, memo}, …]。
    任何字段缺失/类型不符(含模型回退成自由文本 ai_analysis 的旧格式)→ 该词剔除,
    由调用方走重试;可为空列表。"""
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
        w = str(p.get('word') or '').strip()
        memo = _clean(p.get('memo'))
        items, ok = [], isinstance(p.get('items'), list) and bool(p['items']) \
            and not _clean(p.get('ai_analysis'))   # 旧自由文本格式 = 不合格,触发重试
        for it in (p.get('items') or []):
            if not isinstance(it, dict) or not _clean(it.get('seg')) \
                    or not _clean(it.get('note')):
                ok = False
                break
            words = []
            for wd in (it.get('words') or []):
                if isinstance(wd, dict) and _clean(wd.get('w')) and _clean(wd.get('note')):
                    words.append({'w': _clean(wd.get('w')), 'note': _clean(wd.get('note'))})
            if any('�' in x for x in [it.get('seg'), it.get('note')] +
                   [x for wd in words for x in wd.values()]):
                ok = False
                break
            items.append({'seg': _clean(it.get('seg')), 'note': _clean(it.get('note')),
                          'words': words})
        reading = _clean(p.get('reading'))
        if w and ok and reading and memo and '�' not in reading and '�' not in memo:
            out.append({'word': w, 'items': items, 'reading': reading,
                        'culture': _clean(p.get('culture')), 'memo': memo})
    return out


def finalize_result(p, r, diff):
    """结构化条目 → 产物行 {word, ai_analysis, memo}:ai_analysis 由 compose_analysis
    本地拼装(格式确定性),不再接收模型排版的自由文本。"""
    tag_of = make_tag_fn(r['word'], r.get('sent') or '', r.get('cefr') or '', diff)
    return {'word': p['word'],
            'ai_analysis': compose_analysis(p['items'], p['reading'],
                                            p.get('culture') or '', tag_of),
            'memo': p['memo']}


def ask_batch_words(prov, rows, diff, verbose):
    """批量问询:一批词(1..BATCH 个)一次调用;解析缺词时回退单词 prompt 单独重试。
    返回 ({word: 解析}, [失败 word])。"""
    items = '\n\n'.join(build_info(r, hard_words_in(
        r.get('sent') or '', r['word'], r.get('cefr') or '', diff)) for r in rows)
    prompt = BATCH_PROMPT.replace('__ITEMS__', items)

    parsed = []
    for attempt in range(MAX_RETRY):
        if verbose:
            log(f'  {len(rows)} 词/批 尝试 {attempt + 1}/{MAX_RETRY} ...')
        parsed = parse_batch(prov.ask(prompt))
        if parsed:
            break

    by_word = {r['word']: r for r in rows}
    got, fails = {}, []
    for p in parsed:                          # 结构合法的词 → 本地拼装定型
        r = by_word.get(p['word'])
        if r:
            got.setdefault(p['word'], finalize_result(p, r, diff))
    for r in rows:
        if r['word'] in got:
            continue
        # 单词回退:整批解析缺词时,用单词 prompt 单独补(成本低),循环重试不递归
        hard = hard_words_in(r.get('sent') or '', r['word'], r.get('cefr') or '', diff)
        single_prompt = BATCH_PROMPT.replace('__ITEMS__', build_info(r, hard))
        one = None
        for attempt in range(MAX_RETRY):
            if verbose:
                log(f'  {r["word"]}(回退) 尝试 {attempt + 1}/{MAX_RETRY} ...')
            for p in parse_batch(prov.ask(single_prompt)):
                if p['word'] == r['word']:
                    one = finalize_result(p, r, diff)
                    break
            if one:
                break
        if one:
            got[r['word']] = one
        else:
            fails.append(r['word'])
    return got, fails


def process_chapter(ch, rows, prov, args, diff, out_dir, work_dir):
    """处理单章:返回 (生成数, 失败数, 跳过数)。产物文件按章独立,可被并发调用。"""
    out_json = os.path.join(work_dir, f'ai_explain_{args.book}_ch{ch:02d}.json')
    failed_json = os.path.join(work_dir, f'ai_explain_{args.book}_failed.json')

    done = set()
    if os.path.exists(out_json):
        done = {p['word'] for p in json.load(open(out_json, encoding='utf-8'))}
    failed = {p['word'] for p in _load_failed(failed_json)}

    todo = []
    for r in rows:
        if r['word'] in done:
            continue
        if r['word'] in failed and not args.verbose:
            continue                       # 已知失败词不重试(除非 --verbose 强试)
        todo.append(r)
    if args.limit:
        todo = todo[:args.limit]
    n_skip = len(rows) - len(todo)

    if args.dry_run:
        n_batches = (len(todo) + args.batch_size - 1) // args.batch_size if todo else 0
        log(f'[ch{ch:02d}] dry-run: 待处理 {len(todo)} 词 → {n_batches} 批'
            f'({args.batch_size} 词/批,每批最多 {MAX_RETRY} 次尝试)')
        return 0, 0, n_skip

    results, fails = [], []
    for i in range(0, len(todo), args.batch_size):
        batch = todo[i:i + args.batch_size]
        got, bad = ask_batch_words(prov, batch, diff, args.verbose)
        results += [got[w] for w in got]
        fails += [{'word': w} for w in bad]

    if results:
        # 写入前按 word 原子去重:断点+续跑组合下旧文件可能已含部分/全部词,
        # setdefault 保证同词只保留先出现的一份(旧文件优先),产物始终每章限额内
        merged = {}
        if os.path.exists(out_json):
            for p in json.load(open(out_json, encoding='utf-8')):
                merged.setdefault(p['word'],
                                  {'word': p['word'],
                                   **{k: p[k] for k in ('ai_analysis', 'memo')}})
        for p in results:
            merged.setdefault(p['word'], p)
        # 按 raw csv 词序排布;已在 done 中的词保持原顺序在前
        order = {r['word']: i for i, r in enumerate(rows)}
        merged_list = sorted(merged.values(), key=lambda p: order.get(p['word'], 10**9))
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(merged_list, f, ensure_ascii=False, indent=1)
        log(f'[ch{ch:02d}] 写入 {len(merged_list)} 词(新生成 {len(results)})')

    if fails:
        _save_failed(failed_json, [f['word'] for f in fails])

    log(f'[ch{ch:02d}] 生成 {len(results)} 失败 {len(fails)} 跳过 {n_skip}')
    return len(results), len(fails), n_skip


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--book', required=True)
    ap.add_argument('--provider', default='claude-cli',
                    choices=['claude-cli', 'anthropic', 'openai'])
    ap.add_argument('--chapter', type=int, default=0, help='只处理指定章(0=全部)')
    ap.add_argument('--limit', type=int, default=0, help='每章最多处理 N 词(0=不限,试跑用)')
    ap.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
                    help=f'每批词数(默认 {DEFAULT_BATCH_SIZE};调大省调用,注意模型输出长度上限)')
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'并发处理章数(默认 {DEFAULT_WORKERS};--workers 1 关闭并发)')
    ap.add_argument('--dry-run', action='store_true', help='只预览批次规划,不调用任何模型')
    ap.add_argument('--model', default='', help='覆盖默认模型(或环境变量 ANTHROPIC_MODEL/OPENAI_MODEL)')
    ap.add_argument('--base-url', default='', help='openai provider: 覆盖 OPENAI_BASE_URL')
    ap.add_argument('--api-key', default='', help='覆盖环境变量 API key(不入盘)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()
    if args.batch_size < 1:
        sys.exit('--batch-size 必须 ≥ 1')
    if args.workers < 1:
        sys.exit('--workers 必须 ≥ 1')

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    work_dir = os.path.join(out_dir, 'work')
    os.makedirs(work_dir, exist_ok=True)
    raw_dir = os.path.join(out_dir, 'raw')

    prov = Provider.create(args.provider, args)
    diff = Difficulty()
    diff.proper = proper_names.load(args.book)   # 书内专名不进超纲词列表(scripts/proper_names.py)
    files = sorted(f for f in os.listdir(raw_dir) if f.endswith('_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.startswith(f'chapter_{args.chapter:02d}_')]

    chapters = []
    for fn in files:
        ch = int(fn.split('_')[1])
        with open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline='') as f:
            rows = [r for r in csv.DictReader(f) if r.get('cn_mean')]
        chapters.append((ch, rows))

    n_workers = min(args.workers, len(chapters)) if chapters else 1
    total_ok = total_fail = total_skip = 0

    def run_one(item):
        ch, rows = item
        return process_chapter(ch, rows, prov, args, diff, out_dir, work_dir)

    if n_workers > 1:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(run_one, c): c[0] for c in chapters}
            for fut in as_completed(futs):
                ok, fail, skip = fut.result()
                total_ok += ok
                total_fail += fail
                total_skip += skip
    else:
        for c in chapters:
            ok, fail, skip = run_one(c)
            total_ok += ok
            total_fail += fail
            total_skip += skip

    if args.dry_run:
        print(f'DRY-RUN [provider={args.provider}] 待处理 {len(chapters)} 章'
              f'(workers={n_workers})', flush=True)
    else:
        print(f'DONE [provider={args.provider}] 新生成 {total_ok} 失败 {total_fail}'
              f' 跳过 {total_skip}', flush=True)
        print('下一步: uv run python scripts/apply_polish.py --book '
              f'{args.book} --explain data/output/{args.book}/work/ai_explain_{args.book}_ch01.json',
              flush=True)


if __name__ == '__main__':
    main()