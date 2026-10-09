"""Reserve stdout for MCP protocol bytes before loading application services."""
import logging
import sys


class _StderrProxy:
    def __init__(self, real_stdout, stderr):
        self._stderr = stderr
        self.buffer = real_stdout.buffer

    def write(self, data):
        return self._stderr.write(data)

    def flush(self):
        return self._stderr.flush()

    def __getattr__(self, name):
        return getattr(self._stderr, name)


def enforce_stdio_stdout_hygiene() -> None:
    """Redirect diagnostics, preserving the binary stream used by the SDK."""
    real_stdout = sys.stdout
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is real_stdout:
            handler.setStream(sys.stderr)
    if not isinstance(real_stdout, _StderrProxy):
        sys.stdout = _StderrProxy(real_stdout, sys.stderr)
