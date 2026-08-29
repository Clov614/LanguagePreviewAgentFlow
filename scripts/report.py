"""全书推荐报告(⑪):从生词总库按价值排序,输出分档推荐清单
打分 = 书中累计频次 × 级别价值 × 常用度
用法: uv run python scripts/report.py --book little_women
"""
import csv, json, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACE = {'b1': 0.3, 'b2': 1.0, 'c1': 1.2, 'toe': 0.5}

def main():
    book = sys.argv[2] if len(sys.argv) > 2 else 'little_women'
    wl = os.path.join(BASE, 'vocabulary', 'master_wordlist.csv')
    rows = list(csv.DictReader(open(wl, encoding='utf-8')))
    scored = []
    for r in rows:
        fb = int(r['freq_book'] or 0)
        score = (ACE.get(r['cefr'], 0.5) * 2.0
                 + min(fb, 40) / 40.0 * 3.0)
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    out.append(f'# 《Little Women》全书生词推荐报告\n')
    out.append(f'生词总库: **{len(rows)} 词**(B2: {sum(1 for r in rows if r["cefr"]=="b2")}, '
               f'C1: {sum(1 for r in rows if r["cefr"]=="c1")}, 其他: {sum(1 for r in rows if r["cefr"] not in ("b2","c1"))})\n')
    out.append('## 一、全书 TOP 40(书中出现最频繁 + 级别最高,优先掌握)\n')
    out.append('| # | 单词 | 级别 | 书中累计出现 | 卡片来源 | 中文释义 |')
    out.append('|---|------|------|-------------|---------|---------|')
    for i, (s, r) in enumerate(scored[:40], 1):
        out.append(f"| {i} | **{r['word']}** | {r['cefr'].upper()} | {r['freq_book']} | {r['book']} Ch{r['chapters']} | {r['example_cn'][:0] or ''}{(_cn(r))} |")
    out.append('')
    by_ch = {}
    for r in rows:
        ch = r['chapters'].split(',')[0]
        by_ch.setdefault(ch, []).append(r)
    out.append('## 二、按章学习路径(建议顺序:先刷卡片,再读对应章节)\n')
    out.append('刷卡顺序 = 章节顺序。每章卡片见 `data/output/little_women/chapter_XX_anki.tsv`,'
               '读前刷该章 15 分钟,读完在 `annotated/` 高亮版中识别。\n')
    out.append('## 三、使用说明\n')
    out.append('- **Anki 导入**: Anki 桌面版 → File → Import → 选择 `chapter_XX_anki.tsv` (UTF-8, Tab 分隔, 首行字段名)')
    out.append('- **卡片字段**: 单词 | 音标 | 词性 | 中文释义 | CEFR | 原文例句 | 例句译文 | 来源')
    out.append('- **学习节奏**: 每天 1 章 15 分钟刷卡 → 读对应章节(参考标注版)→ 认识后在总库标记 known')
    out.append('- **总库去重**: 将来读第二本书时,已掌握词不会重复推荐')
    with open(os.path.join(BASE, 'data', 'output', book, 'recommend_report.md'),
              'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print('DONE -> recommend_report.md', flush=True)

def _cn(r):
    return (r.get('example_cn') or '')[:26]

if __name__ == '__main__':
    main()