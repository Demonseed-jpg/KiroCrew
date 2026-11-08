"""The Harness installer (``browser_harness/install.py``) with a fake subprocess runner."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.browser_harness import HARNESS_VERSION, build_deps, chromium, install
from kiro_crew.security import paths_lists
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _no_old_glibc(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs as on a host whose glibc the locked wheels cover, unless it says
    otherwise: without the pin an old-glibc test host would refuse every install below."""
    _floor_monkeypatch.setattr(build_deps, "refusal_for_this_host", lambda: None)


#: The distributions the gateway's interpreter must carry for its own copy to be used.
_HARNESS_FAMILY = ("browser-harness", "cdp-use", "fetch-use")


@pytest.fixture(autouse=True)
def runtime_site(_floor_monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The site directories the harness family resolves from in the gateway's interpreter:
    none unless a test adds one, so a test venv that happens to carry the harness does
    not turn the pip-path tests below into runtime-copy ones."""
    dirs: list[Path] = []
    real = importlib.metadata.distribution

    def distribution(name: str) -> importlib.metadata.Distribution:
        if name not in _HARNESS_FAMILY:
            return real(name)
        for dist in importlib.metadata.distributions(name=name, path=[str(d) for d in dirs]):
            return dist
        raise importlib.metadata.PackageNotFoundError(name)

    _floor_monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    return dirs


def _lock_pin(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}==(\S+) \\$", install.LOCK_FILE.read_text(encoding="utf-8"), re.M
    )
    assert match, name
    return match.group(1)


def _runtime_ships(site: Path, **versions: str) -> Path:
    """A site directory carrying the harness family at the lock's pins; a keyword
    (``cdp_use="9.9.9"``) moves one off its pin, an empty one leaves it out."""
    for name in _HARNESS_FAMILY:
        version = versions.get(name.replace("-", "_"), _lock_pin(name))
        if not version:
            continue
        info = site / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
        )
    return site


def _ok(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # ``python -m venv <prefix>/venv`` creates the interpreter the next step runs;
    # ``--clear`` empties the directory first.
    if argv[1:4] == ["-I", "-m", "venv"]:
        if "--clear" in argv:
            shutil.rmtree(argv[-1], ignore_errors=True)
        python = install.venv_python(Path(argv[-1]).parent)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _gateway_ships(monkeypatch, *present: str) -> None:
    """Pin which of ``pip`` / ``ensurepip`` the gateway interpreter can import."""
    real = importlib.util.find_spec

    def find_spec(name: str, *args, **kwargs):
        if name in ("pip", "ensurepip"):
            return real("json") if name in present else None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def test_the_lock_pins_the_harness_release_with_hashes_on_every_line() -> None:
    text = install.LOCK_FILE.read_text(encoding="utf-8")
    assert f"browser-harness=={HARNESS_VERSION}" in text
    requirements = [ln for ln in text.splitlines() if ln and not ln.startswith(("#", " ", "-"))]
    assert requirements, "the lock names no requirement"
    for req in requirements:
        assert re.match(r"^[A-Za-z0-9_.-]+==[0-9][^ ]* \\$", req), req
    assert text.count("--hash=sha256:") >= len(requirements)


def test_absent_home_reads_as_not_installed(tmp_path: Path) -> None:
    state = install.install_state(tmp_path)
    assert state.installed is False
    assert state.version is None and state.python is None
    assert state.prefix == tmp_path / install.PREFIX_LEAF
    assert state.as_dict()["wanted_version"] == HARNESS_VERSION


def test_install_runs_venv_then_hash_pinned_pip_and_writes_the_marker(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    monkeypatch.setattr(
        chromium,
        "download_chromium",
        lambda prefix, progress=None, **kw: chromium.chromium_state(prefix),
    )
    # The packaged app's interpreter: ``pip`` present (pinned, not the test venv's).
    _gateway_ships(monkeypatch, "pip")
    stages: list[str] = []
    state = install.install_sync(tmp_path, lambda s, d: stages.append(s), runner=runner)
    prefix = tmp_path / install.PREFIX_LEAF
    # Both interpreters run isolated (``-I``): the gateway's cwd (an agent's
    # workspace on a pod) and ``PYTHONPATH`` never reach ``sys.path``, so a
    # ``venv/`` or ``pip/`` package written there cannot shadow the real one.
    assert calls[0][:5] == [sys.executable, "-I", "-m", "venv", "--clear"]
    # ``--clear`` empties the directory it is given: the venv's own, never the
    # prefix that also holds the downloaded Chromium.
    assert calls[0][-1] == str(install.venv_dir(prefix)) == str(prefix / "venv")
    # The packaged interpreter has no ``ensurepip``: the venv is made without pip
    # and the gateway's pip installs into it.
    assert "--without-pip" in calls[0]
    assert str(install.venv_python(prefix)).startswith(str(prefix / "venv"))
    assert calls[1][:7] == [
        sys.executable,
        "-I",
        "-m",
        "pip",
        "--python",
        str(install.venv_python(prefix)),
        "install",
    ]
    assert "--require-hashes" in calls[1] and calls[1][-1] == str(install.LOCK_FILE)
    marker = json.loads((prefix / install.MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["version"] == HARNESS_VERSION
    assert marker["lock_sha256"] == install.lock_digest()
    assert state.installed is True and state.version == HARNESS_VERSION
    assert stages[:3] == [
        install.STAGE_PREPARING,
        install.STAGE_CREATING_VENV,
        install.STAGE_INSTALLING_HARNESS,
    ]
    assert stages[-1] == install.STAGE_FINISHING


def test_without_gateway_pip_the_venv_bootstraps_its_own_and_installs_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    # The Toolbox runtime ships ``ensurepip`` but no ``pip`` module.
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    prefix = tmp_path / install.PREFIX_LEAF
    # A finished earlier install made ``--without-pip``: the venv is still rebuilt.
    (prefix / "venv").mkdir(parents=True)
    install.venv_python(prefix).parent.mkdir(parents=True, exist_ok=True)
    install.venv_python(prefix).write_text("", encoding="utf-8")
    (prefix / install.MARKER_FILE).write_text(
        json.dumps({"version": HARNESS_VERSION}), encoding="utf-8"
    )
    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--clear",
        str(install.venv_dir(prefix)),
    ]
    assert calls[1][:5] == [str(install.venv_python(prefix)), "-I", "-m", "pip", "install"]
    assert "--python" not in calls[1] and calls[1][-1] == str(install.LOCK_FILE)


def _current_install(prefix: Path, *, venv_pip: bool) -> None:
    """A finished install on disk: interpreter, marker, and optionally the venv's pip."""
    install.venv_python(prefix).parent.mkdir(parents=True, exist_ok=True)
    install.venv_python(prefix).write_text("", encoding="utf-8")
    if venv_pip:
        lib = "Lib" if install.IS_WINDOWS else "lib/python3.12"
        (install.venv_dir(prefix) / lib / "site-packages" / "pip").mkdir(parents=True)
    (prefix / install.MARKER_FILE).write_text(
        json.dumps({"version": HARNESS_VERSION}), encoding="utf-8"
    )


def test_without_gateway_pip_a_venv_with_its_own_pip_is_kept(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=True)
    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert len(calls) == 1 and calls[0][:4] == [str(install.venv_python(prefix)), "-I", "-m", "pip"]


def _pruned_install(prefix: Path, *, minor: int) -> Path:
    """A finished install whose base Python is gone: the interpreter links dangle.

    The venv was built by Python 3.<minor> and holds its own pip and the locked
    packages; returns a sentinel file in its site-packages.
    """
    venv = install.venv_dir(prefix)
    lib = "Lib" if install.IS_WINDOWS else f"lib/python3.{minor}"
    site = venv / lib / "site-packages"
    (site / "pip").mkdir(parents=True)
    (site / "browser_harness").mkdir()
    sentinel = site / "browser_harness" / "__init__.py"
    sentinel.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text(
        f"home = {prefix.parent / 'pruned'}\nversion = 3.{minor}.4\n", encoding="utf-8"
    )
    bindir = install.venv_python(prefix).parent
    bindir.mkdir(parents=True)
    gone = prefix.parent / "pruned" / f"python3.{minor}"
    for name in ("python", "python3", f"python3.{minor}"):
        (bindir / name).symlink_to(gone)
    (prefix / install.MARKER_FILE).write_text(
        json.dumps({"version": HARNESS_VERSION, "lock_sha256": install.lock_digest()}),
        encoding="utf-8",
    )
    return sentinel


@requires_symlinks
@pytest.mark.parametrize("gateway_has", ["pip", "ensurepip"])
def test_a_venv_whose_base_python_was_removed_is_repaired_in_place(
    tmp_path: Path, monkeypatch, gateway_has: str
) -> None:
    """Toolbox prunes the version the venv was built on: the links dangle, the packages
    stay. The venv is re-pointed at this interpreter instead of cleared, so pip finds
    every pin already there and needs no index."""
    _gateway_ships(monkeypatch, gateway_has)
    prefix = tmp_path / install.PREFIX_LEAF
    sentinel = _pruned_install(prefix, minor=sys.version_info[1])
    assert install.install_state(tmp_path).installed is False
    assert install.harness_current(tmp_path) is False
    calls: list[list[str]] = []
    links_at_venv: list[str] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:4] == ["-I", "-m", "venv"]:
            bindir = install.venv_python(prefix).parent
            links_at_venv.extend(p.name for p in bindir.iterdir() if p.is_symlink())
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--upgrade",
        "--without-pip",
        str(install.venv_dir(prefix)),
    ]
    # ``venv --upgrade`` keeps an entry that is already a symlink, so the dangling
    # ones are gone before it runs.
    assert links_at_venv == []
    assert "pip" in calls[1] and calls[1][-1] == str(install.LOCK_FILE)
    assert len(calls) == 2
    assert sentinel.is_file()
    marker = json.loads((prefix / install.MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["python"] == str(install.venv_python(prefix))
    assert install.harness_current(tmp_path) is True


@requires_symlinks
def test_a_venv_built_by_another_python_minor_is_still_rebuilt(tmp_path: Path, monkeypatch) -> None:
    """Its compiled wheels only fit the minor version that built it."""
    _gateway_ships(monkeypatch, "ensurepip")
    prefix = tmp_path / install.PREFIX_LEAF
    _pruned_install(prefix, minor=sys.version_info[1] - 1)
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0][:5] == [sys.executable, "-I", "-m", "venv", "--clear"]


@requires_symlinks
def test_a_pruned_venv_linked_to_a_runtime_copy_is_rebuilt_not_repaired(
    tmp_path: Path, monkeypatch
) -> None:
    """The runtime stopped shipping the harness and the venv's base Python is gone:
    repairing it in place would keep the runtime's copy on its path behind the lock's."""
    _gateway_ships(monkeypatch, "pip")
    prefix = tmp_path / install.PREFIX_LEAF
    _pruned_install(prefix, minor=sys.version_info[1])
    pth = install._venv_site_packages(prefix) / install.RUNTIME_PTH_FILE
    pth.write_text(f"{tmp_path / 'runtime'}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0][:6] == [sys.executable, "-I", "-m", "venv", "--clear", "--without-pip"]
    assert not pth.exists()


def test_a_rebuild_whose_pip_fails_reads_as_not_installed(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch, "ensurepip")

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "pip" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "network unreachable")
        return _ok(argv, timeout)

    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=False)
    assert install.install_state(tmp_path).installed is True
    with pytest.raises(install.InstallError):
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert install.install_state(tmp_path).installed is False


