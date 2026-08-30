"""生成 Anki 笔记类型模板包(.apkg),一次导入后即可直接导入各章 TSV:
- 笔记类型: EnWords(10 字段,顺序与 chapter_XX_anki.tsv 的 10 列一致)
- 卡片模板: 正面 = 单词+音标+词性+CEFR;
            背面 = 中文释义+原文例句(目标词 hl/超纲词 hard)+例句译文+来源+AI解析+词义概述
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
.sent b.hl { color: #c7254e; font-weight: bold;
             background: #fdeaea; padding: 0 2px; border-radius: 3px; }
.sent b.hard { color: #1e8449; font-weight: bold;
               border-bottom: 1px dashed #1e8449; }
.sent-cn { color: #888; margin-top: 4px; padding-left: 10px; }
.ai { margin-top: 10px; font-size: 15px; color: #444;
      background: #f2f7fc; padding: 8px 10px; border-radius: 6px; }
.ai-title { color: #2a7de1; font-weight: bold; margin-bottom: 4px; }
.memo { margin-top: 8px; font-size: 16px; color: #8a6d3b; font-style: italic; }
.src { color: #aaa; font-size: 13px; margin-top: 12px; text-align: right; }

/* 深色主题适配:Anki 深色模式会给卡片容器添加 nightMode 类,
   #333 等浅色主题文字在深色背景上会看不清,此处逐项覆盖 */
.card.nightMode { background: #26262a; }
.nightMode .word { color: #f2f2f2; }
.nightMode .meta { color: #9c9c9c; }
.nightMode .cefr { color: #6fb1ff; }
.nightMode .mean { color: #f2f2f2; }
.nightMode .sent { color: #e6e6e6; }
.nightMode .sent b.hl { color: #ff9eb3; font-weight: bold;
                        background: #4a2230; padding: 0 2px; border-radius: 3px; }
.nightMode .sent b.hard { color: #7fd8a4; border-bottom: 1px dashed #7fd8a4; }
.nightMode .sent-cn { color: #b0b0b0; }
.nightMode .ai { color: #cfcfcf; background: #31313a; }
.nightMode .ai-title { color: #6fb1ff; }
.nightMode .memo { color: #d9b96a; }
.nightMode .src { color: #7a7a7a; }
.nightMode hr { border-top: 1px solid #4a4a4e; }
"""

def autoplay_script(direction):
    """自动播放脚本:Anki 桌面端把 [sound:] 渲染成 .replay-button(click() 等价手动点击,
    播放走 Anki 原生通道);移动端渲染成 <audio> 标签,兜底调 play()。
    direction=-1 播第一个(正面=单词音);=1 播最后一个(背面=例句音,例句无音时回退单词音)。
    播放失败静默吞掉,不影响卡片复习。"""
    return (
        '<script>\n'
        '(function () {\n'
        "  'use strict';\n"
        '  var DIR = %d;\n'
        '  function enAutoplay() {\n'
        '    try {\n'
        "      var btns = document.querySelectorAll('.replay-button');\n"
        "      var auds = document.querySelectorAll('audio');\n"
        '      if (btns.length) {\n'
        '        (DIR > 0 ? btns[btns.length - 1] : btns[0]).click();\n'
        '        return;\n'
        '      }\n'
        '      if (auds.length) {\n'
        '        var a = (DIR > 0 ? auds[auds.length - 1] : auds[0]);\n'
        '        var p = a.play();\n'
        "        if (p && p.catch) { p.catch(function () {}); }\n"
        '      }\n'
        '    } catch (e) {}\n'
        '  }\n'
        "  var act = function () { window.setTimeout(enAutoplay, 300); };\n"
        "  if (document.readyState === 'loading') {\n"
        "    document.addEventListener('DOMContentLoaded', act);\n"
        '  } else {\n'
        '    act();\n'
        '  }\n'
        '})();\n'
        '</script>'
    ) % direction


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
        {'name': 'AI解析'},
        {'name': '词义概述'},
    ],
    templates=[{
        'name': 'EnWords Card',
        'qfmt': ('<div class="word">{{单词}}</div>\n'
                 '<div class="meta">{{音标}} · {{词性}} · '
                 '<span class="cefr">{{CEFR}}</span></div>\n'
                 + autoplay_script(-1)),
        'afmt': ('{{FrontSide}}\n'
                 '<hr id="answer">\n'
                 '<div class="mean">{{中文释义}}</div>\n'
                 '<div class="sent">{{原文例句}}</div>\n'
                 '<div class="sent-cn">{{例句译文}}</div>\n'
                 '{{#AI解析}}<div class="ai"><div class="ai-title">🤖 例句解析</div>'
                 '{{AI解析}}</div>{{/AI解析}}\n'
                 '{{#词义概述}}<div class="memo">💡 {{词义概述}}</div>{{/词义概述}}\n'
                 '<div class="src">{{来源}}</div>\n'
                 + autoplay_script(1)),
    }],
    css=CSS,
)

# 示例卡:chapter_01 真实数据,导入后可删除
SAMPLE = genanki.Note(
    model=MODEL,
    fields=[
        'decidedly', "di'saididli", '—', '果断地；显然，毫无疑问', 'C1',
        '"I shall get a nice box of Faber\'s drawing pencils; I really '
        'need them," said Amy <b class="hl">decidedly</b>.',
        '“我真的需要它们，”艾米说得斩钉截铁。', 'little_women Ch1',
        '“逐项解析”示例：<br>1. …<br>2. …<br>“整句解读”…<br>“文化点”…',
        'decidedly = 斩钉截铁 —— 她说这句时下巴一扬，没有商量余地。',
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