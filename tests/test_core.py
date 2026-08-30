"""核心纯函数单测(无需网络/模型/词典资源)。
覆盖 2026-08-30 审查整改的关键修复:known 拦截、failed.json 并发去重、
空词表正则退化、AI 解析排版、md 加粗转换、表达候选过滤、cards 排版状态机。

运行:  uv run python -m unittest discover -s tests -v
"""
import html
import json
import os
import re
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))


# ---------------- wordforms:词形展开与空输入退化 ----------------

class TestWordForms(unittest.TestCase):
    def test_regular_forms(self):
        from wordforms import word_forms
        fs = word_forms('glance')
        self.assertIn('glance', fs)
        self.assertIn('glancing', fs)      # 去 e
        self.assertIn('glanced', fs)
        self.assertGreaterEqual(fs, {'glance', 'glancing', 'glances'})

    def test_irregular_forms(self):
        from wordforms import word_forms
        fs = word_forms('make')
        self.assertIn('made', fs)
        self.assertIn('making', fs)
        fs2 = word_forms('creep')
        self.assertIn('crept', fs2)

    def test_token_regex_empty_returns_none(self):
        from wordforms import token_regex
        self.assertIsNone(token_regex([]))          # 历史 bug:空正则全文乱标

    def test_token_regex_matches_inflected(self):
        from wordforms import token_regex
        pat = token_regex(['make'])
        self.assertIsNotNone(pat)
        self.assertEqual(sorted(pat.findall('He made a box. They are making tea.')),
                         ['made', 'making'])

    def test_phrase_regex_empty_returns_none(self):
        from wordforms import phrase_regex
        self.assertIsNone(phrase_regex(''))
        self.assertIsNone(phrase_regex('   '))

    def test_phrase_regex_surface_expansion(self):
        from wordforms import phrase_regex
        # 表面形展开:take -> taken/took(不规则表),off 原样 —— 例证句即原句可命中
        pat = phrase_regex('take off')
        self.assertIsNotNone(pat)
        self.assertTrue(pat.search('he took off his coat'))
        self.assertTrue(pat.search('take off your shoes'))
        # 表面形局限:made 展开不出 making(文档已如实说明,不做跨词换形)
        pat2 = phrase_regex('made her plans')
        self.assertFalse(pat2.search('making her plans'))


# ---------------- hard_words:难度判定 ----------------

class TestHardWords(unittest.TestCase):
    def test_is_harder(self):
        from hard_words import is_harder
        self.assertTrue(is_harder('c1', 'b2', 0))        # 级别更高 → 更难
        self.assertFalse(is_harder('b1', 'b2', 0))       # 级别更低 → 不标
        self.assertFalse(is_harder('b2', 'b2', 0))       # 同级 → 不标
        self.assertFalse(is_harder('c1', 'toe', 0))      # 目标已到顶 → 不标
        self.assertTrue(is_harder('toe', 'b2', 9000))    # 目标 b2:toe 无条件更难
        self.assertFalse(is_harder('toe', 'c1', 3000))   # 目标 c1:toe 需 bnc 低频佐证
        self.assertTrue(is_harder('toe', 'c1', 7000))


# ---------------- ai_explain:容错 JSON / md 加粗 / 失败记录并发 ----------------