def test_neither_pip_nor_ensurepip_fails_before_any_spawn(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected spawn: {argv}")

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_PREPARING
    assert "ensurepip" in excinfo.value.detail


def test_a_failing_pip_raises_with_the_stage_and_the_output_tail(
    tmp_path: Path, monkeypatch
) -> None:
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "pip" in argv:
            return subprocess.CompletedProcess(
                argv, 1, "", "THESE PACKAGES DO NOT MATCH THE HASHES"
            )
        return _ok(argv, timeout)

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_INSTALLING_HARNESS
    assert "HASHES" in excinfo.value.detail
    assert install.install_state(tmp_path).installed is False


def test_a_timed_out_step_is_an_install_error(tmp_path: Path) -> None:
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_CREATING_VENV


def test_chromium_only_skips_the_venv(tmp_path: Path, monkeypatch) -> None:
    seen: list[Path] = []

    def fake_download(prefix: Path, progress=None, **kw) -> chromium.ChromiumState:
        seen.append(prefix)
        return chromium.chromium_state(prefix)

    monkeypatch.setattr(chromium, "download_chromium", fake_download)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise AssertionError("no subprocess for a chromium-only install")

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_CHROMIUM,))
    assert seen == [tmp_path / install.PREFIX_LEAF]


def test_a_chromium_download_failure_is_an_install_error(tmp_path: Path, monkeypatch) -> None:
    def boom(prefix: Path, progress=None, **kw) -> chromium.ChromiumState:
        raise chromium.ChromiumDownloadError("offline")

    monkeypatch.setattr(chromium, "download_chromium", boom)
    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, components=(install.COMPONENT_CHROMIUM,))
    assert excinfo.value.stage == chromium.STAGE_DOWNLOADING


