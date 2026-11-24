"""检查并安装官方 Trina 更新。"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Callable

from . import __version__
from .scope import execute_streamed, reinstall_after_launcher_exit
from .text import clean_text

MIRRORS = {
    "github": "https://github.com/YHalo-wyh/Trina.git",
    "gitee": "https://gitee.com/YHalo/Trina.git",
}
_COMMIT = re.compile(r"[0-9a-f]{40}")
_RELEASE = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)")


class UpdateError(RuntimeError):
    """更新失败，由命令入口报告具体原因。"""


def _run(argv: list[str], cwd: Path, *, timeout: int = 30, stream=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", PYTHONIOENCODING="utf-8")
    try:
        return execute_streamed(argv, timeout=timeout, cwd=str(cwd), env=env, input_data=None,
                                on_output=stream or (lambda _kind, _text: None))
    except FileNotFoundError as exc:
        name = "Git" if argv[0] == "git" else "当前 Python"
        raise UpdateError(f"找不到{name}，无法执行更新") from exc
    except (OSError, TimeoutExpired) as exc:
        raise UpdateError(f"启动命令失败或超时 · {exc}") from exc


def _output(result) -> str:
    if result.returncode:
        raise UpdateError(clean_text(result.stderr.strip() or result.stdout.strip()) or
                          f"命令退出码 {result.returncode}")
    return result.stdout.strip()


def _remote_name(root: Path) -> str:
    result = _run(["git", "remote", "get-url", "origin"], root)
    origin = result.stdout.strip().casefold().removesuffix("/")
    for name, url in MIRRORS.items():
        path = url.removeprefix("https://").casefold().removesuffix(".git")
        host, _, repo = path.partition("/")
        if origin.removesuffix(".git") in {f"https://{path}", f"git@{host}:{repo}", f"ssh://git@{path}"}:
            return name
    return "github"


def _remote_refs(result) -> dict[str, str]:
    refs = {}
    for line in _output(result).splitlines():
        sha, sep, ref = line.partition("\t")
        if sep and _COMMIT.fullmatch(sha):
            refs[ref] = sha
    return refs


def _checkout_relation(root: Path, latest: str) -> str:
    """只比较本地已有对象，供检查和快进前验证共用。"""
    if _run(["git", "cat-file", "-e", f"{latest}^{{commit}}"], root).returncode:
        return "unknown"
    ahead = _run(["git", "merge-base", "--is-ancestor", latest, "HEAD"], root)
    if ahead.returncode == 0:
        return "ahead"
    if ahead.returncode != 1:
        _output(ahead)
    behind = _run(["git", "merge-base", "--is-ancestor", "HEAD", latest], root)
    if behind.returncode == 1:
        return "diverged"
    _output(behind)
    return "behind"


def _update_checkout(root: Path, mirror: str, check: bool, emit: Callable[[str], None]) -> None:
    url = MIRRORS[mirror]
    before = _output(_run(["git", "rev-parse", "--verify", "HEAD"], root))
    if not check and _output(_run(["git", "branch", "--show-current"], root)) != "main":
        raise UpdateError("当前源码分支不是 main。请在 main 上执行更新，或用 --check 查看远端。")
    refs = _remote_refs(_run(["git", "ls-remote", "--exit-code", url, "refs/heads/main"], root))
    latest = refs.get("refs/heads/main")
    if not latest:
        raise UpdateError("镜像没有返回有效的 main 提交")
    emit(f"当前 {before[:10]} · {mirror}/main {latest[:10]}")
    if before == latest:
        emit("源码已是最新。")
        return
    relation = _checkout_relation(root, latest)
    if relation == "ahead":
        emit("本地已包含远端提交。")
        return
    if check:
        emit("本地 main 与远端已分叉，无法快进。" if relation == "diverged" else
             "远端提交不同。运行 trina update 可尝试快进更新。")
        return
    if relation == "unknown":
        _output(_run(["git", "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", url, latest], root,
                     timeout=180))
        relation = _checkout_relation(root, latest)
    if _output(_run(["git", "branch", "--show-current"], root)) != "main":
        raise UpdateError("更新期间切换了分支，请重新运行 trina update")
    if relation == "ahead":
        emit("本地已包含远端提交。")
        return
    if relation == "diverged":
        raise UpdateError("本地 main 与远端已分叉，无法快进。")
    if relation != "behind":
        raise UpdateError("未能读取远端提交")
    _output(_run(["git", "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", latest], root))
    emit(f"源码已更新到 {latest[:10]}。重新运行 trina 即可使用。")


def _latest_release(refs: dict[str, str]) -> tuple[tuple[int, ...], str, str]:
    releases = []
    for ref, sha in refs.items():
        match = _RELEASE.fullmatch(ref)
        if match:
            releases.append((tuple(map(int, match.groups())), ref.removeprefix("refs/tags/"), refs.get(ref + "^{}", sha)))
    if not releases:
        raise UpdateError("镜像没有正式版本标签")
    return max(releases)


def _update_package(mirror: str, check: bool, emit: Callable[[str], None]) -> None:
    url = MIRRORS[mirror]
    with tempfile.TemporaryDirectory(prefix="trina-update-") as tmp:
        work = Path(tmp)
        refs = _remote_refs(_run(["git", "ls-remote", "--tags", url, "refs/tags/v*"], work))
        version, tag, sha = _latest_release(refs)
        current = _RELEASE.fullmatch(f"refs/tags/v{__version__}")
        if not current:
            raise UpdateError("无法识别当前安装的版本号")
        emit(f"当前 v{__version__} · {mirror} 正式版 {tag}")
        if version <= tuple(map(int, current.groups())):
            emit("安装版本已是最新。")
            return
        if check:
            emit(f"发现新版 {tag}。运行 trina update 安装。")
            return
        argv = [sys.executable, "-m", "pip", "--disable-pip-version-check", "--no-input",
                "install", "--upgrade", f"trina @ git+{url}@{sha}"]
        if os.name == "nt" and Path(sys.argv[0]).name.casefold() in {"trina", "trina.exe"}:
            try:
                reinstall_after_launcher_exit(argv)
            except OSError as exc:
                raise UpdateError("无法移交安装进程，请用 python -m trina update") from exc
            emit("已启动安装进程。安装将在当前命令退出后继续，请查看安装结果。")
            return
        _output(_run(argv, work, timeout=600, stream=lambda _kind, chunk: print(clean_text(chunk), end="", flush=True)))
        emit(f"已安装 {tag}。重新运行 trina 即可使用。")


def update(*, check: bool, mirror: str | None, emit: Callable[[str], None] = print) -> None:
    package = Path(__file__).resolve().parent
    root = package.parent
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        _update_checkout(root, mirror or _remote_name(root), check, emit)
        return
    try:
        dist = metadata.distribution("trina")
    except metadata.PackageNotFoundError:
        dist = None
    if dist is None or Path(dist.locate_file("trina/__init__.py")).resolve() != package / "__init__.py":
        raise UpdateError("当前从源码副本运行。请使用 Git 检出目录或通过 pip 安装。")
    _update_package(mirror or "github", check, emit)