class TestAiExplain(unittest.TestCase):
    def test_parse_batch_ok(self):
        from ai_explain import parse_batch
        text = ('{"results": [{"word": "a", "items": [{"seg": "seg", "note": "n"}],'
                ' "reading": "r", "culture": "c", "memo": "钩"}]}')
        out = parse_batch(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['word'], 'a')
        self.assertEqual(out[0]['culture'], 'c')

    def test_parse_batch_md_bold_and_newline(self):
        # 字段内 md 加粗转 <b>、防御性单行化(模型被禁止换行)
        from ai_explain import parse_batch
        text = ('{"results": [{"word": "a", "items": [{"seg": "s", "note": "**重点**词"}],'
                ' "reading": "r1\\nr2", "memo": "m"}]}')
        out = parse_batch(text)
        self.assertEqual(out[0]['items'][0]['note'], '<b>重点</b>词')
        self.assertEqual(out[0]['reading'], 'r1 r2')

    def test_parse_batch_rejects_bad_items(self):
        from ai_explain import parse_batch
        # 缺 note / 含替换符残片 / items 非列表 / 旧自由文本格式的坏项被剔除
        text = ('{"results": ['
                ' {"word": "a", "items": [{"seg": "s", "note": "n"}], "reading": "r", "memo": "m"},'
                ' {"word": "b", "items": [{"seg": "s"}], "reading": "r", "memo": "m"},'
                ' {"word": "c", "items": [{"seg": "s", "note": "�"}], "reading": "r", "memo": "m"},'
                ' {"word": "d", "items": "items", "reading": "r", "memo": "m"},'
                ' {"word": "e", "ai_analysis": "1. 逐项解析…", "memo": "m"}]}')
        out = parse_batch(text)
        self.assertEqual([p['word'] for p in out], ['a'])

    def test_lenient_load(self):
        from ai_explain import _lenient_load
        # 合法 JSON 原样通过
        good = '{"results": [{"word": "a", "ai_analysis": "x", "memo": "m"}]}'
        self.assertEqual(_lenient_load(good)['results'][0]['word'], 'a')
        # 字符串中段裸引号:报错位置在引号之后的字符上,修复分支修不动
        # → 返回 None(调用方走回退/重试,不崩);这是实测的真实契约
        bad = '{"results": [{"word": "a", "ai_analysis": "他说"ok"", "memo": "m"}]}'
        self.assertIsNone(_lenient_load(bad))
        self.assertIsNone(_lenient_load('完全不是 JSON'))

    def test_md_bold_to_html(self):
        from ai_explain import md_bold_to_html as f
        self.assertEqual(f('**x** 和 **yy**'), '<b>x</b> 和 <b>yy</b>')
        self.assertEqual(f('无加粗'), '无加粗')          # 幂等

    def test_failed_save_dedupes_and_atomic(self):
        from ai_explain import _save_failed, _load_failed
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, 'failed.json')
            _save_failed(fp, ['a', 'b'])
            _save_failed(fp, ['b', 'c'])                 # 重复跑:按 word 去重
            loaded = _load_failed(fp)
            self.assertEqual(sorted(p['word'] for p in loaded), ['a', 'b', 'c'])
            self.assertFalse(os.path.exists(fp + '.tmp'))  # 无临时文件残留
        # 损坏文件不崩
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, 'failed.json')
            open(fp, 'w', encoding='utf-8').write('{"半截')
            self.assertEqual(_load_failed(fp), [])

    def test_failed_save_concurrent(self):
        """--workers 4 并发写模拟:无丢失、无重复、无异常"""
        import threading
        from ai_explain import _save_failed, _load_failed
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, 'failed.json')
            def worker(words):
                for w in words:
                    _save_failed(fp, [w])
            threads = [threading.Thread(target=worker, args=(['w%d' % (i * 10 + j)
                                                             for j in range(10)],))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            got = {p['word'] for p in _load_failed(fp)}
            self.assertEqual(got, {'w%d' % i for i in range(40)})


# ---------------- apply_polish:md 加粗 ----------------

class TestApplyPolish(unittest.TestCase):
    def test_md_bold(self):
        from apply_polish import md_bold
        self.assertEqual(md_bold('**b** x'), '<b>b</b> x')
        self.assertEqual(md_bold('plain'), 'plain')


# ---------------- ai_explain:结构化 schema 解析 + 确定性拼装 ----------------

class TestAiExplainSchema(unittest.TestCase):
    STRUCT = ('{"results": [{"word": "delight", '
              '"items": ['
              '{"seg": "Beth ate no more", "note": "谓语部分,写下她停下进食。", '
              ' "words": [{"w": "ate", "note": "eat 的过去式。"},'
              '           {"w": "crept away", "note": "悄悄溜走。"}]},'
              '{"seg": "the delight to come", "note": "名词短语作宾语。"}], '
              '"reading": "整句刻画了贝丝的神态。", '
              '"culture": "出自《小妇人》。", '
              '"memo": "心里捂着一颗快化掉的糖。"}]}')

    def test_parse_batch_valid(self):
        from ai_explain import parse_batch
        out = parse_batch('前缀废话 ' + self.STRUCT + ' ```')
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p['word'], 'delight')
        self.assertEqual(p['items'][0]['seg'], 'Beth ate no more')
        self.assertEqual(p['items'][0]['words'][1]['w'], 'crept away')
        self.assertEqual(p['reading'], '整句刻画了贝丝的神态。')
        self.assertEqual(p['memo'], '心里捂着一颗快化掉的糖。')

    def test_parse_batch_rejects_old_free_text(self):
        # 模型回退旧格式(自由文本 ai_analysis、无 items)→ 判不合格,触发重试
        from ai_explain import parse_batch
        old = ('{"results": [{"word": "x", "ai_analysis": "1. 逐项解析\\n…", '
               '"memo": "钩子"}]}')
        self.assertEqual(parse_batch(old), [])
        self.assertEqual(parse_batch(''), [])
        self.assertEqual(parse_batch('不是 JSON'), [])

    def test_parse_batch_rejects_missing_fields(self):
        from ai_explain import parse_batch
        bad = ('{"results": [{"word": "x", "items": [{"seg": "", "note": "n"}], '
               '"reading": "r", "memo": "m"},'
               '{"word": "y", "items": [], "reading": "r", "memo": "m"}]}')
        self.assertEqual(parse_batch(bad), [])   # seg 空 / items 空均不合格

    def test_compose_analysis_layout(self):
        from ai_explain import parse_batch, compose_analysis
        p = parse_batch(self.STRUCT)[0]

        def tag_fn(w):   # 注入身份判定,单测不依赖词典资源
            return '目标词' if w == 'ate' else '超纲词'

        out = compose_analysis(p['items'], p['reading'], p['culture'], tag_fn)
        self.assertTrue(out.startswith('1. 逐项解析\n'))
        self.assertIn('• <b>Beth ate no more</b>:谓语部分,写下她停下进食。', out)
        self.assertIn('– <b>ate</b>(目标词):eat 的过去式。', out)
        self.assertIn('– <b>crept away</b>(超纲词):悄悄溜走。', out)
        self.assertIn('\n2. 整句解读\n整句刻画了贝丝的神态。', out)
        self.assertIn('\n3. 文化点\n出自《小妇人》。', out)
        # culture 为空 → 整段省略
        out2 = compose_analysis(p['items'], p['reading'], '', tag_fn)
        self.assertNotIn('文化点', out2)


