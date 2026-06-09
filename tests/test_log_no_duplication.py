"""Stop bot.log lines being written twice.

Root cause (confirmed via lsof on running process PID 46162 on 5/22):

  Python    46162   1w   REG   ...  bot.log     # FD 1 = stdout
  Python    46162   2w   REG   ...  bot.log     # FD 2 = stderr
  Python    46162   6w   REG   ...  bot.log     # RotatingFileHandler

The bot is launched with shell stdout redirection (``>> bot.log 2>&1``).
``_setup_logging`` adds a StreamHandler that writes to ``sys.stdout`` —
which the shell has already pointed at bot.log. So every record goes to
bot.log twice: once via the StreamHandler-into-redirected-stdout, once
via the RotatingFileHandler. Every line in bot.log on 5/22 was a
byte-identical pair.

Fix: if stdout already points at the same file as our RotatingFileHandler,
skip the StreamHandler. Other capture modes (PM2, systemd, ``tee``) leave
stdout pointing at a different file or a pipe and continue to receive the
console stream.
"""
import io
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler

import pytest

import main as main_mod


@pytest.fixture(autouse=True)
def _reset_logging():
    """Restore root logger handlers/level around each test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for h in root.handlers[:]:
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _handler_kinds() -> tuple[list[logging.StreamHandler], list[RotatingFileHandler]]:
    stream = []
    rotating = []
    for h in logging.getLogger().handlers:
        if isinstance(h, RotatingFileHandler):
            rotating.append(h)
        elif isinstance(h, logging.StreamHandler):
            stream.append(h)
    return stream, rotating


def test_stdout_to_unrelated_file_keeps_streamhandler():
    """PM2/systemd typically point stdout at their own capture file. That's
    not bot.log, so the StreamHandler is needed for them and must stay."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "bot.log")
        unrelated_path = os.path.join(tmp, "pm2-out.log")
        saved_stdout = sys.stdout
        with open(unrelated_path, "w") as f:
            sys.stdout = f
            try:
                main_mod._setup_logging(log_path=log_path)
            finally:
                sys.stdout = saved_stdout
        stream, rotating = _handler_kinds()
        assert len(rotating) == 1
        assert len(stream) == 1, (
            "PM2-style capture (stdout → unrelated file) needs the "
            "StreamHandler so operators still see live output"
        )


def test_stdout_redirected_to_log_file_drops_streamhandler():
    """The actual 5/22 production setup: stdout was shell-redirected to
    bot.log. We must NOT add a StreamHandler in that case — it would
    duplicate every record on top of the RotatingFileHandler."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "bot.log")
        # Open the log file for writing and point stdout at it BEFORE
        # _setup_logging runs — mirrors the shell `>> bot.log` ordering.
        with open(log_path, "a") as f:
            saved_stdout = sys.stdout
            sys.stdout = f
            try:
                main_mod._setup_logging(log_path=log_path)
            finally:
                sys.stdout = saved_stdout
        stream, rotating = _handler_kinds()
        assert len(rotating) == 1
        assert len(stream) == 0, (
            "stdout already targets bot.log; a StreamHandler would write "
            "every log line a second time into the same file"
        )


def test_no_record_appears_twice_when_stdout_is_log_file():
    """End-to-end: emit one log line under the duplicated-fd scenario and
    confirm bot.log contains exactly one copy. Pinpoints the 5/22 bug."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "bot.log")
        with open(log_path, "a") as f:
            saved_stdout = sys.stdout
            sys.stdout = f
            try:
                main_mod._setup_logging(log_path=log_path)
                logging.getLogger("src.checker").info("UNIQUE_MARKER_xyz")
                for h in logging.getLogger().handlers:
                    h.flush()
            finally:
                sys.stdout = saved_stdout

        with open(log_path) as f:
            contents = f.read()
        assert contents.count("UNIQUE_MARKER_xyz") == 1, (
            f"Expected exactly one log line; got "
            f"{contents.count('UNIQUE_MARKER_xyz')}.\nLog contents:\n{contents}"
        )


def test_non_real_file_stdout_keeps_streamhandler():
    """A non-fileno stdout (e.g. captured StringIO under pytest) must not
    crash the inode check and must keep the StreamHandler."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "bot.log")
        saved_stdout = sys.stdout
        sys.stdout = io.StringIO()  # no fileno()
        try:
            main_mod._setup_logging(log_path=log_path)
        finally:
            sys.stdout = saved_stdout
        stream, rotating = _handler_kinds()
        assert len(rotating) == 1
        assert len(stream) == 1
