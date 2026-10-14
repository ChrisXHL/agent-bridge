"""Run the optional Feishu SDK in a separate local process."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from feishu_settings import load

LOG = logging.getLogger("codex_bridge.feishu_supervisor")
ROOT = Path(__file__).resolve().parent


class FeishuSupervisor:
    def __init__(self, config_path, state_path, bridge_url):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.bridge_url = bridge_url
        self.stop = threading.Event()
        self.thread = None
        self.process = None
        self.signature = None
        self.last_start = 0.0

    def status(self):
        if not self.config_path.exists():
            return "未配置"
        try:
            if not load(self.config_path).get("enabled"):
                return "已停用"
        except (OSError, ValueError):
            return "配置错误"
        return "进程运行中，待验证飞书连接" if self.process and self.process.poll() is None else "等待启动"

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _halt(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def _loop(self):
        while not self.stop.is_set():
            try:
                enabled = load(self.config_path).get("enabled", False)
                signature = self.config_path.stat().st_mtime_ns if self.config_path.exists() else None
                if not enabled:
                    self._halt()
                    self.signature = signature
                elif signature != self.signature:
                    self._halt()
                    self.signature = signature
                if enabled and (not self.process or self.process.poll() is not None):
                    if time.monotonic() - self.last_start >= 10:
                        self._launch()
            except Exception:
                LOG.exception("Feishu receiver supervision failed")
            self.stop.wait(2)
        self._halt()

    def _launch(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.config_path.parent / "feishu-worker.log"
        with log_path.open("ab") as log_file:
            self.process = subprocess.Popen(
                [sys.executable, str(ROOT / "feishu_worker.py"),
                 "--config", str(self.config_path),
                 "--state-db", str(self.state_path),
                 "--bridge-url", self.bridge_url],
                cwd=ROOT, stdout=log_file, stderr=log_file, close_fds=True,
            )
        log_path.chmod(0o600)
        self.last_start = time.monotonic()
        LOG.info("Feishu receiver started pid=%s", self.process.pid)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=7)
        else:
            self._halt()


class MultiFeishuSupervisor:
    """Exactly one receiver per application, with separate inboxes for new apps."""
    def __init__(self, config_path, state_path, bridge_url):
        self.config_path, self.state_path = Path(config_path), Path(state_path)
        self.bridge_url = bridge_url
        self.receivers = {}
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.RLock()

    def reconcile(self):
        from feishu_groups import application_paths
        seen = set()
        with self.lock:
            desired = {}
            for ref, path, inbox in application_paths(self.config_path, self.state_path):
                try: app_id = load(path).get('app_id')
                except (OSError, ValueError):
                    LOG.error('Invalid Feishu application configuration: %s', ref)
                    continue
                if not app_id or app_id in seen: continue
                seen.add(app_id)
                desired[ref] = (path, inbox)
            for ref in set(self.receivers) - set(desired):
                self.receivers.pop(ref).close()
            for ref, (path, inbox) in desired.items():
                if ref not in self.receivers:
                    child = FeishuSupervisor(path, inbox, self.bridge_url)
                    child.start(); self.receivers[ref] = child

    def status_for(self, ref):
        with self.lock:
            child = self.receivers.get(ref)
            return child.status() if child else '等待启动'

    def status(self): return self.status_for('default')

    def start(self):
        self.reconcile()
        def loop():
            while not self.stop.wait(2):
                try: self.reconcile()
                except Exception: LOG.exception('Feishu application discovery failed')
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
        with self.lock:
            for child in self.receivers.values():child.close()
