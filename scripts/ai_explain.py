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

多章并发(--workers,默认 8):并行粒度是"批"而非"章" —— 全书所有待处理批次进同一个
  线程池,workers 即同时在飞的请求数;总时长 ≈ 批数 × 单批耗时 / workers。
  各章产物文件独立、天然无冲突;被上游限流产生失败词时,重跑即补漏(断点跳过已生成词)。

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
import datetime
import glob
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
DEFAULT_WORKERS = 8         # 并发批次数(=同时在飞的请求数):批级并行,2026-08-31 由 4 调高
TIMEOUT = 420               # 单次请求超时(秒):实测批次均耗时 ~4min,300s 会让慢批
                            # 超时重试 —— 超时前已生成的 token 照样计费,等于双倍烧钱

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
    """三接入点抽象:ask(prompt) -> 文本 或 None(失败)。线程安全(每次调起独立请求)。
    失败时把可读原因写入 self.last_error(--verbose 时随重试日志打印,不再黑盒重试)。"""

    last_error = ''

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
        except (subprocess.TimeoutExpired, OSError) as e:
            self.last_error = f'claude CLI 调用失败: {e}'
            return None
        raw = p.stdout or b''
        text = None
        for enc in ('utf-8', 'gbk'):
            try:
                text = raw.decode(enc).strip() or None
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode('utf-8', errors='replace').strip() or None
        # CLI 侧配置错误(中转网关失效/模型无权限等)会以短错误文本吐出,
        # 别当模型输出送去解析 —— 记入 last_error(--verbose 可见)并判失败
        head = (text or '').lstrip().lower()
        if head.startswith(("there's an issue", 'api error', 'error:', 'invalid')):
            self.last_error = (text or '')[:200]
            return None
        return text


