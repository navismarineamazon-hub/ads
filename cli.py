# -*- coding: utf-8 -*-
"""
The entry convention, shared by every project in this folder.

    python run.py --list              列出所有可单独运行的操作
    python run.py <操作> [参数...]      只跑这一个操作，不跑别的
    python run.py <操作> --help        看这个操作自己的参数

An operation is one row in run.py's OPS table: a name, a one-line title, the
flags it takes, and the function to call. Adding an operation is adding a row —
`--list`, each operation's own `--help`, and the dispatch all come from here, so
no project needs a hand-rolled argument loop.

Everything an operation needs must arrive as a flag with a default. There is no
CONFIG block to keep in sync: the command line is the only source of truth.

This is a copy of sp-api/shared/cli.py, kept identical on purpose: the three
projects here share one entry convention but nothing else, and pro1-update talks
to a different API with different credentials. If you change one, change both —
there is nothing to change often, it is only the dispatch.
"""
from __future__ import annotations

import argparse
import sys


def flag(*names, **kw):
    """One argument of one operation, in argparse's own terms. See OPS tables."""
    return names, kw


class Op:
    """One separately runnable operation.

    name     what you type:  python run.py <name>
    title    one line, shown by --list
    run      called with the parsed flags as keyword arguments; returns an exit
             code (or None, which means 0)
    flags    flag() rows this operation accepts
    doc      longer text shown by `python run.py <name> --help`
    """

    def __init__(self, name, title, run, flags=(), doc="", aliases=()):
        self.name, self.title, self.run = name, title, run
        self.flags, self.doc, self.aliases = list(flags), doc or "", list(aliases)


def listing(ops):
    width = max(len(op.name) for op in ops)
    return "\n".join(f"  {op.name:<{width}}   {op.title}" for op in ops)


def build(prog, description, ops):
    hint = f"\n  python {prog} <操作> --help     看某个操作自己的参数"
    p = argparse.ArgumentParser(
        prog=prog, description=description, add_help=True,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="可单独运行的操作:\n" + listing(ops) + "\n" + hint)
    sub = p.add_subparsers(metavar="<操作>")
    for op in ops:
        child = sub.add_parser(
            op.name, aliases=op.aliases, help=op.title,
            description=op.doc or op.title,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        for names, kw in op.flags:
            child.add_argument(*names, **kw)
        child.set_defaults(_run=op.run)
    return p


def main(prog, description, ops, argv=None):
    """Parse argv, run exactly one operation, return its exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")    # Chinese names on a GBK console
        except (AttributeError, ValueError):
            pass

    parser = build(prog, description, ops)
    # Bare `python run.py` is someone pressing Run with nothing typed: show what
    # there is to run rather than an argparse error.
    if not argv or argv[0] in ("--list", "-l", "list"):
        print(f"\n{description}\n\n可单独运行的操作:\n{listing(ops)}\n")
        print(f"  python {prog} <操作> --help     看某个操作自己的参数\n")
        return 0

    args = parser.parse_args(argv)
    if not hasattr(args, "_run"):
        parser.print_help()
        return 1
    opts = {k: v for k, v in vars(args).items() if not k.startswith("_")}
    try:
        return args._run(**opts) or 0
    except KeyboardInterrupt:
        print("\n已中断，什么都没写。")
        return 130
