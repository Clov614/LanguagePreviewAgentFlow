"""EPUB → 管线输入 Markdown(新书第 0 步,固化 MarkItDown 转换流程)

pipeline.py 只读 data/books/_md/<book>.md 并按 **Chapter N 标题** 切章;
本脚本把「EPUB→MD」固化成两步:
  1. Microsoft MarkItDown 把 EPUB 转成原始 MD(缓存 data/books/_md/<book>_raw.md)
  2. 后处理:剥扉页/目录/装饰/图片/元数据,按标题层级切节 —— 带正文的顶层节
     (如 Blue Wednesday)每节一章;碎节(书信的日期小标题等)按累计词数聚成章,
     合成管线要求的 **Chapter N 标题** 标记,正文段落与硬换行原样保留

用法(与 genanki 同款 --with 注入,不进核心依赖):
  uv run --with markitdown python scripts/epub_to_md.py \
      --epub "data/books/X.epub" --book x [--target-tokens 3500] [--max-tokens 4500] [--force]
零模型调用;重跑幂等(--force 才覆盖产物)。
"""
import argparse, os, re, sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORD_RE = re.compile(r"[A-Za-z]+(?:[’'-][A-Za-z]+)*")
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
IMAGE_RE = re.compile(r'^!\[[^\]]*\]\([^)]*\)$')
META_RE = re.compile(r'^\*\*[^*:\n]+:\*\*\s')          # markitdown 元数据行 **Title:** …
ESCAPE_RE = re.compile(r'\\([\\`*_{}\[\]()#+.!-])')
DECOR_RE = re.compile(r'^[\s*\\-]+$')                   # * * * / --- 之类装饰标题


def unescape(t):
    return ESCAPE_RE.sub(r'\1', t).strip()


def tokens(t):
    return len(WORD_RE.findall(t))


def parse_units(md_text):
    """原始 MD → 顶层节列表;unit = {level, title, paras:[段落], subs:[碎节]}
    段落保留 markitdown 的硬换行(管线把换行当空白;标注版阅读体验更好)"""
    units, cur = [], []

    def flush():
        if cur and units:
            units[-1]['paras'].append('\n'.join(cur).strip())
        cur.clear()

    for line in md_text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            title = unescape(m.group(2))
            if title and not DECOR_RE.match(title):
                units.append({'level': len(m.group(1)), 'title': title,
                              'paras': [], 'subs': []})
            continue
        s = line.strip()
        if not s:
            flush()
            continue
        if IMAGE_RE.match(s) or META_RE.match(s) or not units:
            continue
        cur.append(line.rstrip())
    flush()
    tops = []
    for u in units:
        if u['level'] == 1 or not tops:
            tops.append(u)
        else:
            tops[-1]['subs'].append(u)
    return tops


def group_subs(subs, pre_paras, target, maxt):
    """碎节(书信等)按累计词数聚章:达到 target 封章,超过 maxt 提前封章。
    cur_toks 为滚动累计(含各节标题词),避免每步重切整章文本的 O(n²)"""
    out = []
    cur_toks = 0
    for sub in subs:
        add = tokens(sub['title'] + ' ' + ' '.join(sub['paras']))
        cur = out[-1] if out else None
        if cur is None:
            out.append({'title': sub['title'], 'paras': list(sub['paras']), 'units': 1})
            cur_toks = add
        elif cur_toks >= target or cur_toks + add > maxt:
            out.append({'title': sub['title'],
                        'paras': [sub['title']] + list(sub['paras']), 'units': 1})
            cur_toks = add
        else:
            cur['paras'] += [sub['title']] + list(sub['paras'])
            cur['units'] += 1
            cur_toks += add
    if out and pre_paras:
        out[0]['paras'] = list(pre_paras) + out[0]['paras']
    return out


def build_chapters(tops, target, maxt):
    """顶层节 → 章:带正文每节一章;纯容器节(如「The Letters of …」)标题并入
    下一章正文开头;碎节容器交给 group_subs 聚章"""
    chapters, pending = [], []
    for u in tops:
        if u['subs']:
            pre = list(pending) + ([u['title']] if u['title'] else []) + u['paras']
            pending = []
            chapters += group_subs(u['subs'], pre, target, maxt)
        elif u['paras']:
            chapters.append({'title': u['title'],
                             'paras': list(pending) + u['paras'], 'units': 1})
            pending = []
        elif u['title']:
            pending.append(u['title'])
    return chapters


