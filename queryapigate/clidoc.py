"""documentation/CLI.md, the command-line reference, written from the parser itself (rc1 review, D2) - every command,
subcommand, argument, choice and default, as `queryapigate` reads them. It is the CLI's snapshot too: a test fails
while the committed page differs from what this writes, so a renamed flag or a dropped choice is seen in review.

    python -m queryapigate.clidoc        # rewrite documentation/CLI.md
"""
import argparse
import os
from pathlib import Path
from unittest import mock

HEADER = """# Command-line reference

`queryapigate` and its commands, as the CLI itself defines them - this page is written from its parser
(`python -m queryapigate.clidoc`), and a test fails while the two differ.

**What's covered by the compatibility promise** ([Versioning and compatibility](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/CHANGELOG.md#versioning-and-compatibility)):
the commands, their arguments and choices below; output written for a program - every `--json`, `config export` and
`collection export`'s files, an `export`'s data; and the exit codes. **What isn't:** output written for a person - the
wording of a summary, a table, an error message - which can change in any release.

**Exit codes**, one convention: `0` it did what was asked, `1` it ran and failed, `2` usage or setup (a mistyped flag,
a setting the server can't read, a store it can't open). A few give a code its own meaning, said with them: `config
diff` exits `2` for "there are changes" (every `config` failure is `1`), `audit verify` `1` for a broken chain and `2`
when it can't be checked, `rekey --dry-run` `1` while a password can't be decrypted.

Defaults below are with no `QUERYAPIGATE_*` setting: `serve --host` and `--debug` take theirs from
`QUERYAPIGATE_HOST` and `QUERYAPIGATE_DEBUG` ([Configuration](CONFIGURATION.md)).
"""


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help for choice in action._choices_actions}
            for name, sub in action.choices.items():
                yield name, sub, helps.get(name)


def _argument(action):
    if action.option_strings:
        name = ', '.join(f'`{s}`' for s in action.option_strings)
        if action.nargs != 0 and not isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            name += f' `{action.metavar or action.dest.upper()}`'
    else:
        name = f'`{action.metavar or action.dest}`'
    notes = []
    if action.choices and not isinstance(action, argparse._SubParsersAction):
        notes.append('one of ' + ', '.join(f'`{c}`' for c in action.choices))
    if action.required and action.option_strings:
        notes.append('required')
    if not action.option_strings and action.nargs in ('?', '*'):
        notes.append('optional')
    if action.nargs in ('*', '+') or isinstance(action, argparse._AppendAction):
        notes.append('repeatable' if isinstance(action, argparse._AppendAction) else 'several')
    default = action.default
    if (default not in (None, False, argparse.SUPPRESS, [])
            and not isinstance(action, (argparse._StoreTrueAction, argparse._HelpAction))):
        notes.append(f'default `{default}`')
    text = (action.help or '').replace('|', '\\|').replace('%%', '%')
    return f"| {name} | {text}{' (' + '; '.join(notes) + ')' if notes else ''} |"


def _command(path, parser, help_text, out):
    out.append(f"\n{'#' * min(len(path) + 1, 4)} `{' '.join(path)}`\n")
    if help_text:
        out.append(help_text.replace('%%', '%') + '\n')
    rows = [_argument(a) for a in parser._actions
            if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)) and a.help != argparse.SUPPRESS]
    if rows:
        out.extend(['| Argument | Meaning |', '|---|---|', *rows])
    for name, sub, sub_help in _subparsers(parser):
        _command([*path, name], sub, sub_help, out)


def render():
    """The page, for a parser built with no QUERYAPIGATE_* setting - its defaults the CLI's own."""
    from . import cli
    clean = {k: v for k, v in os.environ.items() if not k.startswith('QUERYAPIGATE_')}
    with mock.patch.dict(os.environ, clean, clear=True):
        parser = cli.build_parser()
    out = [HEADER]
    for name, sub, sub_help in _subparsers(parser):
        _command(['queryapigate', name], sub, sub_help, out)
    return '\n'.join(out).rstrip() + '\n'


TARGET = Path(__file__).resolve().parent.parent / 'documentation' / 'CLI.md'

if __name__ == '__main__':
    TARGET.write_text(render())
    print(f'Wrote {TARGET}')
