from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

from librairy.config import Settings

MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 60
BACKOFF_CAP_SECONDS = 60.0


@dataclass(frozen=True)
class ChildSpec:
    name: str
    command: list[str]


class Supervisor:
    def __init__(
        self,
        specs: list[ChildSpec],
        *,
        popen=subprocess.Popen,
        sleep=time.sleep,
    ) -> None:
        self.specs = specs
        self.popen = popen
        self.sleep = sleep
        self.children: dict[str, subprocess.Popen] = {}
        self.restarts: dict[str, deque[float]] = {spec.name: deque() for spec in specs}
        self.stop_requested = False
        self.backoff = 0.5

    def request_stop(self, signum=None, frame=None) -> None:  # noqa: ARG002
        self.stop_requested = True
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()

    def run(self, *, iterations: int | None = None) -> int:
        for spec in self.specs:
            self._start(spec)
        loops = 0
        while not self.stop_requested:
            for spec in self.specs:
                child = self.children[spec.name]
                code = child.poll()
                if code is None:
                    continue
                if not self._record_restart(spec.name):
                    self._terminate_all()
                    return 1
                self.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, BACKOFF_CAP_SECONDS)
                self._start(spec)
            loops += 1
            if iterations is not None and loops >= iterations:
                break
            self.sleep(0.1)
        self._terminate_all()
        return 0

    def _start(self, spec: ChildSpec) -> None:
        self.children[spec.name] = self.popen(spec.command)

    def _record_restart(self, name: str) -> bool:
        now = time.time()
        restarts = self.restarts[name]
        restarts.append(now)
        while restarts and now - restarts[0] > RESTART_WINDOW_SECONDS:
            restarts.popleft()
        return len(restarts) <= MAX_RESTARTS

    def _terminate_all(self) -> None:
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.time() + 10
        for child in self.children.values():
            while child.poll() is None and time.time() < deadline:
                self.sleep(0.05)
            if child.poll() is None:
                child.kill()


def child_specs(settings: Settings) -> list[ChildSpec]:
    return [
        ChildSpec(
            "web",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "librairy.web.app:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                str(settings.dashboard_port),
            ],
        ),
        ChildSpec("worker", [sys.executable, "-m", "librairy", "worker"]),
    ]


def run_supervisor(settings: Settings) -> int:
    supervisor = Supervisor(child_specs(settings))
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m librairy run")
    parser.parse_args(argv)
    return run_supervisor(Settings())


if __name__ == "__main__":
    raise SystemExit(main())
