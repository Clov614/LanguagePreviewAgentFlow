"""把模型润色结果合并回 raw CSV:
- polish 数据(main): word -> {cn_mean, cn_sent}
- ai_en 数据(可选): word -> {ai_en}  书中无例句的词,补充英文例句写入 sent
用法: uv run python scripts/apply_polish.py --book little_women \
      --polish data/output/little_women/polish_ch01-02.json [--ai-en <json>] [--chapter 1]
"""
import argparse, csv, glob, json, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--polish', required=True, help='润色 JSON 文件')
    ap.add_argument('--ai-en', default='', help='补句 JSON 文件')
    ap.add_argument('--chapter', type=int, default=0, help='只处理指定章')
    args = ap.parse_args()

    polish = {p['word']: p for p in json.load(open(args.polish, encoding='utf-8'))}
    ai_en = {p['word']: p['ai_en'] for p in polish.values() if p.get('ai_en')}
    if args.ai_en:
        ai_en.update({p['word']: p['ai_en'] for p in json.load(open(args.ai_en, encoding='utf-8'))})

    out_dir = os.path.join(BASE, 'data', 'output', args.book)
    files = sorted(f for f in glob.glob(os.path.join(out_dir, 'raw', 'chapter_*_raw.csv'))
                   if f.endswith('_raw.csv'))
    if args.chapter:
        files = [f for f in files if f.endswith(f'chapter_{args.chapter:02d}_raw.csv')]

    n_all = n_ai = 0
    for fp in files:
        # utf-8-sig 读取:无 BOM 文件照常读,带 BOM 文件剥掉 BOM(防字段名被 ﻿ 污染)
        with open(fp, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            p = polish.get(r['word'])
            if p:
                # 中文释义/译文里的换行→'；',避免 Excel 里整行竖排撑高
                r['cn_mean'] = p.get('cn_mean', '').replace('\n', '；')
                r['cn_sent'] = p.get('cn_sent', '').replace('\n', '；')
                n_all += 1
            # AI 补句独立于 polish 匹配:缺原句的词即使不在 polish 文件里也能补(仅靠 --ai-en)
            if not r['sent'] and r['word'] in ai_en:
                r['sent'] = ai_en[r['word']].replace('\n', ' ')
                r['ai'] = '1'
                n_ai += 1
        fields = []
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
        with open(fp, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f'[OK] {os.path.basename(fp)}: 润色 {sum(1 for r in rows if r.get("cn_mean"))} 词 (补句 {n_ai})', flush=True)
    print(f'DONE 共润色 {n_all} 词', flush=True)

if __name__ == '__main__':
    main()