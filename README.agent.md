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
  raw/chapter_XX_raw.csv         候选词全量数据(UTF-8 带 BOM,Excel 可开)
  raw/chapter_XX_phrase_raw.csv  表达卡数据(apply --phrases 产物,同表头;可选)
  anki/chapter_XX_anki.tsv       导入卡(UTF-8 无 BOM,10 列,每章 18 词)
  anki/audio/                    发音 mp3 缓存(可再生成,**不入 git**)
  annotated/chapter_XX.md        生词高亮标注版(读书用)
  annotated/chapter_XX_词表.md   章节词表速查
  work/                          polish_*.json 润色工作单、ai_explain_*.json AI 解析产物
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
2. **TSV 严格 10 列**(2026-08-30 用户确认由 8 列扩列):
   `单词|音标|词性|中文释义|CEFR|原文例句|例句译文|来源|AI解析|词义概述`。`validate.py` 硬校验;
   发音以 `[sound:]` **内嵌在「单词」/「原文例句」格里**,不得加列;
   「原文例句」格里目标词包裹 `<b class="hl">…</b>` 高亮,比目标词更难的「超纲词」包裹
   `<b class="hard">…</b>`(每句 ≤2,本地规则,见 `scripts/hard_words.py`;HTML 转义后包裹,
   含屈折形态,见 `scripts/wordforms.py`);「AI解析」格换行以 `<br>` 表示(TSV 单行保持);
   AI解析/词义概述为**可选内容**(可空,模板条件渲染);Anki 导入需勾选"允许 HTML"。
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

## AI 解析模块(ai_explain.py,可选旁路)

- 对 raw CSV 中已润色词批量生成 `ai_analysis`(四段式整句解析:逐项列表 → 整句解读 → 文化点)
  与 `memo`(画面感词义钩子);**缺 key/不想用时管线照跑不出解析,不阻塞**
- 三接入点(`--provider`):`claude-cli`(**默认**,本机 Claude Code `claude -p` headless,零配置零 key)/
  `anthropic`(`ANTHROPIC_API_KEY`)/ `openai`(任意 OpenAI 兼容端点,`OPENAI_BASE_URL`+`OPENAI_API_KEY`)
- **批处理 `--batch-size`(默认 6 词/批)**:一次调用解析多个词,冷启动/请求开销均摊;
  批内个别词失败自动回退单词 prompt 重试;**多章并发 `--workers`(默认 4)**:各章产物文件
  独立、天然无冲突,可并行跑;`--workers 1` 关闭并发
- 环境变量(全可选;key 只读环境变量不落盘,`--api-key/--base-url/--model` 可覆盖):
  - `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`(anthropic provider)
  - `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`(openai;模型示例 gpt-4o-mini /
    deepseek-chat / qwen3)
  - BASE_URL 模板:DeepSeek `https://api.deepseek.com/v1` /
    Gemini `https://generativelanguage.googleapis.com/v1beta/openai/`(key=GEMINI_API_KEY)/
    Ollama `http://localhost:11434/v1`(本地免 key,留空即可)/ LM Studio `http://localhost:1234/v1`
- `--dry-run` 只预览批次规划不调用任何模型;`--limit N` 每章限 N 词试跑;失败词自动重试
  (≤3 次)后记入 `ai_explain_<book>_failed.json`(`--verbose` 强制重试)
- 产物 `work/ai_explain_<book>_ch<NN>.json`(**可手改**,改完重跑 apply 即生效)/
  `ai_explain_<book>_failed.json`;断点:已生成词自动跳过
- **Markdown 加粗转换**:模型输出里的 `**x**`(Anki 不渲染 Markdown,星号裸露)由
  ai_explain/apply_polish 统一转 `<b>x</b>`,cards.py 转义后仅白名单还原该标签为真加粗,
  其余 HTML 一律转义(防注入);手改 JSON 时直接写 `<b>` 或 `**` 均可
