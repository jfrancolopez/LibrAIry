from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.supervisor import ChildSpec, Supervisor, child_specs


class FakeProcess:
    def __init__(self, codes: list[int | None]) -> None:
        self.codes = codes
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.terminated or self.killed:
            return 0
        if len(self.codes) > 1:
            return self.codes.pop(0)
        return self.codes[0]

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_child_specs_start_web_and_worker(tmp_path: Path) -> None:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", DASHBOARD_PORT=9090, _env_file=None)

    specs = child_specs(settings)

    assert specs[0].name == "web"
    assert "uvicorn" in specs[0].command
    assert "9090" in specs[0].command
    assert specs[1].command[-1] == "worker"


def test_supervisor_restarts_crashed_child() -> None:
    started: list[list[str]] = []
    processes = [FakeProcess([1]), FakeProcess([None]), FakeProcess([None])]

    def popen(command: list[str]):
        started.append(command)
        return processes.pop(0)

    supervisor = Supervisor(
        [ChildSpec("web", ["web"]), ChildSpec("worker", ["worker"])],
        popen=popen,
        sleep=lambda seconds: None,
    )

    assert supervisor.run(iterations=1) == 0
    assert started == [["web"], ["worker"], ["web"]]


def test_supervisor_stops_children_on_signal() -> None:
    processes = [FakeProcess([None]), FakeProcess([None])]

    supervisor = Supervisor(
        [ChildSpec("web", ["web"]), ChildSpec("worker", ["worker"])],
        popen=lambda command: processes.pop(0),
        sleep=lambda seconds: None,
    )

    supervisor.run(iterations=1)

    assert all(child.terminated for child in supervisor.children.values())


def test_flapping_child_exits_nonzero() -> None:
    def popen(command: list[str]):
        return FakeProcess([1])

    supervisor = Supervisor([ChildSpec("web", ["web"])], popen=popen, sleep=lambda seconds: None)

    assert supervisor.run(iterations=10) == 1


def test_no_executor_in_web_or_supervisor_request_path() -> None:
    assert "execute_plan" not in Path("src/librairy/supervisor.py").read_text(encoding="utf-8")
    assert "execute_plan" not in Path("src/librairy/web/app.py").read_text(encoding="utf-8")
