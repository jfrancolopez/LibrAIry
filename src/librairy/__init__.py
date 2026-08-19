"""LibrAIry.

`__version__` is the project's one version number, and `pyproject.toml` reads
it from here — `[tool.hatch.version] path` — rather than declaring a second.

It used to be the other way round: `pyproject.toml` held the number and this
module asked `importlib.metadata` for it. An editable install writes its
`dist-info` once and never looks at `pyproject.toml` again, so a checkout whose
project file said 1.2.0 reported 1.0.0 in the footer and from `--version` for
as long as nobody reinstalled it — while the container, built from a wheel,
reported 1.2.0 from the same source tree. The number a person saw depended on
how stale their install was, which is the one thing a version number may not
do.

A literal cannot go stale. `tests/test_release.py` holds every surface that
shows it against this line.
"""

from __future__ import annotations

__version__ = "1.2.0"