# ---------------- cards:TSV 消毒 / 高亮 / AI 解析排版 / sources 幂等 ----------------

class TestCards(unittest.TestCase):
    def test_tsv_cell(self):
        from cards import tsv_cell
        self.assertEqual(tsv_cell('a\tb\nc'), 'a b c')   # 制表符/换行消毒(保 10 列)
        self.assertEqual(tsv_cell(None), '')

    def test_hl_sentence(self):
        from cards import hl_sentence
        esc = html.escape('He slipped away.')
        out = hl_sentence(esc, 'slip')
        self.assertEqual(out, 'He <b class="hl">slipped</b> away.')
        self.assertEqual(hl_sentence('', 'slip'), '')    # 空句安全
        self.assertEqual(hl_sentence('x', ''), 'x')      # 空词安全

    def test_hl_phrase(self):
        from cards import hl_phrase
        esc = html.escape('Take off your coat.')
        out = hl_phrase(esc, 'take off')
        self.assertEqual(out, '<b class="hl">Take off</b> your coat.')
        self.assertEqual(hl_phrase('', 'take off'), '')

    def test_ai_cell_html_layout(self):
        from cards import ai_cell_html
        v = ('1. 逐项解析\n1. a is good\n- b bad\n2. 整句解读\nok\n')
        out = ai_cell_html(v)
        self.assertIn('<b>1. 逐项解析</b>', out)
        self.assertIn('• a is good', out)
        self.assertIn('– b bad', out)
        self.assertIn('<br>', out)
        # 无换行输入的幂等
        self.assertEqual(ai_cell_html('plain'), 'plain')
        self.assertEqual(ai_cell_html(''), '')

    def test_ai_cell_html_whitelist(self):
        from cards import ai_cell_html
        # 白名单只放行 <b>(ai_explain 已把 **x** 转为 <b>x</b>),其余尖括号保持转义(防模型注入)
        out = ai_cell_html('<script>x</script> <b>b</b>')
        self.assertNotIn('<script>', out)
        self.assertIn('<b>b</b>', out)

    def test_ai_cell_html_circled_numbers(self):
        # 2026-08-31 回归:decidedly 卡实测格式——①②③ 圆圈号条目 → 统一收敛为 • 条目
        from cards import ai_cell_html
        v = ('1. 逐项解析\n① “I shall get”: 主句主干\n② “;”: 分号\n'
             '2. 整句解读\n整句读来一本正经\n')
        out = ai_cell_html(v)
        self.assertNotIn('①', out)
        self.assertIn('• <b>“I shall get”</b>:主句主干', out)      # 行首成分自动加粗
        self.assertIn('<br><br>• ', out)                            # 条目独立成段
        self.assertIn('<br><br><b>2. 整句解读</b>', out)            # 段首行前插空行

    def test_ai_cell_html_inline_section_markers(self):
        # 2026-08-31 回归:comfort 卡实测格式——无段首行,段落用 “# 整句解读#” 拼进行内,
        # 条目分隔用 “——” → 三段重建 + 分隔符归一为冒号
        from cards import ai_cell_html
        v = ('1. **Tell them** —— tell 是祈使句动词\n'
             '2. **at all times** —— 固定短语。'
             '# 整句解读# 整句开头是祈使句。# 文化点# 本句出自《小妇人》。\n')
        out = ai_cell_html(v)
        self.assertNotIn('#', out.replace('&#x27;', ''))            # 行内段标记拆除
        self.assertIn('<b>1. 逐项解析</b>', out)                    # 悬空条目自动补段首行
        self.assertIn('• <b>Tell them</b>:tell 是祈使句动词', out)  # —— 归一为冒号
        self.assertIn('<b>2. 整句解读</b>', out)
        self.assertIn('<b>3. 文化点</b>', out)

    def test_ai_cell_html_two_level_numbering(self):
        # 2026-08-31 回归:delight 卡实测格式——1.1 两级编号条目 → • 条目(与 1. 同权)
        from cards import ai_cell_html
        v = '1. 逐项解析:\n1.1 **Beth**:人名\n1.2 **but**:转折连词\n\n2. 整句解读:\nok\n'
        out = ai_cell_html(v)
        self.assertNotIn('1.1', out)
        self.assertIn('• <b>Beth</b>:人名', out)
        self.assertIn('• <b>but</b>:转折连词', out)

    def test_ai_cell_html_word_tag_colors(self):
        # 身份标记圈点上色:(目标词)=例句高亮红,(超纲词)=超纲绿;词级行词名自动加粗
        from cards import ai_cell_html
        v = '1. 逐项解析\n1. crept away(超纲词):悄悄溜走\n- rosy(超纲词):玫瑰色的\n'
        out = ai_cell_html(v)
        self.assertIn('<b class="hard">(超纲词)</b>', out)
        self.assertIn('– <b>rosy</b><b class="hard">(超纲词)</b>:玫瑰色的', out)
        v2 = '1. 逐项解析\n1. delight(目标词):本卡的词\n'
        self.assertIn('<b class="hl">(目标词)</b>', ai_cell_html(v2))

    def test_ai_cell_html_bullets_only_section(self):
        # 逐项解析全区只有圆点行时,圆点就是条目(而非词级拆解),收敛为 •
        from cards import ai_cell_html
        v = '1. 逐项解析\n- Beth 人名\n- but 转折连词\n2. 整句解读\nok\n'
        out = ai_cell_html(v)
        self.assertIn('• Beth 人名', out)
        self.assertIn('• but 转折连词', out)
        self.assertNotIn('– ', out)

    def test_ai_cell_html_literal_newline_escape(self):
        # 2026-08-31 回归:hush 卡实测——模型把换行双转义成字面 \n,整个结构解析失效
        from cards import ai_cell_html
        v = '引子。\\n\\n1. 逐项解析\\n\\n① A hush:一阵寂静\\n② fell over:降临\\n\\n2. 整句解读\\n整句有画面。'
        out = ai_cell_html(v)
        self.assertIn('<b>1. 逐项解析</b>', out)
        self.assertIn('<b>2. 整句解读</b>', out)
        self.assertIn('• <b>A hush</b>:一阵寂静', out)

    def test_ai_cell_html_quoted_section_marks(self):
        # 2026-08-31 回归:refuge/defend 卡实测——段首行被「」包住(「逐项解析」)
        from cards import ai_cell_html
        v = '「逐项解析」\n1. The little arbour:主语部分\n2. was:系动词\n\n「整句解读」\n读来轻松惬意\n\n「文化点」\n19 世纪花园场景\n'
        out = ai_cell_html(v)
        self.assertIn('<b>1. 逐项解析</b>', out)
        self.assertIn('<b>2. 整句解读</b>', out)
        self.assertIn('<b>3. 文化点</b>', out)

    def test_ai_cell_html_section_annotation_and_alias(self):
        # 2026-08-31 回归:splendid/anxiety 卡实测——段名带 (注释) 后缀、【N. 别名段名】,
        # 别名段名(例句逐词解析)按规范段名重编号
        from cards import ai_cell_html
        v = '1. 逐项解析（把例句拆成零件逐一讲解）\n1. Mother:专有名词\n\n2. 整句解读（讲透）\n骨架清爽\n'
        out = ai_cell_html(v)
        self.assertIn('<b>1. 逐项解析</b>', out)
        self.assertNotIn('逐项解析（', out)
        self.assertIn('<b>2. 整句解读</b>', out)
        v2 = '【1. 目标词详解】\n词义先讲透。\n\n【2. 例句逐词解析】\n1. Language:主语\n\n【3. 整句解读】\n夸张句式\n\n【4. 文化点】\n背景补充\n'
        out2 = ai_cell_html(v2)
        self.assertEqual(re.findall(r'<b>\d\. (?:逐项解析|整句解读|文化点)</b>', out2),
                         ['<b>1. 逐项解析</b>', '<b>2. 整句解读</b>', '<b>3. 文化点</b>'])

    def test_ai_cell_html_unlabeled_trailing_paragraph(self):
        # 2026-08-31 回归:cheek/forehead 卡实测——条目后跟着漏写段首行的整句解读长段 → 自动补
        from cards import ai_cell_html
        v = '1. Jo:人名,女主角\n2. read also:并列动作\n\n整句骨架是先动作再神态,画面非常亲昵自然,读起来前段紧凑。'
        out = ai_cell_html(v)
        self.assertIn('<b>1. 逐项解析</b>', out)
        self.assertIn('<b>2. 整句解读</b>', out)
        self.assertLess(out.index('• <b>read also</b>'), out.index('<b>2. 整句解读</b>'))
        # 短尾行/无空行隔开的不补(保守,宁可漏)
        v2 = '1. a:成分一\n2. b:成分二\n尾随小注。'
        self.assertNotIn('<b>2. 整句解读</b>', ai_cell_html(v2))

    def test_ai_cell_html_single_line_analysis(self):
        # 2026-08-31 回归:ch47 实测——三段挤成一行,条目用分号连排 → 切段 + 拆条目
        from cards import ai_cell_html
        v = ('1. 逐项解析：Grasshoppers 蚱蜢（超纲词）；skipped 蹦跳（skip 的过去式）；'
             'briskly 轻快地；crickets 蟋蟀（目标词 cricket 的复数）；chirped 啾啾叫。'
             '2. 整句解读：全句是一幅静中有动的秋日写景,一动一静互相衬托。'
             '3. 文化点：蟋蟀是田园宁静的象征。')
        out = ai_cell_html(v)
        self.assertEqual(re.findall(r'<b>\d\. (?:逐项解析|整句解读|文化点)</b>', out),
                         ['<b>1. 逐项解析</b>', '<b>2. 整句解读</b>', '<b>3. 文化点</b>'])
        self.assertIn('• Grasshoppers 蚱蜢<b class="hard">(超纲词)</b>', out)
        self.assertIn('• crickets 蟋蟀（目标词 cricket 的复数）', out)
        self.assertNotIn('；', out)   # 分号连排已拆开

    def test_ai_cell_html_circled_paragraphs_in_culture(self):
        # 2026-08-31 回归:agreeable/envy 卡实测——文化点里 ①②③ 枚举段 → 圆点列表
        from cards import ai_cell_html
        v = '1. 逐项解析\n1. a:成分\n2. 整句解读\n段落。\n3. 文化点\n① 本句出自《小妇人》,背景是十九世纪的美国。\n② 桑丘出自《堂吉诃德》。\n'
        out = ai_cell_html(v)
        self.assertNotIn('①', out)
        self.assertIn('• 本句出自《小妇人》,背景是十九世纪的美国。', out)
        self.assertIn('• 桑丘出自《堂吉诃德》。', out)

    def test_src_key_idempotent(self):
        from cards import _src_key
        e = {'sources': 'little_women|ch5|18;little_women|ch9|7'}
        self.assertTrue(_src_key(e, 'little_women|ch5'))
        self.assertTrue(_src_key(e, 'little_women|ch9'))
        self.assertFalse(_src_key(e, 'little_women|ch3'))
        self.assertFalse(_src_key({'sources': ''}, 'book|ch1'))