def test_the_prefix_leaf_is_spelled_the_same_in_every_fence() -> None:
    leaf = install.PREFIX_LEAF
    assert leaf == "browser-harness"
    assert leaf in sandbox_leaves._CREW_READONLY_LEAVES
    assert leaf in sandbox_leaves._CREW_CHILD_WITHHELD_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._DELEGATED_OVERLAP_LEAF_REASONS
    assert leaf in paths_lists._CREW_SECRET_LEAVES


def test_the_stage_order_covers_chromium_too() -> None:
    assert chromium.STAGE_DOWNLOADING in install.STAGES
    assert (
        install.STAGES[0] == install.STAGE_PREPARING
        and install.STAGES[-1] == install.STAGE_FINISHING
    )


def test_the_installers_children_get_a_scrubbed_env_and_the_prefix_as_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """The second and third fence beside ``-I``: no ``PYTHON*``/``PIP_*`` from the
    gateway's shell (``PYTHONPATH``, ``PIP_INDEX_URL``, a ``PIP_CONFIG_FILE``), and a
    working directory that is the owner-only prefix, never the gateway's own cwd."""
    env = install._child_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/o",
            "HTTPS_PROXY": "http://proxy:3128",
            "PYTHONPATH": "/workspace/evil",
            "PYTHONSTARTUP": "/workspace/evil/startup.py",
            "PIP_INDEX_URL": "http://mirror.evil/simple",
            "pip_config_file": "/workspace/pip.conf",
        }
    )
    assert "PATH" in env and "HOME" in env and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert not any(
        k.upper().startswith(("PYTHON", "PIP_"))
        for k in env
        if k not in ("PYTHONIOENCODING", "PIP_NO_INPUT", "PIP_CONFIG_FILE")
    )
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PIP_NO_INPUT"] == "1"
    # The null device, not the inherited file: pip then loads no config file at all.
    assert env["PIP_CONFIG_FILE"] == os.devnull

    seen: dict[str, object] = {}

    class FakeChild:
        returncode = 0

        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(kwargs)

        def communicate(self, timeout):  # type: ignore[no-untyped-def]
            return "", ""

    monkeypatch.setattr(install.subprocess, "Popen", FakeChild)
    prefix = tmp_path / install.PREFIX_LEAF
    install._run([sys.executable, "-I", "-c", "pass"], 1.0, cwd=prefix)
    assert seen["cwd"] == str(prefix)
    assert "PYTHONPATH" not in seen["env"]  # type: ignore[operator]


