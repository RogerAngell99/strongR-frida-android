#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, message: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def log(message: str) -> None:
    print(message, flush=True)


def add_mask(secret: str | None) -> None:
    if secret:
        print(f"::add-mask::{secret}", flush=True)


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
    display_cmd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    shown = display_cmd or " ".join(shlex.quote(part) for part in cmd)
    log(f"+ {shown}")
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )
    if check and completed.returncode != 0:
        raise CommandError(
            f"Command failed with exit code {completed.returncode}: {shown}",
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    return completed


def git(
    repo_dir: Path,
    *args: str,
    check: bool = True,
    display_cmd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo_dir, check=check, display_cmd=display_cmd)


def git_output(repo_dir: Path, *args: str) -> str:
    return git(repo_dir, *args).stdout.strip()


def clone_repo(url: str, dest: Path, *, display_url: str | None = None) -> None:
    shown = f"git clone --filter=blob:none {display_url or url} {dest.name}"
    run(
        ["git", "clone", "--filter=blob:none", url, str(dest)],
        display_cmd=shown,
    )


def remote_branch_exists(repo_dir: Path, remote: str, branch: str) -> bool:
    result = git(repo_dir, "ls-remote", "--heads", remote, branch, check=False)
    return bool(result.stdout.strip())


def fetch_branch(repo_dir: Path, remote: str, branch: str) -> None:
    git(
        repo_dir,
        "fetch",
        remote,
        f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
    )


def fetch_tag(repo_dir: Path, remote: str, tag: str) -> None:
    git(
        repo_dir,
        "fetch",
        "--no-tags",
        remote,
        f"refs/tags/{tag}:refs/tags/{tag}",
    )


def write_output(path: str | None, name: str, value: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def configure_identity(repo_dir: Path) -> None:
    git(repo_dir, "config", "user.name", "github-actions[bot]")
    git(
        repo_dir,
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )


def commit_range(repo_dir: Path, upstream_tag: str, base_branch: str) -> list[str]:
    output = git_output(
        repo_dir,
        "rev-list",
        "--reverse",
        f"refs/tags/{upstream_tag}..refs/remotes/origin/{base_branch}",
    )
    commits = [line.strip() for line in output.splitlines() if line.strip()]
    return commits


def start_branch_from_tag(repo_dir: Path, branch: str, tag: str) -> None:
    git(repo_dir, "checkout", "--detach", f"refs/tags/{tag}")
    git(repo_dir, "switch", "-C", branch)


def cherry_pick_range(repo_dir: Path, commits: list[str], target_branch: str) -> None:
    for commit in commits:
        try:
            git(repo_dir, "cherry-pick", "-x", commit)
        except CommandError as exc:
            conflicted = git_output(
                repo_dir,
                "diff",
                "--name-only",
                "--diff-filter=U",
            )
            status = git_output(repo_dir, "status", "--short")
            git(repo_dir, "cherry-pick", "--abort", check=False)
            details = [f"Unable to cherry-pick {commit} into {target_branch}."]
            if conflicted:
                details.append(f"Conflicted files: {conflicted}")
            if status:
                details.append(f"Git status:\n{status}")
            raise CommandError(
                "\n".join(details),
                stdout=exc.stdout,
                stderr=exc.stderr,
            ) from exc


def build_repo_url(owner: str, repo: str, token: str | None = None) -> str:
    if token:
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    return f"https://github.com/{owner}/{repo}.git"


def prepare_frida_core(
    workspace: Path,
    *,
    fork_owner: str,
    token: str | None,
    base_version: str,
    target_version: str,
    target_branch: str,
    dry_run: bool,
) -> tuple[str, bool]:
    repo_dir = workspace / "frida-core"
    clone_repo(
        build_repo_url(fork_owner, "frida-core", token),
        repo_dir,
        display_url=f"https://github.com/{fork_owner}/frida-core.git",
    )
    git(repo_dir, "remote", "add", "upstream", "https://github.com/frida/frida-core.git")
    configure_identity(repo_dir)

    if remote_branch_exists(repo_dir, "origin", target_branch):
        fetch_branch(repo_dir, "origin", target_branch)
        core_head = git_output(repo_dir, "rev-parse", f"refs/remotes/origin/{target_branch}")
        log(f"frida-core branch {target_branch} already exists at {core_head}")
        return core_head, False

    base_branch = f"strongr-{base_version}"
    if not remote_branch_exists(repo_dir, "origin", base_branch):
        raise RuntimeError(
            f"Missing baseline branch {base_branch} in {fork_owner}/frida-core."
        )

    fetch_branch(repo_dir, "origin", base_branch)
    fetch_tag(repo_dir, "upstream", base_version)
    fetch_tag(repo_dir, "upstream", target_version)

    commits = commit_range(repo_dir, base_version, base_branch)
    if not commits:
        raise RuntimeError(
            f"No patch commits found in {fork_owner}/frida-core:{base_branch}."
        )

    start_branch_from_tag(repo_dir, target_branch, target_version)
    cherry_pick_range(repo_dir, commits, target_branch)
    core_head = git_output(repo_dir, "rev-parse", "HEAD")

    if dry_run:
        log(f"[dry-run] skipping push for frida-core:{target_branch}")
        return core_head, True

    if not token:
        raise RuntimeError("STRONGR_FORK_TOKEN is required to create frida-core branches.")

    git(repo_dir, "push", "origin", f"HEAD:refs/heads/{target_branch}")
    return core_head, True


def prepare_frida_repo(
    workspace: Path,
    *,
    fork_owner: str,
    token: str | None,
    base_version: str,
    target_version: str,
    target_branch: str,
    core_head: str,
    dry_run: bool,
) -> bool:
    repo_dir = workspace / "frida"
    clone_repo(
        build_repo_url(fork_owner, "frida", token),
        repo_dir,
        display_url=f"https://github.com/{fork_owner}/frida.git",
    )
    git(repo_dir, "remote", "add", "upstream", "https://github.com/frida/frida.git")
    configure_identity(repo_dir)

    if remote_branch_exists(repo_dir, "origin", target_branch):
        log(f"frida branch {target_branch} already exists")
        return False

    base_branch = f"strongr-{base_version}"
    if not remote_branch_exists(repo_dir, "origin", base_branch):
        raise RuntimeError(f"Missing baseline branch {base_branch} in {fork_owner}/frida.")

    fetch_branch(repo_dir, "origin", base_branch)
    fetch_tag(repo_dir, "upstream", base_version)
    fetch_tag(repo_dir, "upstream", target_version)

    commits = commit_range(repo_dir, base_version, base_branch)
    start_branch_from_tag(repo_dir, target_branch, target_version)
    if commits:
        cherry_pick_range(repo_dir, commits, target_branch)

    git(
        repo_dir,
        "config",
        "-f",
        ".gitmodules",
        "submodule.subprojects/frida-core.url",
        f"https://github.com/{fork_owner}/frida-core.git",
    )
    git(repo_dir, "submodule", "sync", "--", "subprojects/frida-core")
    git(repo_dir, "submodule", "update", "--init", "--depth", "1", "subprojects/frida-core")

    submodule_dir = repo_dir / "subprojects" / "frida-core"
    git(submodule_dir, "fetch", "origin", target_branch)
    git(submodule_dir, "checkout", core_head)

    git(repo_dir, "add", ".gitmodules", "subprojects/frida-core")
    if git_output(repo_dir, "status", "--porcelain"):
        git(repo_dir, "commit", "-m", f"Prepare strongR sources for {target_version}")

    if dry_run:
        log(f"[dry-run] skipping push for frida:{target_branch}")
        return True

    if not token:
        raise RuntimeError("STRONGR_FORK_TOKEN is required to create frida branches.")

    git(repo_dir, "push", "origin", f"HEAD:refs/heads/{target_branch}")
    return True


def verify_custom_source_ref(
    workspace: Path,
    *,
    fork_owner: str,
    token: str | None,
    source_ref: str,
) -> None:
    repo_dir = workspace / "frida-verify"
    clone_repo(
        build_repo_url(fork_owner, "frida", token),
        repo_dir,
        display_url=f"https://github.com/{fork_owner}/frida.git",
    )
    if not remote_branch_exists(repo_dir, "origin", source_ref):
        raise RuntimeError(
            f"Missing source branch {source_ref} in {fork_owner}/frida."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare strongR source branches in frida and frida-core forks.",
    )
    parser.add_argument("--fork-owner", required=True)
    parser.add_argument("--base-version", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--source-ref", default="")
    parser.add_argument("--github-output", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("STRONGR_FORK_TOKEN", "").strip() or None
    add_mask(token)

    default_source_ref = f"strongr-{args.target_version}"
    requested_source_ref = args.source_ref.strip()
    source_ref = requested_source_ref or default_source_ref
    custom_source_ref = bool(requested_source_ref) and requested_source_ref != default_source_ref

    try:
        with tempfile.TemporaryDirectory(prefix="strongr-prepare-") as raw_workspace:
            workspace = Path(raw_workspace)

            if custom_source_ref:
                verify_custom_source_ref(
                    workspace,
                    fork_owner=args.fork_owner,
                    token=token,
                    source_ref=source_ref,
                )
                write_output(args.github_output, "source_ref", source_ref)
                write_output(args.github_output, "core_ref", "")
                write_output(args.github_output, "prepared", "0")
                return 0

            core_head, core_created = prepare_frida_core(
                workspace,
                fork_owner=args.fork_owner,
                token=token,
                base_version=args.base_version,
                target_version=args.target_version,
                target_branch=default_source_ref,
                dry_run=args.dry_run,
            )
            source_created = prepare_frida_repo(
                workspace,
                fork_owner=args.fork_owner,
                token=token,
                base_version=args.base_version,
                target_version=args.target_version,
                target_branch=default_source_ref,
                core_head=core_head,
                dry_run=args.dry_run,
            )
    except (CommandError, RuntimeError) as exc:
        log(f"::error::{exc}")
        if isinstance(exc, CommandError):
            if exc.stdout.strip():
                log(exc.stdout.rstrip())
            if exc.stderr.strip():
                log(exc.stderr.rstrip())
        return 1

    write_output(args.github_output, "source_ref", source_ref)
    write_output(args.github_output, "core_ref", default_source_ref)
    write_output(
        args.github_output,
        "prepared",
        "1" if (core_created or source_created) else "0",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