# ---------------- validate:known 对账口径(P2 回归) ----------------

class TestValidateAlignment(unittest.TestCase):
    def test_card_expected_excludes_known(self):
        from validate import _card_expected
        rows = [
            {'word': 'alpha', 'cn_mean': '释义一'},
            {'word': 'beta', 'cn_mean': ''},
            {'word': 'gamma', 'cn_mean': '释义二'},
        ]
        # known 词已被 cards 从 TSV 过滤,期望卡数必须同口径排除
        self.assertEqual(_card_expected(rows, {'gamma'}), 1)
        self.assertEqual(_card_expected(rows, set()), 2)
        self.assertEqual(_card_expected(rows, {'alpha', 'gamma'}), 0)

    def test_card_expected_case_insensitive(self):
        from validate import _card_expected
        rows = [{'word': 'Alpha', 'cn_mean': 'x'}]
        self.assertEqual(_card_expected(rows, {'alpha'}), 0)


# ---------------- annotate:空词表不高亮 ----------------

class TestAnnotate(unittest.TestCase):
    def test_build_marks_empty(self):
        from annotate import build_marks, highlight
        self.assertIsNone(build_marks([]))
        self.assertEqual(highlight('The cat sat.', None), 'The cat sat.')

    def test_build_marks_words(self):
        from annotate import build_marks, highlight
        pat = build_marks([{'word': 'slip'}])
        self.assertIsNotNone(pat)
        self.assertIn('**slipped**', highlight('He slipped away.', pat))