def test_the_installers_pip_reads_no_index_from_the_owners_pip_config(
    tmp_path: Path, monkeypatch
) -> None:
    """A real pip under the installer's own spawn never sees the owner's pip config.

    An ``index-url`` there (a CodeArtifact repository whose login expired answers 401
    for every locked wheel) would otherwise replace the index the lock was audited
    against, and the Settings page would show pip's raw failure.
    """
    if importlib.util.find_spec("pip") is None:
        pytest.skip("this interpreter ships no pip")
    owner_home = tmp_path / "owner"
    index = "https://owner-pip-config.invalid/simple/"
    if install.IS_WINDOWS:
        conf = owner_home / "pip" / "pip.ini"
    else:
        conf = owner_home / ".pip" / "pip.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"[global]\nindex-url = {index}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("USERPROFILE", str(owner_home))
    argv = [sys.executable, "-I", "-m", "pip", "config", "list"]
    # The file is one pip loads: with the installer's environment minus its
    # config-file override, pip reports the owner's index.
    control_env = {k: v for k, v in install._child_env().items() if k != "PIP_CONFIG_FILE"}
    control = subprocess.run(
        argv,
        cwd=str(tmp_path),
        env=control_env,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert index in control.stdout, control.stdout + control.stderr
    prefix = tmp_path / install.PREFIX_LEAF
    prefix.mkdir()
    result = install._run(argv, FALLBACK_TIMEOUT_SECS, cwd=prefix)
    assert result.returncode == 0, result.stderr
    assert index not in result.stdout + result.stderr


def _old_glibc_missing(monkeypatch, *missing: str) -> None:
    text = build_deps.refusal(list(missing), glibc=(2, 26))
    monkeypatch.setattr(build_deps, "refusal_for_this_host", lambda: text)


def test_an_old_glibc_host_without_the_build_deps_is_refused_before_any_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _old_glibc_missing(monkeypatch, "jpeglib.h")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    with pytest.raises(install.InstallError) as info:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert info.value.stage == install.STAGE_PREPARING
    assert "pillow 12.3.0" in info.value.detail and "jpeglib.h" in info.value.detail
    assert calls == []


def test_a_refused_rebuild_leaves_the_existing_install_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    """The refusal comes before the marker is removed and the venv cleared."""
    _gateway_ships(monkeypatch, "ensurepip")
    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=False)
    assert install.install_state(tmp_path).installed is True
    _old_glibc_missing(monkeypatch, build_deps.MISSING_COMPILER)
    with pytest.raises(install.InstallError):
        install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    assert install.install_state(tmp_path).installed is True