class Anthropic(Provider):
    URL = 'https://api.anthropic.com/v1/messages'

    def __init__(self, args):
        self.key = args.api_key or os.environ.get('ANTHROPIC_API_KEY', '')
        self.model = args.model or os.environ.get('ANTHROPIC_MODEL') \
            or DEFAULT_ANTHROPIC_MODEL
        # ANTHROPIC_BASE_URL 与官方 SDK 同语义:自建代理网关(one-api 等)也走 Anthropic 协议;
        # 值不带 /v1 时自动补。OpenAI 兼容网关请用 --provider openai
        base = (args.base_url or os.environ.get('ANTHROPIC_BASE_URL') or '').rstrip('/')
        if base:
            self.url = (base[:-3] if base.endswith('/v1') else base) + '/v1/messages'
        else:
            self.url = self.URL
        if not self.key:
            sys.exit('[anthropic] 缺 ANTHROPIC_API_KEY(环境变量或 --api-key)')

    def ask(self, prompt):
        body = json.dumps({
            'model': self.model, 'max_tokens': 8192,
            'messages': [{'role': 'user', 'content': prompt}],
        }).encode('utf-8')
        req = urllib.request.Request(self.url, data=body, method='POST', headers={
            'x-api-key': self.key, 'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return ''.join(b.get('text', '') for b in data.get('content', [])) or None
        except urllib.error.HTTPError as e:
            self.last_error = f'{self.url} -> HTTP {e.code}: ' + \
                (e.read() or b'')[:200].decode('utf-8', 'replace')
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self.last_error = f'{self.url} -> {e}'
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
            tag = tag_of(w_txt)
            note = _clean(wd['note'])
            if tag:   # 身份已由代码标注,讲解开头再念一遍「超纲词/目标词」是冗余
                stripped = re.sub(rf'^{tag}\s*[。:：,，.;；、\s]*', '', note)
                if stripped:
                    note = stripped
            suffix = f'({tag})' if tag else ''
            lines.append(f'– <b>{w_txt}</b>{suffix}:{note}')
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
        raw = prov.ask(prompt)
        parsed = parse_batch(raw)
        if parsed:
            break
        if verbose:
            log(f'  批次无有效结果: {(getattr(prov, "last_error", "") or (raw or "")[:120])[:160]}')

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
            raw_one = prov.ask(single_prompt)
            for p in parse_batch(raw_one):
                if p['word'] == r['word']:
                    one = finalize_result(p, r, diff)
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


def plan_chapter(ch, rows, args, work_dir):
    """章内规划:按断点(产物已有词)与失败记录筛出本章待处理词。
    返回 (todo, n_skip, out_json)。"""
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
    return todo, len(rows) - len(todo), out_json


def write_chapter(ch, rows, got_new, out_json):
    """章结果落盘:与既有产物按 word 原子去重合并(旧文件优先,断点+续跑幂等),
    按 raw csv 词序排布。返回写入总数;无任何(新+旧)产物则不写文件。"""
    merged = {}
    if os.path.exists(out_json):
        for p in json.load(open(out_json, encoding='utf-8')):
            merged.setdefault(p['word'],
                              {'word': p['word'],
                               **{k: p[k] for k in ('ai_analysis', 'memo')}})
    for p in got_new.values():
        merged.setdefault(p['word'], p)
    if not merged:
        return 0
    order = {r['word']: i for i, r in enumerate(rows)}
    merged_list = sorted(merged.values(), key=lambda p: order.get(p['word'], 10**9))
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(merged_list, f, ensure_ascii=False, indent=1)
    return len(merged_list)


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
                    help=f'并发批次数 = 同时在飞的请求数(默认 {DEFAULT_WORKERS};'
                         f'批级并行,章内批次也并行;被限流就调低或重跑补漏)')
    ap.add_argument('--dry-run', action='store_true', help='只预览批次规划,不调用任何模型')
    ap.add_argument('--clear', action='store_true',
                    help='重生成前清场(不调用模型):产物 JSON 备份到 work/_old_format_<日期>/,'
                         '并清空单词 raw 的 ai_analysis/memo 两列(润色列与表达 raw 不动)')
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

    if args.clear:
        backup = os.path.join(work_dir, f'_old_format_{datetime.date.today():%Y%m%d}')
        os.makedirs(backup, exist_ok=True)
        n_moved = 0
        for fp in glob.glob(os.path.join(work_dir, f'ai_explain_{args.book}_*.json')):
            shutil.move(fp, os.path.join(backup, os.path.basename(fp)))
            n_moved += 1
        n_cleared = 0
        for fp in sorted(glob.glob(os.path.join(out_dir, 'raw', 'chapter_*_raw.csv'))):
            if fp.endswith('_phrase_raw.csv'):   # 表达解析是 ai_pick_phrases 产物,不在重生成范围
                continue
            with open(fp, encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            if not rows:
                continue
            fields = list(rows[0].keys())
            for r in rows:
                for k in ('ai_analysis', 'memo'):
                    if r.get(k):
                        r[k] = ''
                        n_cleared += 1
            with open(fp, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
                w.writeheader()
                w.writerows(rows)
        log(f'--clear 完成: {n_moved} 个产物 JSON 移入 {os.path.relpath(backup, BASE)},'
            f'清空解析列 {n_cleared} 格(润色列不动)。'
            f'下一步: ai_explain → apply_polish --explain → cards')
        return

    prov = Provider.create(args.provider, args)
    diff = Difficulty()
    diff.proper = proper_names.load(args.book)   # 书内专名不进超纲词列表(scripts/proper_names.py)
    # 表达卡 raw(_phrase_raw.csv)是 ai_pick_phrases 的领地,解析风格不同,不在这里重生成
    files = sorted(f for f in os.listdir(raw_dir)
                   if f.endswith('_raw.csv') and not f.endswith('_phrase_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.startswith(f'chapter_{args.chapter:02d}_')]

    chapters = []
    for fn in files:
        ch = int(fn.split('_')[1])
        with open(os.path.join(raw_dir, fn), encoding='utf-8-sig', newline='') as f:
            rows = [r for r in csv.DictReader(f) if r.get('cn_mean')]
        chapters.append((ch, rows))

    # 规划:全书所有待处理批次进同一个池 —— 并行粒度是"批"而非"章"。
    # 旧版按章并行时每章 3 批只能串行,同一时刻最多 workers 个请求在飞,是时长瓶颈;
    # 批级并行把 141 批摊到 workers 个并发请求上,总时长 ≈ 批数 × 单批耗时 / workers
    jobs, ch_info = [], {}
    for ch, rows in chapters:
        todo, n_skip, out_json = plan_chapter(ch, rows, args, work_dir)
        ch_info[ch] = {'rows': rows, 'skip': n_skip, 'out': out_json, 'got': {}}
        if args.dry_run:
            log(f'[ch{ch:02d}] dry-run: 待处理 {len(todo)} 词 → '
                f'{(len(todo) + args.batch_size - 1) // args.batch_size if todo else 0} 批'
                f'({args.batch_size} 词/批,每批最多 {MAX_RETRY} 次尝试)')
        for i in range(0, len(todo), args.batch_size):
            jobs.append((ch, todo[i:i + args.batch_size]))
    if args.dry_run:
        print(f'DRY-RUN [provider={args.provider}] 共 {len(ch_info)} 章、'
              f'{len(jobs)} 批(workers={args.workers} = 并发请求数)', flush=True)
        return

    failed_json = os.path.join(work_dir, f'ai_explain_{args.book}_failed.json')
    total = len(jobs)
    n_workers = max(1, min(args.workers, total)) if total else 1
    log(f'待处理 {total} 批(每批 ≤{args.batch_size} 词),并发 {n_workers} 个请求')

    def run_job(job):
        ch, batch = job
        got, fails = ask_batch_words(prov, batch, diff, args.verbose)
        return ch, got, fails

    total_fail = 0
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(run_job, j) for j in jobs]
        for n_done, fut in enumerate(as_completed(futs), 1):
            ch, got, fails = fut.result()
            ch_info[ch]['got'].update(got)
            total_fail += len(fails)
            if fails:
                _save_failed(failed_json, fails)
            # 每批完成立刻落盘(与既有产物合并,幂等):中断/取消只损失在飞批次,
            # 已完成批次全部保留,重跑断点续传不重复烧 token
            write_chapter(ch, ch_info[ch]['rows'], ch_info[ch]['got'], ch_info[ch]['out'])
            log(f'[进度] 批次 {n_done}/{total} 完成 → ch{ch:02d} 累计 '
                f'{len(ch_info[ch]["got"])} 词')

    total_ok = total_skip = 0
    for ch in sorted(ch_info):
        info = ch_info[ch]
        n = write_chapter(ch, info['rows'], info['got'], info['out'])
        if n:
            log(f'[ch{ch:02d}] 写入 {n} 词(新生成 {len(info["got"])})')
        total_ok += len(info['got'])
        total_skip += info['skip']

    print(f'DONE [provider={args.provider}] 新生成 {total_ok} 失败 {total_fail}'
          f' 跳过 {total_skip}', flush=True)
    print('下一步: uv run python scripts/apply_polish.py --book '
          f'{args.book} --explain data/output/{args.book}/work/ai_explain_{args.book}_ch01.json',
          flush=True)


if __name__ == '__main__':
    main()