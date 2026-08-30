"""书内专名扫描(新书第 0.5 步,零模型):发现疑似人名/地名,生成按书专名表建议。

流程:epub_to_md 转换 → **scan_proper 扫描确认** → pipeline(选词时自动排除)。
背景:pipeline 的 is_proper 启发式对「常作句首主语的人名」(如 Jerusha)会漏,
专名表现在按书外置(data/books/proper_names/<book>.txt,pipeline/hard_words/validate
共用),本脚本负责「发现候选」——报告 + 以注释行写入建议,确认(去注释)后才生效。

用法:uv run python scripts/scan_proper.py --book daddy_long_legs [--write] [--top 40]
  默认只打印报告;--write 把新疑似以注释行并入 proper_names/<book>.txt
  (文件不存在则创建;已生效行绝不改动 —— "不静默覆盖")
"""
import argparse, os, sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))

import proper_names
from pipeline import load_oxford


def main():
    ap = argparse.ArgumentParser(description='扫描书内疑似专名,维护 proper_names/<book>.txt')
    ap.add_argument('--book', required=True)
    ap.add_argument('--write', action='store_true',
                    help='把新疑似专名以注释行写入 proper_names/<book>.txt(不存在则创建)')
    ap.add_argument('--top', type=int, default=40, help='报告条数(默认 40)')
    args = ap.parse_args()

    md_path = os.path.join(BASE, 'data', 'books', '_md', f'{args.book}.md')
    if not os.path.exists(md_path):
        sys.exit(f'[STOP] 找不到 {md_path} —— 先跑 scripts/epub_to_md.py')
    ppath = proper_names.path_for(args.book)
    active = proper_names.load(args.book)
    sus = proper_names.suspects(open(md_path, encoding='utf-8').read(),
                                load_oxford(), exclude=active)
    new = [s for s in sus if s['word'] not in active]

    print(f'book: {args.book} | 专名表: {ppath}'
          + ('' if os.path.exists(ppath) else '(不存在)')
          + f' | 已生效 {len(active)} | 新疑似 {len(new)} / 疑似总数 {len(sus)}')
    print(f'{"word":<16}{"freq":>5}{"cap%":>6}{"nu%":>6}  note')
    for s in sus[:args.top]:
        note = '已生效' if s['word'] in active else '新疑似'
        print(f'{s["word"]:<16}{s["freq"]:>5}{s["cap"] * 100 // s["freq"]:>5}%'
              f'{s["nu"] * 100 // s["freq"]:>5}%  {note}')
    if len(sus) > args.top:
        print(f'… 其余 {len(sus) - args.top} 条见 --top N 或 --write 后的文件')

    if args.write:
        os.makedirs(os.path.dirname(ppath), exist_ok=True)
        existing = ''
        if os.path.exists(ppath):
            with open(ppath, encoding='utf-8') as f:
                existing = f.read()
        # 解析式去重:取每行(含注释建议行)首 token;子串 in 判断会误判('ace' in 'spacecraft')
        mentioned = {ln.lstrip('#').split('#', 1)[0].split()[0].lower()
                     for ln in existing.splitlines() if ln.strip()}
        add = [s for s in new if s['word'] not in mentioned]
        if add:
            if existing and not existing.endswith('\n'):
                existing += '\n'
            block = ''.join(f'# {s["word"]}  # 疑似专名 freq={s["freq"]} cap={s["cap"]}/{s["freq"]}\n'
                            for s in add)
            with open(ppath, 'w', encoding='utf-8', newline='\n') as f:
                f.write(existing + block)
            print(f'[OK] 已写入 {len(add)} 条建议(注释态) -> {ppath};'
                  f'确认后去掉行首 # 再跑 pipeline')
        else:
            print('[OK] 无新增建议')


if __name__ == '__main__':
    main()
