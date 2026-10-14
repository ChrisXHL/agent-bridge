"""Use the desktop's bundled Codex, keeping its model/account support aligned."""
import os
import shutil
from pathlib import Path


def resolve_codex_binary(explicit=None):
    if explicit and explicit != 'codex':
        return explicit
    for base in (Path('/Applications'), Path.home() / 'Applications'):
        for suffix in (
            'ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
            'Codex.app/Contents/Resources/codex',
        ):
            candidate = base / suffix
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    launcher = shutil.which('codex') or 'codex'
    # Avoid a Node launcher when the standalone native binary is available.
    package = Path(launcher).resolve().parent.parent
    native = package / 'node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex'
    return str(native) if native.is_file() and os.access(native, os.X_OK) else launcher
