"""语言预习管线统一入口(成熟 CLI:一条命令从 EPUB 产物到 Anki TSV,无需人工逐步指挥)。

一条命令(新书另需一次前置,见文末):
  uv run python scripts/run.py --book <书名>                 # 全流程到 TSV(不含发音)
  uv run python scripts/run.py --book <书名> --audio         # 全流程 + 发音(需联网)
  uv run python scripts/run.py --book <书名> --chapters 1-5   # 只跑部分章(测试/分批)
  uv run python scripts/run.py --book <书名> --stage cards    # 只重跑某个阶段
  uv run python scripts/run.py --book <书名> --stage validate --verbose

阶段顺序(模型阶段自动跳过已完成词/章 —— 断点安全,不重复烧 token;失败词记 failed.json):
  [前置,仅新书]  epub_to_md 转 MD + scan_proper 专名扫描
  pipeline        词选 + 重建 raw(自动继承既有润色/解析列,重跑不再清空资产)
  polish          AI 补齐缺失 cn_mean/cn_sent;无原句的词自动生成例句(ai_polish.py)
  phrases         表达候选 + AI 精选,只挑有润色数据的章(ai_pick_phrases.py)
  explain         AI 三段式例句解析(ai_explain.py)
  apply           把 polish/ai-en/explain/picked 全部合并进 raw(自动发现 work/ 产物)
  [audio]         发音 mp3(--audio 时插入,需联网)
  cards → annotate → report → validate
validate 含硬门禁:要出卡的词例句必须齐全,缺释义/缺例句会让校验非零退出。
任何阶段失败立即终止(非零退出码);每个阶段仍是独立脚本,可单独直调。

手动覆盖:--polish/--ai-en/--explain/--phrase-picked 可指定自己的 JSON(不传则自动发现
polish 阶段与 explain/phrases 阶段在 work/ 下的产物)。
"""
import argparse
import csv
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"

STAGE_SCRIPT = {
    "pipeline": "pipeline.py",
    "polish": "ai_polish.py",
    "phrases": "ai_pick_phrases.py",
    "explain": "ai_explain.py",
    "apply": "apply_polish.py",
    "audio": "gen_audio.py",
    "cards": "cards.py",
    "annotate": "annotate.py",
    "report": "report.py",
    "validate": "validate.py",
}
# 全流程顺序(audio 由 --audio 插到 cards 前;polish/phrases/explain 是模型阶段,
# 自动跳过已完成内容,重复跑不重复花钱)
STAGES_TOTAL = ["pipeline", "polish", "phrases", "explain", "apply",
                "cards", "annotate", "report", "validate"]
STAGES_CHAPTER = ("apply", "audio", "cards", "annotate")   # 支持 --chapter 的阶段
MODEL_STAGES = ("polish", "phrases", "explain")            # 逐章圈范围的模型阶段


