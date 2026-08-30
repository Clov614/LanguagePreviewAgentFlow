# 🗂️ 语言预习工作流 (Language Preview Agent Flow)

> 参考《skill+美剧 学英语也太高效了》的方法论:**不要背单词,要识别单词**。
> 先提取生词预习、建立短期记忆,再在阅读中识别 —— 把"追剧学英语"的流程迁移到"读原著学英语"。

---

## <a id="intro"></a>🚀 项目简介(摘要)

**把英语原著变成"先刷词、再读书"的沉浸式学习闭环。**

读原著时最大的坎是生词:查得太勤打断阅读,不查又读不懂。本项目只做一件事——
把每章**最值得先认识**的生词(按 CEFR 级别与词频挑选,每章 15–20 个)**连同书中原句**做成 Anki 卡片,
先花 15 分钟刷出短期记忆,再回原文阅读时"认出"它 —— **不要背单词,要识别单词**。

- **输入**:一本英语 EPUB(示例已跑通:《Little Women》47 章 / 846 张卡 / 854 词总库)
- **产物**:每章 Anki TSV · 生词总库(跨书去重)· 阅读标注版 · 免费英音发音(mp3)
- **成本**:词库本地判定 + edge-tts 免费神经语音 —— 全程零付费、无需 API key

![LangPreviewAgentFlow_anki_exp_01.jpg](/resources/guide_pic/LangPreviewAgentFlow_anki_exp_01.jpg)

## <a id="quickstart"></a>⚡ 快速开始(3 步上手)

1. **装模板**(只做一次):Anki → File → Import → `resources/anki/anki_template.apkg`
2. **导卡**:读哪章导哪章 —— Import `data/output/little_women/anki/chapter_XX_anki.tsv`,**模板选 EnWords**(细节见「📥 Anki 导入方法」)
3. **开刷**:正面认词(发音自动播)→ 翻面看释义 + 书中原句 → 读对应章节(配 `annotated/` 高亮版)→ 掌握了的词追加到 `vocabulary/known_words.txt`,同词永不再推

想自己跑一本新书?一个命令即可(全流程细节见「🔄 管线」):

```bash
uv run python scripts/run.py --book <书名> --polish <润色json> --audio
```

---

## 📑 目录