def run_markitdown(epub_path):
    """优先库内调用(uv --with markitdown 场景);库不可用则回退 PATH 上的 markitdown CLI
    (用户全局安装,Windows 上不要用 python -m,托管 python 未必是装它的那个)"""
    try:
        from markitdown import MarkItDown
    except ImportError:
        import shutil, subprocess
        exe = shutil.which('markitdown')
        if not exe:
            sys.exit('[STOP] markitdown 不可用:请 `uv run --with markitdown python '
                     'scripts/epub_to_md.py …` 或本机安装 `pip install markitdown`')
        print(f'    (库不可用,回退 CLI {exe})', flush=True)
        r = subprocess.run([exe, epub_path], capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        if r.returncode != 0 or not r.stdout.strip():
            sys.exit(f'[STOP] markitdown 转换失败:\n{r.stderr[-2000:]}')
        return r.stdout
    return MarkItDown().convert(epub_path).text_content


def main():
    ap = argparse.ArgumentParser(description='EPUB → 管线输入 MD(MarkItDown + 合成 **Chapter N** 标记)')
    ap.add_argument('--epub', required=True, help='EPUB 路径(相对仓库根或绝对)')
    ap.add_argument('--book', required=True, help='书名 slug,输出 data/books/_md/<slug>.md')
    ap.add_argument('--target-tokens', type=int, default=3500,
                    help='碎节聚章目标词数(默认 3500,≈little_women 每章体量)')
    ap.add_argument('--max-tokens', type=int, default=4500, help='碎节聚章词数上限')
    ap.add_argument('--reconvert', action='store_true', help='重跑 markitdown(默认复用 <book>_raw.md 缓存)')
    ap.add_argument('--force', action='store_true', help='覆盖输出 MD(不重跑 markitdown)')
    args = ap.parse_args()

    epub = args.epub if os.path.isabs(args.epub) else os.path.join(BASE, args.epub)
    md_dir = os.path.join(BASE, 'data', 'books', '_md')
    raw_md = os.path.join(md_dir, f'{args.book}_raw.md')
    out_md = os.path.join(md_dir, f'{args.book}.md')
    if not os.path.exists(epub):
        sys.exit(f'[STOP] 找不到 {epub}')
    if os.path.exists(out_md) and not args.force:
        sys.exit(f'[STOP] {out_md} 已存在,确认重转请加 --force(--reconvert 连 markitdown 一起重跑)')

    if args.reconvert or not os.path.exists(raw_md):
        print(f'[1/2] markitdown 转换 {os.path.basename(epub)} …', flush=True)
        text = run_markitdown(epub)
        os.makedirs(md_dir, exist_ok=True)
        with open(raw_md, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
    else:
        print(f'[1/2] 复用已有缓存 {raw_md}', flush=True)
        with open(raw_md, encoding='utf-8') as f:
            text = f.read()

    mtitle = re.search(r'^\*\*Title:\*\*\s+(.+)$', text, re.M)
    tops = parse_units(text)
    ci = next((i for i, u in enumerate(tops)
               if u['title'].strip().lower() == 'contents'), None)
    book_title = (mtitle.group(1).strip() if mtitle else '')
    if ci is not None:
        tops = tops[ci + 1:]          # 扉页/版权/目录:全部丢弃
    else:
        # 无 Contents 节的兜底:丢弃书名节(其版权文字随节丢弃)与无正文的引导节
        bt = book_title.strip().lower()
        while tops and ((not tops[0]['paras'] and not tops[0]['subs'])
                        or (bt and tops[0]['title'].strip().lower() == bt)):
            print(f'[INFO] 丢弃扉页节: {tops[0]["title"] or "(无标题)"}', flush=True)
            tops.pop(0)
    if not tops:
        sys.exit('[STOP] 未找到正文章节,检查 EPUB 结构')
    chapters = build_chapters(tops, args.target_tokens, args.max_tokens)

    if not book_title:
        book_title = tops[0]['title'] or args.book
    lines = [f'# {book_title}', '']
    for i, ch in enumerate(chapters, 1):
        title = re.sub(r'\s+', ' ', re.sub(r'[*\\\n]', ' ', ch['title'])).strip()
        lines.append(f'**Chapter {i} {title}**')
        lines.append('')
        for p in ch['paras']:
            if p.strip():
                lines.append(p)
                lines.append('')
    with open(out_md, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))

    print(f'[2/2] 合成章节标记:{len(tops)} 个顶层节 -> {len(chapters)} 章')
    print(f'{"ch":>3}  {"units":>5}  {"tokens":>6}  title')
    for i, ch in enumerate(chapters, 1):
        print(f'{i:>3}  {ch["units"]:>5}  {tokens(" ".join(ch["paras"])):>6}  {ch["title"]}')
    total = sum(tokens(' '.join(c['paras'])) for c in chapters)
    print(f'total: {len(chapters)} chapters / {total} tokens -> {out_md}', flush=True)


if __name__ == '__main__':
    main()
