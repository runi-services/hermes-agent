"""Install a reviewed sender-only module overlay without changing shared source.

Call before platform/tool discovery in a named runtime launcher. Only the exact
upstream module origin is substituted; protocol, receiver and other tools remain native.
"""
import importlib.abc
import importlib.machinery
import importlib.util
from pathlib import Path


def install_sender_overlay(upstream_file, corrected_file):
    upstream = Path(upstream_file).resolve(strict=True)
    corrected = Path(corrected_file).resolve(strict=True)

    class SenderOverlay(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.endswith('.tools'):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or not spec.origin:
                return None
            if Path(spec.origin).resolve() != upstream:
                return None
            return importlib.util.spec_from_file_location(fullname, corrected)

    import sys
    finder = SenderOverlay()
    sys.meta_path.insert(0, finder)
    return finder