| | |
|---|---|
| [🚀 项目简介](#intro) · [⚡ 快速开始](#quickstart) | [📦 交付物](#deliverables) · [🔊 发音(可选)](#audio) |
| [🎴 卡片内容(10 列)](#cards) · [📥 Anki 导入方法](#import) | [🤖 AI 例句解析](#ai_explain) · [🔄 管线与用法](#pipeline) |
| [🧱 资源与结构](#structure) · [⚙️ 环境准备](#setup) | [📜 许可与已知限制](#limits) |

---

## <a id="deliverables"></a>📦 交付物 —— 已完成的《Little Women》

| 产物 | 位置 | 说明 |
|---|---|---|
| **Anki 模板包**(导入一次即可) | `resources/anki/anki_template.apkg` | EnWords 笔记类型 + 卡片样式 + 示例卡 + **自动播放脚本**(正面自动播单词音、翻面自动播例句音),File → Import 直接装 |
| **Anki 卡片 ×47 章,846 张** | `data/output/little_women/anki/chapter_XX_anki.tsv` | 每章 18 词一个 TSV,配上面模板导入 |
| **单词/例句英音发音**(可选) | `data/output/little_women/anki/audio/` | edge-tts 免费神经语音预生成 mp3,已自动拷入 collection.media,导入即带发音 |
| **生词 CSV**(Excel 直接打开) | `data/output/little_women/raw/chapter_XX_raw.csv` | 每章候选词全量数据,UTF-8 带 BOM |
| **生词标注版** | `data/output/little_women/annotated/chapter_XX.md` | 刷完卡,读书时**识别**高亮词 |
| **章节词表速查** | `annotated/chapter_XX_词表.md` | 每章生词 + 释义一览 |
| **推荐报告** | `data/output/little_women/recommend_report.md` | TOP 40 + 使用说明 |
| **生词总库** | `vocabulary/master_wordlist.csv` | 854 词,跨书累积、去重、状态流转 |
| **润色工作单** | `data/output/little_women/work/` | polish_*.json 等,已应用的记录留存 |

---

## <a id="cards"></a>🎴 卡片内容(TSV 10 列)

```
单词 │ 音标 │ 词性 │ 中文释义 │ CEFR │ 原文例句 │ 例句译文 │ 来源 │ AI解析 │ 词义概述
```

- **例句全部取自书中原句**(含目标词上下文),配人工润色译文 —— 与"看剧识别台词"同构
- 无原句的词由模型补充,例句贴近文风,「来源」列标注 **"(AI 补句)"**
- **级别**:B2/C1 为主(考研英语二基线:排除 A1–A2,B1 少量配额,用 BNC 词频过滤常见词)
- **AI解析**:**四段式整句解析**(逐项拆解 → 整句解读 → 文化点),例句中比目标词更难的
  **超纲词**已用橙色高亮标注(每句 ≤2);**词义概述**:一句"画面感"记忆钩子。两列可选,
  由 `scripts/ai_explain.py` 批量生成(见"AI 例句解析"一节)
- **发音**:可选,见下节

---

## <a id="audio"></a>🔊 发音(可选):免费英音神经语音

**原理**:用 edge-tts(微软 Edge 浏览器同款**神经网络语音**)预生成 mp3,免费、无需注册、无需 API key。生成后 `[sound:]` 内嵌进卡片对应字段,TSV 仍保持 10 列,导入即听,同步后手机端(AnkiDroid / AnkiMobile)也能播。

**生成命令**(在出卡前跑;已出卡的书随时可补):

```bash
uv run python scripts/gen_audio.py --book little_women    # 全书单词 + 例句英音
# 常用参数:--chapter 1 先试单章;--force 重新生成;--no-copy 只生成不拷入 Anki
# 跑完后重跑 cards.py 才会把 [sound:] 写进 TSV;也可直接用统一入口:
uv run python scripts/run.py --book little_women --audio
```

**可选发音人**(当前统一为英音):

| 语音 | 说明 |
|---|---|
| `en-GB-SoniaNeural` | ⭐ 英音 · 女声(**默认**,自然沉稳) |
| `en-GB-LibbyNeural` | 英音 · 女声(年轻活泼) |
| `en-GB-MaisieNeural` | 英音 · 少女声 |
| `en-GB-RyanNeural` | 英音 · 男声 |
| `en-GB-ThomasNeural` | 英音 · 男声(低沉老成) |
| `en-US-AriaNeural` / `en-US-AndrewNeural` | 美音 · 女 / 男(想换美音时同样免费) |

- **换发音人**:`--voice en-GB-RyanNeural --force` 重生成即可;文件名不变,**无需重导卡片**
- 完整英文语音可用 `--list-voices` 查看;edge-tts 还支持日/德/西等 300+ 语音(未来日语书可直接复用)

---

## <a id="import"></a>📥 Anki 导入方法(跟着做即可)

> **关键:先导入模板包,再导卡片 TSV。** 模板只装一次,之后每章只需导入 TSV。

**第一步:安装笔记模板(只做一次)**

1. 打开 Anki 桌面版 → 菜单 **File(文件)→ Import(导入)**
2. 选择 `resources/anki/anki_template.apkg` 导入
3. 验证:菜单 **Tools → Manage Note Types**,应出现 **EnWords**(10 字段与 TSV 10 列一一对应)
4. 牌组栏出现 `EnglishBooksWords::LittleWomen`,自带 1 张示例卡(decidedly,可留作预览,也可删)

**第二步:导入每章卡片(每读一章做一次)**

1. File → Import → 选 `data/output/little_women/anki/chapter_XX_anki.tsv`
2. **笔记模板选 `EnWords`** ⚠️ 不要用默认的 Basic(会丢例句译文、来源、AI解析等列,排版也乱)
   - 区分:EnWords 能看到"音标 / 中文释义 / 来源"字段,Basic 只有 Front / Back
3. **勾选「允许在字段中使用 HTML」**——例句中的生词高亮(`<b class="hl">`)依赖它
4. **牌组**:选或新建 `EnglishBooksWords::LittleWomen::ChXX`(每章一个子牌组,方便按章复习)
5. 其余保持默认 → 导入
6. 自检:正面大号单词 + 音标 + 词性;背面释义 + 蓝色引用块例句(**例句里的生词加粗高亮显示**,含 slipped / glancing / crept 等变形);已生成发音则单词旁有播放按钮、背面例句自动朗读

**给旧卡补发音**:已导入过的章节重新导入该章 TSV,导入时选 **更新现有笔记** 即可补上 [sound:],无需删卡。

**常见问题**

| 问题 | 解决 |
|---|---|
| 多列挤在一个格里 / 排版乱 | 模板选成了 Basic → 删卡重导,选 EnWords |
| 同一词跨章重复 | 正常现象;导入选「更新现有笔记」(按"单词"匹配) |
| 没声音 → 已跑 gen_audio 仍无声 | 确认 mp3 拷入 collection.media 后重导 TSV;再不行 → 工具 → 检查媒体 |
| 例句里生词没高亮 | 导入卡时勾选「允许在字段中使用 HTML」;已导过的章重新导入该章 TSV 并选 **更新现有笔记** 即补上(与补发音同路径) |
| 想换发音人 | 见上文「发音(可选)」,`--force` 重生成,不必重导卡片 |
| 想关掉自动播放 | 重导模板后自动播放默认开启(需先跑过 gen_audio 生成 mp3)。要关闭:Tools → Manage Note Types → EnWords → Cards,删掉正面/背面模板末尾的 `<script>…</script>` 两段即可 |
| 想改卡片样式 | 改 `scripts/make_anki_template.py` 的 CSS(已内置深色主题适配),重新生成并导入模板包 |

---

## <a id="pipeline"></a>🔄 管线(第二本书怎么用)

**推荐:统一入口 `scripts/run.py`** —— 一个命令跑全流程,失败即停:

```bash
# ① EPUB 放入 data/books/,模型润色好 work/ 下工作单后,一键全流程:
uv run python scripts/run.py --book <书名> --polish <润色json>
#    加 --audio 顺带批量生成发音(需联网,免费):
uv run python scripts/run.py --book <书名> --polish <润色json> --audio
#    断点续跑 pipeline(只重跑第 20 章起):
uv run python scripts/run.py --book <书名> --from-chapter 20
#    只跑某阶段 / 只看校验:
uv run python scripts/run.py --book <书名> --stage validate --verbose
```

**流程**:EPUB

→ `pipeline`(分词 / CEFR / 选词 / 例句)

→ 模型润色(work/ 工作单)

→ `apply`(合并润色)

→ `audio`(发音,可选)

→ `cards`(Anki TSV + 生词总库)

→ `annotate`(标注版)

→ `report`(推荐报告)

→ `validate`(缺口校验,全绿 exit 0)

**每个脚本也可独立直调**(参数见各自 `-h`),便于单步执行或调试:

```bash
uv run python scripts/gen_audio.py --book <书名> --chapter 1   # 只做第 1 章发音
uv run python scripts/cards.py --book <书名>                   # 只出 TSV + 更新总库
```

---

## <a id="ai_explain"></a>🤖 AI 例句解析(可选,填卡背两列)

**不填也能出卡**;想要更深入的自学体验再跑。给每章选词批量生成
「AI解析」(四段式整句解析)+「词义概述」(画面感记忆钩子),产物在
`data/output/<书名>/work/ai_explain_*.json`,可手改后合并进卡片。

```bash
# ① 批量生成(ch 可省略;可断点续跑,已生成词自动跳过)
uv run python scripts/ai_explain.py --book <书名> --workers 4 --batch-size 6
#    试跑一章 6 词看效果:加 --chapter 1 --limit 6
#    只预览不调用:加 --dry-run

# ② 合并进 raw CSV → ③ 重新出卡
uv run python scripts/apply_polish.py --book <书名> --explain work/ai_explain_<书名>_ch01.json
uv run python scripts/cards.py --book <书名>
```

**接入点三选**(`--provider`):

| 接入点 | 说明 |
|---|---|
| `claude-cli`(默认) | 本机 Claude Code,零配置零 key |
| `anthropic` | 官方 API,环境变量 `ANTHROPIC_API_KEY` |
| `openai` | 任意 OpenAI 兼容端点:GPT / **DeepSeek** / **Gemini** / **Ollama**(本地免费)… |

**环境变量配置**(key 只读环境变量,不落盘;也可 `--api-key/--base-url/--model` 覆盖):

```bash
# DeepSeek 示例:
set OPENAI_API_KEY=sk-xxx
set OPENAI_BASE_URL=https://api.deepseek.com/v1
set OPENAI_MODEL=deepseek-chat
uv run python scripts/ai_explain.py --book <书名> --provider openai

# Ollama 本地示例(无需 key,模型名如 qwen3):
set OPENAI_BASE_URL=http://localhost:11434/v1
set OPENAI_MODEL=qwen3
uv run python scripts/ai_explain.py --book <书名> --provider openai

# Gemini 示例(key 用 GEMINI_API_KEY 的值):
set OPENAI_API_KEY=<GEMINI_API_KEY>
set OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
set OPENAI_MODEL=gemini-2.5-flash
uv run python scripts/ai_explain.py --book <书名> --provider openai
```

**提速参数**:`--workers 4` 多章并发(默认开)、`--batch-size 6` 一批解析 6 词(默认),实测比逐词快约 4.6 倍,claude-cli 下 47 章约 1 小时内;`--limit N` 试跑、`--verbose` 重试失败词。

---

## <a id="structure"></a>🧱 资源与结构

**输入**:`data/books/<书名>.epub` → 缓存 `data/books/_md/<书名>.md`

**输出**(每本书):`data/output/<书名>/`

| 目录 | 内容 |
|---|---|
| `raw/` | 候选词 CSV(UTF-8 **带 BOM**,Excel 直接打开) |
| `anki/` | 导入卡片 TSV(UTF-8 **无 BOM**)+ `audio/` 发音缓存 |
| `annotated/` | 高亮标注版 + 章节词表 |
| `work/` | 润色工作单(已应用记录留存) |

**脚本**(`scripts/`):

- 🚀 入口:`run.py`(统一调用)、`tts_paths.py`(发音命名约定)
- 🔧 管线:`pipeline.py` → `apply_polish.py` → `gen_audio.py` → `cards.py` → `annotate.py` → `report.py` → `validate.py`
- 🛠️ 一次性:`build_dict_db.py`、`make_anki_template.py`、`migrate_wordlist.py`

**核心资产**:`vocabulary/master_wordlist.csv`(生词总库,跨书去重)、`vocabulary/known_words.txt`(已掌握词,不再重复推荐)

**其他**:`tools/wenyi/`(参考项目,未来美剧模式复用)、语言适配层(日语等新语言只需新增 adapter,见 PLAN.md §7)

---

## <a id="setup"></a>⚙️ 环境准备:词表资源获取

> 大型词表文件(数百 MB)不入仓库,clone 后按下面两步准备,之后全程**离线可用**。

**① 本地词典 `resources/ecdict.db`**(音标 / 释义 / 词频,约 294 MB)

```bash
# 下载 https://github.com/skywind3000/ECDICT/releases 的「ECDICT - 28」版本
#   → 资产 ecdict-stardict-28.zip(约 70 MB,MIT 许可)
# 解压到 resources/stardict-ecdict-2.4.2/(含 .idx / .dict / .ifo 三个文件)
uv run python scripts/build_dict_db.py    # 340 万词条 → resources/ecdict.db
```

**② 已随仓库的资源(无需下载)**

| 资源 | 来源 | 说明 |
|---|---|---|
| `resources/oxford3000-5000/` | github.com/chunzhng/Oxford-3000-5000 | oxford-5000.csv 含 A1–C1 全部级别,CEFR 判定主词表 |
| `resources/ecdict.mini.csv` | ECDICT(skywind3000, MIT) | 精简词表,快速分词 / 词频辅助 |

---

## <a id="limits"></a>📜 许可与已知限制

**第三方许可**:示例书《Little Women》(1868–1869)**公有领域**;`tools/wenyi/` 为 MIT;ECDICT 词表为 MIT(供个人学习);牛津词表来自公开仓库,仅个人使用。

**已知限制**

- 级别标注已按词表校准,个别词可能有偏差(以 BNC 词频校准)
- 例句抽取优先短句,个别例句非最优上下文;AI 补句已在「来源」列标注
- 生僻度打分为启发式,如需更严 / 更松可在 `scripts/pipeline.py` 调整配比
- `scripts/migrate_wordlist.py`:P0 遗留数据迁移脚本,新流程不需要跑