"""bridge.log has to stay readable, because it is the whole diagnostic story.

Offline. What is being defended is a log a person can actually scan: Telethon
logs one line per ``iter_download`` at INFO, this bridge makes one
``iter_download`` per 512 KiB request, and a sweep's shell warm therefore
sustained ~360 of the same line a minute — 1,993 out of 2,100 lines in a
measured bridge.log, with five FLOOD_WAIT lines buried in them and 8 MB of
history rotated away in minutes.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import ThrottleRepeats  # noqa: E402


def _logger(every, name):
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    kept = []

    class _Sink(logging.Handler):
        def emit(self, record):
            kept.append(record.getMessage())

    log.handlers = [_Sink()]
    log.filters = [ThrottleRepeats(every=every)]
    return log, kept


def test_a_flood_from_one_call_site_becomes_one_line(request):
    log, kept = _logger(60.0, request.node.name)

    for offset in range(0, 400):
        log.info("Starting direct file download in chunks of %d at %d", 524288, offset)

    assert len(kept) == 1
    assert "at 0" in kept[0]


def test_the_next_line_says_how_many_it_held(request):
    """A collapsed flood still has to be legible as a flood, not as one read."""
    log, kept = _logger(60.0, request.node.name)
    log.info("Starting direct file download in chunks of %d at %d", 524288, 0)
    for _ in range(9):
        log.info("Starting direct file download in chunks of %d at %d", 524288, 0)
    log.filters[0].every = 0.0  # the window has closed
    log.info("Starting direct file download in chunks of %d at %d", 524288, 0)

    assert "+9 more" in kept[-1]


def test_other_lines_from_the_same_logger_are_not_delayed(request):
    """The reason this is a filter and not logger.setLevel(WARNING).

    These are the download diagnostics CLAUDE.md is written around: a file in
    another DC, a file reference that expired mid-read. Throttling is keyed on
    the message template, so a flood of one of them cannot hold back the first
    of another.
    """
    log, kept = _logger(60.0, request.node.name)

    for _ in range(50):
        log.info("Starting direct file download in chunks of %d at %d", 524288, 0)
    log.info("File lives in another DC")
    log.info("File ref expired during download; refetching message")

    assert kept[1:] == [
        "File lives in another DC",
        "File ref expired during download; refetching message",
    ]


def test_a_distinct_shape_of_the_same_flood_still_reports(request):
    """direct vs indirect is a 2x change in reads per chunk (see CLAUDE.md).

    Different templates, so the one that shows up second is not swallowed by
    the first one's window.
    """
    log, kept = _logger(60.0, request.node.name)

    for _ in range(50):
        log.info("Starting direct file download in chunks of %d at %d", 524288, 0)
    log.info("Starting indirect file download in chunks of %d at %d", 524288, 0)

    assert len(kept) == 2
    assert "indirect" in kept[1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
