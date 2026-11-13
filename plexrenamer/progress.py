"""The progress bar that apply and undo print, and how a duration is written."""

from __future__ import annotations

import sys
import time


class Progress:
    def __init__(self, total: int, label: str = "", width: int = 38):
        self.total, self.label, self.width = max(total, 1), label, width
        self.n = 0
        self.start = time.time()
        self.tty = sys.stdout.isatty()
        self.last = 0.0
        self.last_pct = -1

    def step(self, n: int = 1, note: str = ""):
        self.n += n
        now = time.time()
        pct = int(self.n * 100 / self.total)
        if self.tty:
            if now - self.last < 0.08 and self.n < self.total:
                return
        elif pct == self.last_pct and self.n < self.total:
            return
        self.last, self.last_pct = now, pct
        self.render(note)

    def render(self, note: str = ""):
        elapsed = time.time() - self.start
        rate = self.n / elapsed if elapsed > 0 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        filled = int(self.width * self.n / self.total)
        bar = "#" * filled + "." * (self.width - filled)
        line = (f"{self.label} [{bar}] {self.n}/{self.total} "
                f"{self.n * 100 / self.total:5.1f}%  {rate:6.1f}/s  "
                f"eta {fmt_duration(eta)}")
        if note:
            line += f"  {note[:48]}"
        if self.tty:
            sys.stdout.write("\r" + line.ljust(140)[:140])
            sys.stdout.flush()
        else:
            print(line, flush=True)

    def close(self):
        self.render()
        if self.tty:
            sys.stdout.write("\n")
            sys.stdout.flush()


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