- **解析排版约定**(prompt 督导 + cards.py 渲染收敛双保险):ai_analysis 段首固定
  `1. 逐项解析 / 2. 整句解读 / 3. 文化点`;逐项解析内成分条目行首 `N. ` 编号,**条目内拆词
  讲解另起一行、行首 `- `**,被讲解词统一标注身份——词名后括号 `(目标词)` 或 `(超纲词)`,
  普通词不标;cards.py 渲染把条目收敛为 `• `、词级收敛为 `– `、换行 → `<br>`
  (模型输出不规范也可兜底,手改 JSON 按此格式最省心)
- 接入管线:`uv run python scripts/ai_explain.py --book <b> --workers 4 --batch-size 6` →
  `uv run python scripts/apply_polish.py --book <b> --explain <产物 json>` → `cards.py`(10 列出卡)

## 表达收录(Phase 2,ai_pick_phrases.py 可选旁路)

把"值得整块背的 2-4 词表达"(take off / in an altered tone / made her plans)也做成卡片,
与每章单词卡同 TSV、**排最前**(先刷表达再刷单词)。

**链路**:`pipeline --phrases`(候选)→ `ai_pick_phrases.py`(AI 精选+释义)→
`apply_polish.py --phrases`(合并成表达 raw)→ `cards.py`(同章 TSV 表达排前)

- **候选生成**:`uv run python scripts/pipeline.py --book <b> --phrases`(只写
  `work/phrase_cands_<b>.json`,不动 raw,可反复跑)。语块切分:感叹/应答词、说话动词、
  从句连词当边界;块长 2-4;**全书出现 ≥2 次**(PLAN 12.2.1:无重复即无搭配价值);
  代词/be 动词/介词开头 2 词的残片、专名、撇号/连字符组合滤掉;跨章去重(同一表达只在
  最早出现的章节推荐);打分排序(动词短语 +2 · C1/toe +1.5 · B2 +1 · B1 +0.5),top40。
  入池量不足 40 是常态(little_women 平均每章约 4 条),候选这头宁缺毋滥即可
