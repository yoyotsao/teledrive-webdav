"""Static guard: no log call or error message may interpolate what the person stored.

test_log_privacy.py runs scenarios; this reads the source, so a new log line is
caught even on a code path no scenario reaches. It looks for the few names that
carry identity: Telegram message / document ids and drive entry names.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "teledrive_webdav"

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
#: bare names that hold a Telegram message or document id
FORBIDDEN_NAMES = {"message_id", "msg_id", "document_id"}
#: attribute chains whose last two parts name a stored file
FORBIDDEN_ATTRS = {("entry", "name"), ("zip_entry", "name"), ("item", "document_id"),
                   ("part", "message_id"), ("msg", "id")}


def _offending(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in FORBIDDEN_NAMES:
            yield sub.id
        elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            if (sub.value.id, sub.attr) in FORBIDDEN_ATTRS or sub.attr in FORBIDDEN_NAMES:
                yield f"{sub.value.id}.{sub.attr}"


def _wrapped_in_label(arg: ast.AST) -> bool:
    """``label_for(message_id)`` and the fileid tag helpers turn an identity into an id tag."""
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, (ast.Name, ast.Attribute))
        and getattr(arg.func, "id", getattr(arg.func, "attr", "")).startswith(("label_for", "tag", "_tag", "_gt", "_gtag"))
    )


def _scan(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_log = isinstance(func, ast.Attribute) and func.attr in LOG_METHODS and (
            getattr(func.value, "id", "") in {"log", "logger"})
        is_raise_arg = False
        if is_log:
            args = node.args
        else:
            continue
        for arg in args:
            if _wrapped_in_label(arg):
                continue
            for hit in _offending(arg):
                yield node.lineno, hit, is_raise_arg


def test_no_log_call_interpolates_message_ids_or_entry_names():
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for lineno, hit, _ in _scan(path):
            problems.append(f"{path.relative_to(PACKAGE.parent)}:{lineno}: logs {hit}")
    assert not problems, "identity reached a log call (use fileid tags / tgio.label_for):\n" + "\n".join(problems)


def test_no_error_message_interpolates_message_ids_or_entry_names():
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            for arg in node.exc.args:
                if not isinstance(arg, ast.JoinedStr):
                    continue
                for value in arg.values:
                    if isinstance(value, ast.FormattedValue) and not _wrapped_in_label(value.value):
                        for hit in _offending(value.value):
                            problems.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno}: raises with {hit}")
    assert not problems, "identity reached an error message:\n" + "\n".join(problems)