# ---------------- pipeline:表达候选块切分与过滤 ----------------

class _FakeDB:
    """ECDICT 查询桩:未知词一律 toe"""
    def get(self, w):
        return (None, None, '', 0, 0)


class TestPipelinePhrases(unittest.TestCase):
    def setUp(self):
        import pipeline
        self.pipeline = pipeline
        # oxford:词 -> (词性, 级别)
        self.oxford = {
            'take': ('v', 'b1'), 'off': ('ad', 'a2'), 'in': ('ad', 'a2'),
            'an': ('art', 'a1'), 'altered': ('adj', 'b2'), 'tone': ('n', 'b1'),
            'she': ('pron', 'a1'), 'was': ('v', 'a1'),
        }
        self.db = _FakeDB()

    def test_chapter_chunks_lengths_and_filters(self):
        # 标点(;)制造语块边界,让 take off 成为 2 词块
        sents = ['Take off; your coat, in an altered tone.',
                 'She was happy, no doubt.']
        keys = list(self.pipeline.chapter_chunks(sents, self.oxford, self.db))
        keyset = {k for k, _ in keys}
        self.assertIn('take off', keyset)            # 动词短语保留
        # 前置介词跟在标点后 → 标点把介词切为边界词,块保留实词核心(真实切块语义)
        self.assertIn('an altered tone', keyset)
        self.assertNotIn('she was', keyset)          # 代词开头残片滤掉
        self.assertNotIn('your coat', keyset)        # 代词开头残片滤掉

    def test_extract_phrases_book_frequency(self):
        sents = ['Take off; your coat is here.',
                 'A single line appears now.']
        out = self.pipeline.extract_phrases(sents, self.oxford, self.db,
                                            {'take off': 5}, top=10)
        words = [c['phrase'] for c in out]
        self.assertIn('take off', words)             # 动词短语单次豁免
        self.assertNotIn('a single line', words)     # 仅 1 次且非动词短语

    def test_extract_phrases_book_seen_skip(self):
        sents = ['Take off; your coat is here.']
        seen = {'take off'}
        out = self.pipeline.extract_phrases(sents, self.oxford, self.db,
                                            {'take off': 5}, top=10, seen=seen)
        self.assertNotIn('take off', [c['phrase'] for c in out])