def test_a_venv_that_already_holds_the_locked_pillow_needs_no_build(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-pressing Install on a host that once built pillow does not ask for a compiler."""
    _gateway_ships(monkeypatch, "ensurepip")
    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=True)
    lib = "Lib" if install.IS_WINDOWS else "lib/python3.12"
    dist_info = f"pillow-{build_deps.LOCKED_PILLOW}.dist-info"
    (install.venv_dir(prefix) / lib / "site-packages" / dist_info).mkdir(parents=True)
    _old_glibc_missing(monkeypatch, build_deps.MISSING_COMPILER, "jpeglib.h")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert len(calls) == 1 and "pip" in calls[0]


def _recording_runner(calls: list[list[str]]):
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    return runner


def test_a_runtime_that_ships_the_pinned_harness_is_linked_and_pip_never_runs(
    tmp_path: Path, monkeypatch, runtime_site: list[Path]
) -> None:
    """The gateway's interpreter already carries the harness at the lock's pins (a
    distribution that bundles it): pip, and so the host's index, is never asked, which is
    what lets a host whose index refuses an expired credential install at all."""
    site = _runtime_ships(tmp_path / "runtime" / "site-packages")
    runtime_site.append(site)
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []
    home = tmp_path / "home"
    state = install.install_sync(
        home, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    prefix = install.harness_prefix(home)
    assert not [argv for argv in calls if "pip" in argv], calls
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--clear",
        "--without-pip",
        str(install.venv_dir(prefix)),
    ]
    # The venv's own interpreter proves it imports the copy at the lock's pins.
    assert calls[1][:3] == [str(install.venv_python(prefix)), "-I", "-c"]
    assert calls[1][4:] == [f"{name}=={_lock_pin(name)}" for name in _HARNESS_FAMILY]
    assert len(calls) == 2
    pth = install._venv_site_packages(prefix) / install.RUNTIME_PTH_FILE
    assert pth.read_text(encoding="utf-8") == f"{site}\n"
    assert state.installed is True and install.harness_current(home) is True


def test_a_runtime_copy_needs_neither_pip_nor_the_pillow_build_deps(
    tmp_path: Path, monkeypatch, runtime_site: list[Path]
) -> None:
    """Nothing is installed or built from the runtime's copy, so an interpreter without pip
    or ensurepip, on an old glibc without pillow's build deps, is not refused."""
    runtime_site.append(_runtime_ships(tmp_path / "runtime"))
    _gateway_ships(monkeypatch)
    _old_glibc_missing(monkeypatch, build_deps.MISSING_COMPILER, "jpeglib.h")
    state = install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    assert state.installed is True


@pytest.mark.parametrize("off_lock", [{"cdp_use": "0.0.1"}, {"fetch_use": ""}])
def test_a_runtime_off_the_lock_keeps_the_hash_pinned_pip_install(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], off_lock: dict[str, str]
) -> None:
    runtime_site.append(_runtime_ships(tmp_path / "runtime", **off_lock))
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    prefix = install.harness_prefix(tmp_path)
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--clear",
        str(install.venv_dir(prefix)),
    ]
    assert calls[1][:5] == [str(install.venv_python(prefix)), "-I", "-m", "pip", "install"]
    assert "--require-hashes" in calls[1] and calls[1][-1] == str(install.LOCK_FILE)
    assert not list(install.venv_dir(prefix).rglob("*.pth"))