def parse_chapters(spec: str) -> list[int]:
    """解析 "1-5" / "3" / "1,3-5" → [1,2,3,4,5];空串 → [](=全部章)。"""
    out: list[int] = []
    for part in (spec or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def cardable_chapters(book: str) -> list[int]:
    """有润色数据(会出卡)的章号 —— phrases 阶段只挑这些章,防止给
    尚未润色的章生成表达卡(TSV 出现只有短语没有单词的假象)。"""
    raw = BASE / "data" / "output" / book / "raw"
    out = []
    for fp in sorted(raw.glob("chapter_*_raw.csv")):
        if fp.name.endswith("_phrase_raw.csv"):
            continue
        rows = list(csv.DictReader(open(fp, encoding="utf-8-sig", newline="")))
        if any(r.get("cn_mean") for r in rows):
            out.append(int(fp.name.split("_")[1]))
    return out


def run_stage(stage: str, args_list: list[str]):
    script = SCRIPTS / STAGE_SCRIPT[stage]
    print(f"\n========== [{stage}] {script.name} {' '.join(args_list)} ==========", flush=True)
    proc = subprocess.run([sys.executable, str(script), *args_list], cwd=BASE)
    if proc.returncode != 0:
        print(f"[run.py] 阶段 {stage} 失败(exit {proc.returncode}),管线终止", flush=True)
        sys.exit(proc.returncode)


def main():
    ap = argparse.ArgumentParser(
        description="语言预习管线统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--book", required=True, help="书名(如 little_women)")
    ap.add_argument("--stage", default="all",
                    choices=["all", *STAGE_SCRIPT.keys()],
                    help="只跑指定阶段(默认 all 全流程)")
    ap.add_argument("--chapters", default="",
                    help='章范围,如 "1-5" / "3" / "1,3-5"(默认全部章;'
                         "作用于 polish/phrases/explain/apply/audio/cards/annotate/validate)")
    ap.add_argument("--audio", action="store_true",
                    help="在 cards 前跑 gen_audio(发音,需联网)")
    ap.add_argument("--from-chapter", type=int, help="pipeline 断点续跑起始章")
    ap.add_argument("--chapter", type=int,
                    help="只处理指定单章(等价 --chapters N,兼容旧参数)")
    ap.add_argument("--polish", help="apply 阶段: 润色 json 路径(手动覆盖)")
    ap.add_argument("--ai-en", help="apply 阶段: AI 补句 json 路径(手动覆盖)")
    ap.add_argument("--explain", help="apply 阶段: AI 例句解析 json(支持 glob,手动覆盖)")
    ap.add_argument("--phrase-picked", default="",
                    help="apply 阶段: 表达精选 json(支持 glob,手动覆盖)")
    ap.add_argument("--voice", help="audio 阶段: 英音语音,默认 en-GB-SoniaNeural")
    ap.add_argument("--force-audio", action="store_true",
                    help="audio 阶段: 忽略已有缓存重生成")
    ap.add_argument("--verbose", action="store_true",
                    help="模型阶段/validate 详细输出")
    args = ap.parse_args()

    chapters = parse_chapters(args.chapters)
    if args.chapter:
        chapters = sorted({*chapters, args.chapter})

    if args.stage == "all":
        stages = list(STAGES_TOTAL)
        if args.audio:
            stages.insert(stages.index("cards"), "audio")
    else:
        stages = [args.stage]

    # 新书预检:管线输入 MD 由 epub_to_md.py 生成(EPUB→MD 是管线外的一次性前置步骤)
    if "pipeline" in stages:
        md = BASE / "data" / "books" / "_md" / f"{args.book}.md"
        if not md.exists():
            sys.exit(
                f"[run.py] [FAIL] 找不到管线输入 {md} —— 新书先做前置两步:\n"
                "  1. uv run --with markitdown python scripts/epub_to_md.py "
                '--epub "data/books/<书>.epub" --book <书名>\n'
                "  2. uv run python scripts/scan_proper.py --book <书名> --write   "
                "# 扫描专名,确认 proper_names/<书名>.txt 后再回来")

    work = BASE / "data" / "output" / args.book / "work"

    def in_scope(fp: Path) -> bool:
        if not chapters:
            return True
        digits = "".join(ch for ch in fp.stem if ch.isdigit())
        return bool(digits) and any(fp.stem.endswith(f"ch{n:02d}") for n in chapters)

    def run_model_stage(stage: str):
        """模型阶段:给了章范围就逐章圈定,否则整书一次(脚本自身会跳过已完成内容)。"""
        if chapters and stage in MODEL_STAGES:
            for n in chapters:
                run_stage(stage, ["--book", args.book, "--chapter", str(n)])
        else:
            extra = ["--verbose"] if args.verbose else []
            run_stage(stage, ["--book", args.book, *extra])

    for stage in stages:
        if stage == "pipeline":
            sa = ["--book", args.book]
            if args.from_chapter:
                sa += ["--from-chapter", str(args.from_chapter)]
            run_stage(stage, sa)

        elif stage == "polish":
            run_model_stage(stage)

        elif stage == "phrases":
            # 表达候选(本地)→ AI 精选(只挑有润色数据的章,防"只有短语没有单词"假象)
            run_stage("pipeline", ["--book", args.book, "--phrases"])
            scope = chapters or cardable_chapters(args.book)
            if not scope:
                print("[run.py] 无润色章,跳过 phrases", flush=True)
                continue
            for n in scope:
                run_stage("phrases", ["--book", args.book, "--chapter", str(n)])

        elif stage == "explain":
            run_model_stage(stage)

        elif stage == "apply":
            manual_any = args.polish or args.ai_en or args.explain or args.phrase_picked
            if args.phrase_picked:
                run_stage(stage, ["--book", args.book,
                                  "--phrases", args.phrase_picked])
            if args.polish:
                sa = ["--book", args.book, "--polish", args.polish]
                if args.ai_en:
                    sa += ["--ai-en", args.ai_en]
                if args.chapter and args.stage == "apply":
                    sa += ["--chapter", str(args.chapter)]
                run_stage(stage, sa)
            if args.explain:
                run_stage(stage, ["--book", args.book, "--explain", args.explain])
            if manual_any:
                continue
            # 自动发现:polish 阶段产物(逐文件,ai_en 就在同文件里)→ explain → picked
            for fp in sorted(work.glob(f"polish_ai_{args.book}_ch*.json")):
                if in_scope(fp):
                    run_stage(stage, ["--book", args.book, "--polish", str(fp)])
            expl = sorted(p for p in work.glob(f"ai_explain_{args.book}_ch*.json")
                          if in_scope(p) and not p.name.endswith("_failed.json"))
            if expl:
                # apply 的 --explain/--phrases 以仓库根为 cwd,必须给 data/output 全路径
                run_stage(stage, ["--book", args.book, "--explain",
                                  f"data/output/{args.book}/work/ai_explain_{args.book}_ch*.json"])
            picked = sorted(p for p in work.glob(f"phrases_picked_{args.book}_ch*.json")
                            if in_scope(p) and not p.name.endswith("_failed.json"))
            if picked:
                run_stage(stage, ["--book", args.book,
                                  "--phrases",
                                  f"data/output/{args.book}/work/phrases_picked_{args.book}_ch*.json"])
            if not any([args.polish, args.explain, args.phrase_picked,
                        list(work.glob(f"polish_ai_{args.book}_ch*.json")),
                        expl, picked]):
                print("[run.py] apply 无可合并产物(模型阶段会生成),跳过", flush=True)
                continue

        elif stage == "audio":
            sa = ["--book", args.book]
            if args.voice:
                sa += ["--voice", args.voice]
            if args.force_audio:
                sa += ["--force"]
            if chapters:
                for n in chapters:
                    run_stage(stage, [*sa, "--chapter", str(n)])
            else:
                run_stage(stage, sa)

        elif stage == "cards":
            for n in (chapters or [None]):
                run_stage(stage, ["--book", args.book, *(["--chapter", str(n)] if n else [])])

        elif stage == "annotate":
            for n in (chapters or [None]):
                run_stage(stage, ["--book", args.book, *(["--chapter", str(n)] if n else [])])

        elif stage == "report":
            run_stage(stage, ["--book", args.book])

        elif stage == "validate":
            sa = ["--book", args.book]
            if args.verbose:
                sa += ["--verbose"]
            if chapters:
                sa += ["--chapters", args.chapters or ",".join(map(str, chapters))]
            run_stage(stage, sa)

    print("\n[run.py] 全部阶段完成 ✔ 产物: data/output/<书名>/anki/*.tsv", flush=True)


if __name__ == "__main__":
    main()
