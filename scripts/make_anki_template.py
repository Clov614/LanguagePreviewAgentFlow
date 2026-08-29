"""生成 Anki 笔记类型模板包(.apkg),一次导入后即可直接导入各章 TSV:
- 笔记类型: EnWords(8 字段,顺序与 chapter_XX_anki.tsv 的 8 列一致)
- 卡片模板: 正面 = 单词+音标+词性+CEFR; 背面 = 中文释义+原文例句+例句译文+来源
- 牌组: EnglishBooksWords::LittleWomen(含 1 张示例卡,导入后可删)

用法:  uv run --with genanki python scripts/make_anki_template.py
输出:  resources/anki/anki_template.apkg
导入:  Anki 桌面版 → File → Import → 选择 anki_template.apkg
       (之后导入每章 TSV 时,笔记模板选 EnWords 即可)
"""
import os

import genanki

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, 'resources', 'anki', 'anki_template.apkg')

# 固定 ID:重复生成同一模板,不产生新笔记类型副本
MODEL_ID = 1042168908
DECK_ID = 1042168916

CSS = """
.card { font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
        text-align: left; line-height: 1.7; }
.word { font-size: 34px; font-weight: bold; }
.meta { color: #888; font-size: 16px; margin: 4px 0 14px; }
.cefr { color: #2a7de1; font-weight: bold; }
.mean { font-size: 22px; margin-bottom: 10px; }
.sent { border-left: 3px solid #2a7de1; padding-left: 10px; color: #333; }
.sent-cn { color: #888; margin-top: 4px; padding-left: 10px; }
.src { color: #aaa; font-size: 13px; margin-top: 12px; text-align: right; }
"""

MODEL = genanki.Model(
    MODEL_ID,
    'EnWords',
    fields=[
        {'name': '单词'},
        {'name': '音标'},
        {'name': '词性'},
        {'name': '中文释义'},
        {'name': 'CEFR'},
        {'name': '原文例句'},
        {'name': '例句译文'},
        {'name': '来源'},
    ],
    templates=[{
        'name': 'EnWords Card',
        'qfmt': ('<div class="word">{{单词}}</div>\n'
                 '<div class="meta">{{音标}} · {{词性}} · '
                 '<span class="cefr">{{CEFR}}</span></div>'),
        'afmt': ('{{FrontSide}}\n'
                 '<hr id="answer">\n'
                 '<div class="mean">{{中文释义}}</div>\n'
                 '<div class="sent">{{原文例句}}</div>\n'
                 '<div class="sent-cn">{{例句译文}}</div>\n'
                 '<div class="src">{{来源}}</div>'),
    }],
    css=CSS,
)

# 示例卡:chapter_01 真实数据,导入后可删除
SAMPLE = genanki.Note(
    model=MODEL,
    fields=[
        'decidedly', "di'saididli", '—', '果断地；显然，毫无疑问', 'C1',
        '"I shall get a nice box of Faber\'s drawing pencils; I really '
        'need them," said Amy decidedly.',
        '“我真的需要它们，”艾米说得斩钉截铁。', 'little_women Ch1',
    ],
)

def main():
    deck = genanki.Deck(DECK_ID, 'EnglishBooksWords::LittleWomen')
    deck.add_note(SAMPLE)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    genanki.Package(deck).write_to_file(OUT)
    print('DONE ->', OUT, flush=True)

if __name__ == '__main__':
    main()