# ---------------- proper_names:按书专名表(2026-08-31 机制化) ----------------

class TestProperNames(unittest.TestCase):
    def test_load_parses_comments_blanks_case(self):
        import shutil
        import proper_names
        old_dir = proper_names.PROPER_DIR
        proper_names.PROPER_DIR = tempfile.mkdtemp()
        try:
            with open(os.path.join(proper_names.PROPER_DIR, 'ut_book.txt'),
                      'w', encoding='utf-8') as f:
                f.write('# 头注释\njudy  # 人名\n\n Jerusha \n')
            proper_names._cache.pop('ut_book', None)
            self.assertEqual(proper_names.load('ut_book'),
                             frozenset({'judy', 'jerusha'}))
            self.assertEqual(proper_names.load('ut_book_absent'), frozenset())
        finally:
            shutil.rmtree(proper_names.PROPER_DIR, ignore_errors=True)
            proper_names.PROPER_DIR = old_dir
            proper_names._cache.pop('ut_book', None)

    def test_suspects_flags_always_capped_names(self):
        import proper_names
        md = ('# T\n\n**Chapter 1 Test**\n\n'
              + 'Jerusha walked to the store. The store was closed. ' * 5)
        sus = {s['word'] for s in proper_names.suspects(
            md, {'store': ('noun', 'a2')})}
        self.assertIn('jerusha', sus)       # 常作句首主语的人名(cap 占比 100%)
        self.assertNotIn('store', sus)      # 词表内不判
        self.assertNotIn('walk', sus)       # 小写普通词不判


