"""Pin the process's standard streams to UTF-8.

Every entry point calls `force_utf8_stdio()` before it touches a stream. Windows
gives a *piped* stream the locale encoding — cp1252 on most installs, not UTF-8 —
which breaks both directions the harness uses:

  stdin   the harness writes its JSON payload as UTF-8. A prompt in any language
          outside cp1252 is mangled, and a byte cp1252 leaves undefined (0x81,
          0x8D, 0x8F, 0x90, 0x9D) raises UnicodeDecodeError outright.
  stdout  the KB text printed back carries `—`, `→`, `✓` and `⚠`, none of which
          cp1252 can encode — UnicodeEncodeError.

The stdout failure is the dangerous one: hooks swallow every exception by design
so that a crashing hook cannot end the user's session, which turns an encode
error into an *empty* injection with no visible symptom. Pinning the encoding
makes that failure impossible instead of merely unlikely.

`errors="replace"` is a backstop, never the contract — the streams are UTF-8, so
it should not fire; it exists so a lone surrogate cannot take a hook down.
"""

from __future__ import annotations

import contextlib
import io
import sys


def force_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if not isinstance(stream, io.TextIOWrapper):
            # Something replaced the stream and is not locale-encoded at all —
            # pytest's capture object, most often.
            continue
        # Reconfiguring fails on a detached or already-read stream. Neither is
        # the locale-encoded pipe this guards against, so carrying on is correct.
        with contextlib.suppress(ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")
