# 语言预习工作流 — AI Agent 操作手册

> 供 AI Agent / 自动化脚本阅读的权威操作文档。**人类用户请看 `README.md`。**
> 有任何改动需求,先读完本文件,再对照 `scripts/` 实际代码确认参数。

## 项目一句话

把英语原著 EPUB 转化为"先提取生词预习、建立短期记忆,再在阅读中识别"的学习产物:
每章 15–20 个生词,卡片 = 单词 + 音标 + 中文释义 + CEFR + **书中原句** + 译文 + 来源,
可预生成英音发音(mp3 内嵌 [sound:],导入即听)。

## 数据流与目录

```
data/books/<book>.epub           用户放入的原著
data/books/_md/<book>.md         转换缓存(pipeline 的输入)
data/output/<book>/
  raw/chapter_XX_raw.csv         候选词全量数据(UTF-8 带 BOM,18 列,Excel 可开)
  anki/chapter_XX_anki.tsv       导入卡(UTF-8 无 BOM,8 列,每章 18 词)
  anki/audio/                    发音 mp3 缓存(可再生成,**不入 git**)
  annotated/chapter_XX.md        生词高亮标注版(读书用)
  annotated/chapter_XX_词表.md   章节词表速查
  work/                          polish_*.json 润色工作单(模型产出,应用后留存)
  meta.json / recommend_report.md
vocabulary/
  master_wordlist.csv            生词总库:跨书去重、幂等累积(12 列,无 BOM)
  known_words.txt                已掌握词(带 # 注释行);known 词绝不重复推荐
resources/ecdict.db              本地词典(sqlite,MIT,不入 git)
resources/anki/anki_template.apkg  EnWords 笔记模板包
```

## 运行约定(全部在仓库根目录)

- **统一入口(推荐)**: `uv run python scripts/run.py --book <书名>`
  - 阶段顺序:`pipeline → apply → [audio] → cards → annotate → report → validate`
  - 常用:`--polish <json>`(apply 输入)、`--ai-en <json>`、`--audio`(cards 前生成发音)、
    `--from-chapter N`(pipeline 断点续跑)、`--chapter N`(apply/audio/cards/annotate 单章)、
    `--voice <短名> --force-audio`(换发音人,默认 en-GB-SoniaNeural)、`--stage <阶段>`、`--verbose`
  - 任一阶段非零退出 → 管线终止;apply 无输入时仅提示并跳过(不失败)
- **单脚本直调**: `uv run python scripts/<x>.py --book <书名> [--chapter N]`,参数见各自 `-h`
- Windows 11 / macOS 均用 `uv run`;无需手动激活 venv
- 依赖:项目依赖在 `pyproject.toml`(`edge-tts` 用于发音;genanki 只在一键模板脚本里 `--with` 注入)

## 硬性不变式(违反前必须停下问用户)

1. **编码**:anki TSV = UTF-8 **无 BOM**;raw CSV = UTF-8 **带 BOM**;总库 = UTF-8 无 BOM。
2. **TSV 严格 8 列**:`单词|音标|词性|中文释义|CEFR|原文例句|例句译文|来源`。`validate.py` 硬校验;
   发音以 `[sound:]` **内嵌在「单词」/「原文例句」格里**,不得加列;
   「原文例句」格里目标词包裹 `<b class="hl">…</b>` 高亮(HTML 转义后包裹,
   含屈折形态,见 `scripts/wordforms.py`),仍属 6 列内容不变式;Anki 导入需勾选"允许 HTML"。
3. **总库合并幂等**:同 (书, 章) 重复跑不重复累加 sources/freq_book;`known_words.txt` 的词不推荐不出卡。
4. **不静默覆盖**:pipeline 输出与总库/历史数据矛盾时,停下向用户说明再处理。
5. **质量优先**:例句/释义宁少勿滥;AI 补句的词必须在「来源」标注 `(AI 补句)`(raw csv `ai=1` 列)。
6. **音频**:mp3 只由 `gen_audio.py` 生成(幂等:已有文件跳过,`--force` 重生成),输出 `anki/audio/`
   并自动拷入 Anki `collection.media`;该目录不入 git。
