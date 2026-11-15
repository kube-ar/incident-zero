"""`oncall`: run checks, and try the console's tools by hand.

oncall tools                          the tools an agent gets, as the model sees them
oncall call get_page                  run one tool yourself
oncall call search_logs query="catalog-api error" limit=5
oncall check 0                        run milestone 0's checks on your code (needs an LLM)
oncall check 0 --reference            ...on the reference solution, to see what passing looks like
oncall use-reference 0                replace your file with the reference (yours is kept as .mine.py)
oncall use-reference 0 --undo
oncall llm                            check your LLM provider setup (.env) with three tiny calls
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from . import ROOT
from .milestones import MILESTONES


def _milestone(mid: str):
    if mid not in MILESTONES:
        sys.exit(f"No milestone {mid!r}. Available: {', '.join(MILESTONES)}")
    return MILESTONES[mid]


def cmd_tools(args) -> None:
    from .console import TOOLS

    for t in TOOLS:
        params = ", ".join(t["parameters"]["properties"]) or "no arguments"
        print(f"{t['name']}({params})")
        print(f"    {t['description']}\n")


def cmd_call(args) -> None:
    from .console import Console, is_error

    arguments: dict = {}
    for pair in args.arguments:
        key, sep, value = pair.partition("=")
        if not sep:
            sys.exit(f"Write arguments as name=value, like query=error (got {pair!r})")
        try:
            arguments[key] = json.loads(value)  # numbers and lists, like limit=5 or evidence='["a","b"]'
        except json.JSONDecodeError:
            arguments[key] = value
    result = Console().call(args.tool, arguments)
    print(result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(1 if is_error(result) else 0)


def cmd_check(args) -> None:
    m = _milestone(args.milestone)
    if args.reference:
        path = ROOT / m.reference
        spec = importlib.util.spec_from_file_location(f"reference_{m.id}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses in the module look themselves up here
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(m.module)
    checks = importlib.import_module(m.checks)
    from .checks.core import report

    print(f"Running milestone {m.id} on {'the reference solution' if args.reference else m.file}…")
    ok = report(m, checks.run(module), args.lang)
    sys.exit(0 if ok else 1)


def cmd_use_reference(args) -> None:
    m = _milestone(args.milestone)
    mine_dir, ref_dir = (ROOT / m.file).parent, (ROOT / m.reference).parent
    pairs = [(ROOT / m.file, ROOT / m.reference)] + [(mine_dir / f, ref_dir / f) for f in m.extra]

    def backup(path: Path) -> Path:
        return path.with_name(path.stem + ".mine" + path.suffix)

    if args.undo:
        if not backup(pairs[0][0]).exists():
            sys.exit(f"No backup at {backup(pairs[0][0]).relative_to(ROOT)}")
        for mine, _ in pairs:
            if backup(mine).exists():
                shutil.move(backup(mine), mine)
        print(f"Restored your files for milestone {m.id}")
        return
    if backup(pairs[0][0]).exists():
        sys.exit(f"{backup(pairs[0][0]).relative_to(ROOT)} already exists: run with --undo first, or remove it")
    for mine, ref in pairs:
        if mine.exists():
            shutil.copy(mine, backup(mine))
        shutil.copy(ref, mine)
    names = ", ".join(str(p.relative_to(ROOT)) for p, _ in pairs)
    print(f"Replaced {names} with the reference; yours are kept as *.mine.* (--undo puts them back).")


def cmd_llm(args) -> None:
    from .llm import Conversation, LLMError, get_llm

    try:
        llm = get_llm()
        print(f"Provider {llm.provider.name}, model {llm.model}" + (f", effort {llm.effort}" if llm.effort else ""))
        print("text:", llm.text("Reply with the single word: ready").strip()[:80])
        tool = {
            "name": "get_time",
            "description": "The current time in UTC.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }
        convo = Conversation(llm, [tool])
        turn = convo.send("What time is it in UTC? Use the tool.")
        if turn.done:
            print("tools: the model answered without calling the tool; pick a model that supports tool calling")
        else:
            turn = convo.reply({c.id: "14:08" for c in turn.tool_calls})
            print("tools:", turn.text.strip()[:80])
        print(f"OK: {llm.usage.calls} calls, {llm.usage.input_tokens} tokens in, {llm.usage.output_tokens} out.")
    except LLMError as e:
        sys.exit(f"LLM setup problem: {e}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="oncall", description="Incident Zero: checks and the on-call console.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tools", help="the console's tools, as the model sees them")
    p.set_defaults(fn=cmd_tools)

    p = sub.add_parser("call", help="run one tool yourself, like `oncall call search_logs query=error limit=5`")
    p.add_argument("tool")
    p.add_argument("arguments", nargs="*", help="name=value pairs")
    p.set_defaults(fn=cmd_call)

    p = sub.add_parser("check", help="run a milestone's checks")
    p.add_argument("milestone")
    p.add_argument("--reference", action="store_true", help="check the reference solution instead of your code")
    p.add_argument("--lang", default="en", choices=["en", "es"], help="language of the links to the lessons")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("use-reference", help="swap your file for the reference solution")
    p.add_argument("milestone")
    p.add_argument("--undo", action="store_true", help="put your own file back")
    p.set_defaults(fn=cmd_use_reference)

    p = sub.add_parser("llm", help="check your LLM provider setup (.env) with three tiny calls")
    p.set_defaults(fn=cmd_llm)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
