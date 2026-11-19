"""Small helpers so every chapter harness prints results the same way."""
import sys


class Report:
    def __init__(self, title, max_shown=10):
        self.title, self.max_shown = title, max_shown
        self.failures = []
        self.checks = 0

    def check(self, ok, message):
        """Record one check. `message` is only shown on failure (be specific)."""
        self.checks += 1
        if not ok:
            self.failures.append(message)

    def compare(self, label, expected, actual):
        self.check(expected == actual, f"{label}: expected {expected!r}, got {actual!r}")

    def compare_rows(self, label, expected_rows, actual_rows, fmt=repr):
        """Compare two lists; report length mismatch and the first differing rows."""
        self.check(len(expected_rows) == len(actual_rows),
                   f"{label}: expected {len(expected_rows)} rows, got {len(actual_rows)}")
        for i, (e, a) in enumerate(zip(expected_rows, actual_rows)):
            self.check(e == a, f"{label} row {i}: expected {fmt(e)}, got {fmt(a)}")

    def finish(self):
        for m in self.failures[: self.max_shown]:
            print("  FAIL:", m)
        extra = len(self.failures) - self.max_shown
        if extra > 0:
            print(f"  ... and {extra} more failure(s)")
        ok = not self.failures
        print(f"{'PASS' if ok else 'FAIL'}: {self.title} ({self.checks - len(self.failures)}/{self.checks} checks)")
        sys.exit(0 if ok else 1)
