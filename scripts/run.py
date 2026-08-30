"""语言预习管线统一入口(推荐用法,Windows/macOS 均可)。

用法:
  uv run python scripts/run.py --book little_women                  # 一键全流程(不含发音)
  uv run python scripts/run.py --book little_women --audio          # 全流程 + 发音批量生成(需联网)
  uv run python scripts/run.py --book little_women --stage cards    # 只跑某个阶段
  uv run python scripts/run.py --book little_women --from-chapter 20    # pipeline 断点续跑
  uv run python scripts/run.py --book little_women --phrases           # 表达候选(不动 raw)
  uv run python scripts/run.py --book little_women --phrases --phrase-picked 'work/phrases_picked_*.json'  # 表达合并出卡
  uv run python scripts/run.py --book little_women --stage validate --verbose

阶段顺序: pipeline → apply(润色合并) → [audio] → cards → annotate → report → validate
说明: 任何阶段失败立即终止(非零退出码);底层仍是各单脚本,可独立直调。
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"

STAGE_SCRIPT = {
    "pipeline": "pipeline.py",
    "apply": "apply_polish.py",
    "audio": "gen_audio.py",
    "cards": "cards.py",
    "annotate": "annotate.py",
    "report": "report.py",
    "validate": "validate.py",
}
# 默认全流程顺序(audio 为可选,由 --audio 插入 cards 之前)
STAGES_TOTAL = ["pipeline", "apply", "cards", "annotate", "report", "validate"]
STAGES_CHAPTER = ("apply", "audio", "cards", "annotate")  # 支持 --chapter 的阶段


def run_stage(stage: str, args_list: list[str]):
    script = SCRIPTS / STAGE_SCRIPT[stage]
    print(f"\n========== [{stage}] {script.name} ==========", flush=True)
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
    ap.add_argument("--audio", action="store_true",
                    help="在 cards 前跑 gen_audio(发音,需联网)")
    ap.add_argument("--from-chapter", type=int, help="pipeline 断点续跑起始章")
    ap.add_argument("--phrases", action="store_true",
                    help="pipeline 阶段:只出表达候选(work/phrase_cands_*.json),不动 raw")
    ap.add_argument("--phrase-picked", default="",
                    help="apply 阶段:表达精选 JSON(ai_pick_phrases.py 产物,支持 glob)")
    ap.add_argument("--chapter", type=int,
                    help="只处理指定章(apply/gen_audio/cards/annotate)")
    ap.add_argument("--polish", help="apply 阶段: 润色 json 路径")
    ap.add_argument("--ai-en", help="apply 阶段: AI 补句 json 路径")
    ap.add_argument("--explain", help="apply 阶段: AI 例句解析 json 路径(ai_explain.py 产物,支持 glob)")
    ap.add_argument("--voice", help="audio 阶段: 英音语音,默认 en-GB-SoniaNeural")
    ap.add_argument("--force-audio", action="store_true",
                    help="audio 阶段: 忽略已有缓存重生成")
    ap.add_argument("--verbose", action="store_true", help="validate 阶段: 详细输出")
    args = ap.parse_args()

    if args.stage == "all":
        stages = list(STAGES_TOTAL)
        if args.audio:
            stages.insert(stages.index("cards"), "audio")
    else:
        stages = [args.stage]

    for stage in stages:
        sa = ["--book", args.book]
        if stage == "pipeline":
            if args.from_chapter:
                sa += ["--from-chapter", str(args.from_chapter)]
            if args.phrases:
                sa += ["--phrases"]
        elif stage == "apply":
            if args.phrase_picked:
                # 有 --phrase-picked(--phrases 可省:纯候选模式不需要合并时不传即可)
                sa += ["--phrases", args.phrase_picked]
            elif args.phrases:
                # --phrases 只出候选(未给 --phrase-picked):不需要合并,跳过 apply
                print("[run.py] 无 --phrase-picked 输入,跳过 apply"
                      "(--phrases 只出候选时不需要合并)", flush=True)
                continue
            elif not (args.polish or args.ai_en or args.explain):
                print("[run.py] 无 --polish/--ai-en/--explain 输入,跳过 apply"
                      "(先让模型润色 work/ 下的工作单)", flush=True)
                continue
            if args.polish:
                sa += ["--polish", args.polish]
            if args.ai_en:
                sa += ["--ai-en", args.ai_en]
            if args.explain:
                sa += ["--explain", args.explain]
        elif stage == "audio":
            if args.voice:
                sa += ["--voice", args.voice]
            if args.force_audio:
                sa += ["--force"]
        elif stage == "validate":
            if args.verbose:
                sa += ["--verbose"]
        if args.chapter and stage in STAGES_CHAPTER:
            sa += ["--chapter", str(args.chapter)]
        run_stage(stage, sa)

    print("\n[run.py] 全部阶段完成 ✔", flush=True)


if __name__ == "__main__":
    main()