def test_a_change_of_source_rebuilds_the_venv_and_a_repeat_keeps_it(
    tmp_path: Path, monkeypatch, runtime_site: list[Path]
) -> None:
    """A venv pip filled would shadow the runtime's copy from its own site-packages, and a
    linked one would keep the runtime's copy behind the lock's: either is rebuilt when the
    source changes. Pressing Install again on a linked venv only re-checks it."""
    prefix = install.harness_prefix(tmp_path)
    _current_install(prefix, venv_pip=True)
    runtime_site.append(_runtime_ships(tmp_path / "runtime"))
    _gateway_ships(monkeypatch, "pip")
    harness_only = (install.COMPONENT_HARNESS,)

    calls: list[list[str]] = []
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    assert calls[0][3:6] == ["venv", "--clear", "--without-pip"] and len(calls) == 2

    calls.clear()
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    assert len(calls) == 1 and calls[0][1:3] == ["-I", "-c"]

    runtime_site.clear()
    calls.clear()
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    assert calls[0][3:5] == ["venv", "--clear"] and calls[1][2:4] == ["-m", "pip"]


@pytest.mark.parametrize("update", ["moved", "gone", "off_lock"])
def test_a_linked_venv_whose_runtime_copy_an_update_moved_is_not_current(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], update: str
) -> None:
    """An update can prune the tree holding the runtime's copy, install it in a new tree, or
    ship it at other pins, while the venv's interpreter, the marker and the lock stay as they
    were. The startup check reads that venv as stale, so the next start relinks it offline,
    or rebuilds it from the lock, instead of leaving a venv that cannot import the harness."""
    old = _runtime_ships(tmp_path / "crew-venv-0.13.0" / "site-packages")
    runtime_site.append(old)
    _gateway_ships(monkeypatch, "ensurepip")
    harness_only = (install.COMPONENT_HARNESS,)
    install.install_sync(tmp_path, runner=_ok, components=harness_only)
    assert install.harness_current(tmp_path) is True

    runtime_site.clear()
    shutil.rmtree(old)
    new = tmp_path / "crew-venv-0.14.0" / "site-packages"
    if update == "moved":
        runtime_site.append(_runtime_ships(new))
    elif update == "off_lock":
        runtime_site.append(_runtime_ships(new, cdp_use="0.0.1"))
    assert install.install_state(tmp_path).installed is True
    assert install.harness_current(tmp_path) is False

    calls: list[list[str]] = []
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    pth = install._venv_site_packages(install.harness_prefix(tmp_path)) / install.RUNTIME_PTH_FILE
    if update == "moved":
        assert len(calls) == 1 and calls[0][1:3] == ["-I", "-c"], calls
        assert pth.read_text(encoding="utf-8") == f"{new}\n"
    else:
        assert calls[0][3:5] == ["venv", "--clear"] and calls[1][2:4] == ["-m", "pip"], calls
        assert not pth.exists()
    assert install.harness_current(tmp_path) is True