class TestHardWordsProperThread(unittest.TestCase):
    def test_diff_proper_excludes_names(self):
        from hard_words import hard_words_in

        class FakeDiff:
            proper = {'jervie'}
            def level_of(self, w):
                return {'mansion': 'c1', 'pony': 'c1'}.get(w, 'a1')
            def bnc_of(self, w):
                return 9000

        d = FakeDiff()
        hard = hard_words_in('Jervie kept his mansion and a pony.', 'kept', 'b2', d)
        self.assertEqual(hard, ['mansion', 'pony'])   # 句首大写人名靠 diff.proper 拦下


class TestAiPickProper(unittest.TestCase):
    def test_parse_items_filters_to_wanted(self):
        from ai_pick_proper import parse_items
        raw = ('说明 {"items":[{"word":"judy","proper":true,"type":"人物"},'
               '{"word":"embarrass","proper":false},{"word":"alien","proper":true}]} 尾注')
        got = parse_items(raw, {'judy', 'embarrass'})
        self.assertEqual(set(got), {'judy', 'embarrass'})   # 越权词丢弃
        self.assertTrue(got['judy']['proper'])

    def test_parse_items_survives_garbage(self):
        from ai_pick_proper import parse_items
        self.assertEqual(parse_items('', {'x'}), {})
        self.assertEqual(parse_items('模型拒答没有 JSON', {'x'}), {})

    def test_build_user_prompt_keeps_sentences(self):
        from ai_pick_proper import build_user_prompt
        batch = [{'word': 'judy', 'freq': 79, 'cap': 79,
                  'contexts': ['Judy is here today.', 'Judy sings badly.']}]
        p = build_user_prompt(batch)
        self.assertIn('judy', p)
        self.assertIn('Judy is here today.', p)      # 整句上下文(曾误切片成单字符)