- **AI 精选**:`uv run python scripts/ai_pick_phrases.py --book <b> [--chapter N] [--pick-min 1 --pick-max 4]`
  - provider/并发/断点/失败机制与 ai_explain 同(`--dry-run` / `--workers` / 已产物章跳过 /
    `phrases_picked_<b>_failed.json`;**记入 failed 也可能是因为 AI 判定该章候选无值得收藏的**
     (不是网络失败,`--verbose` 可强试);**数量纪律:每章硬上限 `--pick-max`(默认 4)——
    每章单词卡 18 张,表达卡必须明显更少才突出重点**(用户 2026-08-30 拍板:放宽不意味着
    不限制,章与章之间也要均衡);`--pick-min` 默认 1(可 0=允许一章全不挑)防模型空手而归;
    候选不足的章自然少挑(宁缺毋滥)
  - 产物 `work/phrases_picked_<b>_ch<NN>.json`(**可手改**,改完重跑 apply 即生效):
    `[{phrase, cn_mean, cn_sent, ai_analysis, memo}]`;模型输出不在候选清单里的短语一律丢弃
  - AI 承诺:cn_mean 中文释义、cn_sent 例证句完整译文、ai_analysis 2-4 句"为什么值得背+场合"、
    memo 画面感钩子;引号铁律与 markdown 加粗转换同 ai_explain
- **合并出卡**:`uv run python scripts/apply_polish.py --book <b> --phrases 'work/phrases_picked_*.json'`
  → 生成/重写 `raw/chapter_XX_phrase_raw.csv`(与单词 raw 同 20 列表头;word=表达短语,
  pos=`phrase`,**音标留空**,CEFR=pipeline 算的**块内最高级**,例证句/频次从候选补)→
  `uv run python scripts/cards.py --book <b>`(表达卡排同章 TSV 最前)
  统一入口: `uv run python scripts/run.py --book <b> --phrases`(只出候选)/ 
  `--phrases --phrase-picked 'work/phrases_picked_*.json'`(候选→合并→cards→annotate→report→validate)
- **表达卡渲染约定**:多词短语**无词级音频**;例句整短语高亮 `<b class="hl">`(按候选
  表面形逐词展开规则屈折匹配,见 wordforms.phrase_regex —— 例证句即原句,表面形即可命中,
  不做跨词换形如 made→making);不标超纲词 hard;句级音频照常查缓存
  (未预生成则不带 [sound:],不报错);标注版 annotated 不含表达(按词高亮,短语不参与)
- **发音**:gen_audio.py 与 cards.py 同口径——表达行只生成**例证句音频**(s_ch<NN>_<短语>.mp3,
  按 tts_paths.sent_audio_name 命名,可跨章复用缓存),不生成词级音频;未预生成则卡片不带
  [sound:],不报错
- **总库**:表达与单词同规则并入 `master_wordlist.csv`(word 列含空格=表达),跨书去重
  (同表达不重复推荐;用户认识后照常记入 known_words.txt);词级频次累积口径与单词卡一致
- **注意**:apply --phrases 与 apply --polish/--explain 互不干扰(处理对象不同文件);
  候选文件过期(重跑 --phrases 换了口径)时,不在候选里的 picked 条目会被 apply 跳过并提示,
  cards 不崩

## 常见任务速查

| 任务 | 命令 |
|---|---|
| 新书全流程(已润色) | `uv run python scripts/run.py --book <书名> --polish <json> --audio` |
| 批量生成 AI 例句解析 | `uv run python scripts/ai_explain.py --book <书名> --workers 4 --batch-size 6` → `uv run python scripts/apply_polish.py --book <书名> --explain work/ai_explain_<书名>_ch01.json` → `uv run python scripts/cards.py --book <书名>` |
| 补全书发音(已出卡的书) | `uv run python scripts/gen_audio.py --book <书名>` → `uv run python scripts/cards.py --book <书名>` |
| 只重做某章卡片 | `uv run python scripts/cards.py --book <书名> --chapter 1` |
| 换发音人 | `uv run python scripts/gen_audio.py --book <书名> --voice en-GB-RyanNeural --force` → 重跑 cards |
| 标记已掌握 | 向 `vocabulary/known_words.txt` 追加小写单词(每行一个) |
| 全局健康检查 | `uv run python scripts/validate.py --book <书名> --verbose`(全绿 exit 0) |
| 清理孤儿词 | `uv run python scripts/validate.py --book <书名> --prune-orphans --yes`(先打印清单,legacy 历史词保留;不加 --yes 交互确认) |

## 当前台账(2026-08)

- 已完成书:**little_women** 47 章 / 846 单词卡 + **94 表达卡**(42 章,每章 1-4 条;
  ch7/ch31 候选无价值未出、ch23/40/45 无候选)/ 总库 956 词 / 发音已全部生成
  (1786 mp3 已入 collection.media;曾被淘汰表达的例证句音频已清理,与 TSV 引用零孤儿)
- Phase 2 表达收录链路已落地:候选提取(平均 3.9 条/章,全书 ≥2 次 + 动词短语单次豁免)→
  AI 精选(可选旁路)→ apply 合并 → cards 出卡(表达排前)→ gen_audio 例证句音频 →
  validate 全绿(exit 0)
- 输出路径均已按 raw/ + anki/ + audio/ 新结构;文档以 `README.md`(人)与本文档(agent)为准

## 坑

- Anki 导入 TSV 时模板必须选 **EnWords**(默认 Basic 会丢列);跨章重复词选"更新现有笔记"
- 例句生词高亮依赖 HTML:导入时必须勾选"允许在字段中使用 HTML";旧卡重导(更新现有笔记)即补上高亮
- 高亮词形由 `scripts/wordforms.py` 生成(规则屈折 + 内置不规则动词表);词典/原文遗留的异常词
  (如 little_women 的 gan,例句里根本没有该词)无法高亮,属数据质量问题,提示用户而非静默处理
- edge-tts 为非官方接口,可能随上游调整而抖动:升级依赖前先看 `pyproject.toml` 中版本约束
- validate.py 会因 TSV 列数/行数不齐而 exit 1 —— 改 cards.py 输出结构前先读它的断言