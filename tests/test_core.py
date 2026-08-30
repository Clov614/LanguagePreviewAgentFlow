"""核心纯函数单测(无需网络/模型/词典资源)。
覆盖 2026-08-30 审查整改的关键修复:known 拦截、failed.json 并发去重、
空词表正则退化、AI 解析排版、md 加粗转换、表达候选过滤、cards 排版状态机。

运行:  uv run python -m unittest discover -s tests -v
"""
import html
import json
import os
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
    def test_extract_batch_ok(self):
        from ai_explain import extract_batch
        text = '{"results": [{"word": "a", "ai_analysis": "好", "memo": "钩"}]}'
        out = extract_batch(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['word'], 'a')

    def test_extract_batch_wraps_markdown(self):
        from ai_explain import extract_batch
        text = '{"results": [{"word": "a", "ai_analysis": "**重点**词", "memo": "m"}]}'
        out = extract_batch(text)
        self.assertEqual(out[0]['ai_analysis'], '<b>重点</b>词')

    def test_extract_batch_rejects_filler(self):
        from ai_explain import extract_batch
        # 缺字段 / 含替换符残片的坏项被剔除
        text = ('{"results": [{"word": "a", "ai_analysis": "x", "memo": "y"},'
                ' {"word": "b", "ai_analysis": "�", "memo": "z"},'
                ' {"word": "c", "ai_analysis": "x"}]}')
        out = extract_batch(text)
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


if __name__ == '__main__':
    unittest.main()