class TestEpubToMd(unittest.TestCase):
    MD = ('# BOOK\n\n## \\* \\* \\*\n\n## JEAN WEBSTER\n\n# Contents\n\n## \\*\n\n'
          '*[A](x.html)*\n\n# Blue Wednesday\n\nIt was a perfect day.\n\n'
          '# The Letters\n\n### 24th September\n\nDear Sir,\n\nHere I am!\n\n'
          '### 1st October\n\nCold today.\n')

    def test_parse_units_drops_decor_keeps_levels(self):
        from epub_to_md import parse_units
        tops = parse_units(self.MD)
        self.assertEqual([u['title'] for u in tops],
                         ['BOOK', 'Contents', 'Blue Wednesday', 'The Letters'])
        self.assertEqual(tops[0]['subs'][0]['title'], 'JEAN WEBSTER')  # `## \* \* \*` 装饰被丢
        self.assertEqual(tops[1]['subs'], [])                          # Contents 下无碎节
        self.assertEqual(tops[3]['subs'][0]['title'], '24th September')
        self.assertEqual(tops[3]['subs'][1]['paras'], ['Cold today.'])

    def test_build_chapters_groups_by_budget(self):
        from epub_to_md import build_chapters
        tops = [{'level': 1, 'title': 'Letters', 'paras': [], 'subs': [
            {'level': 3, 'title': 'L1', 'paras': ['one two'], 'subs': []},
            {'level': 3, 'title': 'L2', 'paras': ['three four'], 'subs': []},
            {'level': 3, 'title': 'L3', 'paras': ['five six'], 'subs': []},
        ]}]
        chs = build_chapters(tops, target=3, maxt=5)
        # L2 并入 L1(2+3=5 未超 maxt),L3 时累计词数 ≥ target 封章
        self.assertEqual([c['title'] for c in chs], ['L1', 'L3'])
        self.assertIn('L2', ' '.join(chs[0]['paras']))


if __name__ == '__main__':
    unittest.main()