7. **git**:不主动提交,除非用户明确要求。

## 发音模块约定(gen_audio.py / cards.py / tts_paths.py)

- `tts_paths.py` 是共享命名约定(不依赖 edge-tts):
  - 单词(跨章去重): `w_<sanitize>_<sha1前8>.mp3`
  - 例句(按 章+词):  `s_ch<NN>_<sanitize>_<sha1前8>.mp3`
  - `sanitize` 把非 `[A-Za-z0-9_.-]` 字符替换为 `_`;hash 防同形词/大小写碰撞
- cards.py 只对**实际存在**的 mp3 追加 ` [sound:...]`;缺文件则该格不加标签(不报错)
- **自动播放由模板 JS 实现**(make_anki_template.py 的 autoplay_script):qfmt 播第一个 sound(单词),
  afmt 播最后一个(例句,例句无音时回退单词);桌面端点 `.replay-button`、移动端兜底 `audio.play()`,
  失败静默。重导模板 apkg 即对所有卡生效(**无需重导卡片**);要关掉就删模板里两段 `<script>`
- collection.media 自动定位:单 Anki 配置直接拷;多配置需 `--media-dir`;`--no-copy` 只生成
- 用 `--list-voices`(或 `uv run python -c "import asyncio,edge_tts;print(sorted(x['ShortName'] for x in asyncio.run(edge_tts.list_voices()) if x['ShortName'].startswith('en-GB')))"`)查看可用英音
- 语音速查(edge-tts 免费神经语音,当前默认 `en-GB-SoniaNeural` 英音女声):
  - 英音:`en-GB-SoniaNeural`(女·默认)、`en-GB-LibbyNeural`(女·活泼)、`en-GB-MaisieNeural`(少女)、`en-GB-RyanNeural`(男)、`en-GB-ThomasNeural`(男·低沉)
  - 美音(如用户要求时):`en-US-AriaNeural`(女)、`en-US-AndrewNeural`(男)等 17 个
- 换发音人:文件名不含 voice,`--force` 覆盖同名 mp3 即为整库换音,卡片 [sound:] 无需重导
- 全量语音约 322 个(含日/德/西等),未来日语书可复用(`ja-JP-NanamiNeural` 等)

## 常见任务速查

| 任务 | 命令 |
|---|---|
| 新书全流程(已润色) | `uv run python scripts/run.py --book <书名> --polish <json> --audio` |
| 补全书发音(已出卡的书) | `uv run python scripts/gen_audio.py --book <书名>` → `uv run python scripts/cards.py --book <书名>` |
| 只重做某章卡片 | `uv run python scripts/cards.py --book <书名> --chapter 1` |
| 换发音人 | `uv run python scripts/gen_audio.py --book <书名> --voice en-GB-RyanNeural --force` → 重跑 cards |
| 标记已掌握 | 向 `vocabulary/known_words.txt` 追加小写单词(每行一个) |
| 全局健康检查 | `uv run python scripts/validate.py --book <书名> --verbose`(全绿 exit 0) |

## 当前台账(2026-08)

- 已完成书:**little_women** 47 章 / 846 卡 / 总库 854 词 / 发音已全部生成(1693 mp3 已入 collection.media)
- 输出路径均已按 raw/ + anki/ + audio/ 新结构;文档以 `README.md`(人)与本文档(agent)为准

## 坑

- Anki 导入 TSV 时模板必须选 **EnWords**(默认 Basic 会丢列);跨章重复词选"更新现有笔记"
- 例句生词高亮依赖 HTML:导入时必须勾选"允许在字段中使用 HTML";旧卡重导(更新现有笔记)即补上高亮
- 高亮词形由 `scripts/wordforms.py` 生成(规则屈折 + 内置不规则动词表);词典/原文遗留的异常词
  (如 little_women 的 gan,例句里根本没有该词)无法高亮,属数据质量问题,提示用户而非静默处理
- edge-tts 为非官方接口,可能随上游调整而抖动:升级依赖前先看 `pyproject.toml` 中版本约束
- validate.py 会因 TSV 列数/行数不齐而 exit 1 —— 改 cards.py 输出结构前先读它的断言