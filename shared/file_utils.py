"""File-system helpers: path validation, staging, copy/move primitives."""
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from shared.config import (
    EDITS, FILE_ROOTS, MAX_EDIT_BYTES, MAX_UPLOAD_BYTES,
    SENSITIVE_PATHS, UPLOADS,
)


def sensitive_path(candidate):
    return any(
        candidate == root or root in candidate.parents
        for root in SENSITIVE_PATHS
    )


def allowed_path(path, require_directory=False):
    candidate = Path(path).expanduser().resolve()
    if not any(candidate == root or root in candidate.parents for root in FILE_ROOTS):
        raise ValueError("路径不在允许的文件范围内。")
    if sensitive_path(candidate):
        raise ValueError("该路径包含工作台密钥、会话或内部配置，网页访问已禁止。")
    if not candidate.exists():
        raise ValueError("路径不存在。")
    if require_directory and not candidate.is_dir():
        raise ValueError("该路径不是目录。")
    return candidate


def staged_upload(token):
    path = UPLOADS / token
    if not path.is_file():
        raise ValueError("上传暂存文件不存在或已过期。")
    return path


def install_upload(staged, destination):
    if destination.exists() and not destination.is_file():
        raise ValueError("不能用上传文件覆盖目录。")
    temporary = destination.with_name("." + destination.name + ".codex-web-upload")
    try:
        shutil.copyfile(staged, temporary)
        if not any(
            root == destination or root in destination.parents
            for root in (Path("/mnt/c"), Path("/mnt/d"))
        ):
            temporary.chmod(0o600)
        temporary.replace(destination)
        staged.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def copy_file_compatible(source, destination):
    shutil.copyfile(source, destination)
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass


def copy_directory_compatible(source, destination):
    destination.mkdir()
    for child in source.iterdir():
        if child.is_symlink():
            raise ValueError("目录包含符号链接，暂不支持网页复制。")
        target = destination / child.name
        if child.is_dir():
            copy_directory_compatible(child, target)
        elif child.is_file():
            copy_file_compatible(child, target)
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass


def staged_edit(token):
    path = EDITS / token
    if not path.is_file():
        raise ValueError("编辑暂存内容不存在或已过期。")
    return path


def clean_staging():
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    for directory in (UPLOADS, EDITS):
        if not directory.exists():
            continue
        for child in directory.iterdir():
            if not child.is_file():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    child.unlink(missing_ok=True)
            except OSError:
                pass


def editable_text(path):
    if not path.is_file():
        raise ValueError("只能编辑普通文件。")
    if path.stat().st_size > MAX_EDIT_BYTES:
        raise ValueError("文件超过 1 MB 上限。")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError:
        raise ValueError("该文件不是文本文件。")


def workspace(path, target):
    path = str(path).strip()
    home_prefix = str(Path.home()) + "/"
    wsl_roots = (home_prefix, "/mnt/")
    win_roots = ("C:\\", "D:\\", "c:\\", "d:\\")
    if target == "wsl":
        if not any(path.startswith(prefix) for prefix in wsl_roots):
            raise ValueError("WSL 路径必须以 " + home_prefix + " 或 /mnt/ 开头。")
    elif target == "windows":
        if not any(path.startswith(prefix) for prefix in win_roots):
            raise ValueError("Windows 路径必须以 C:\\ 或 D:\\ 开头。")
    directory = Path(path).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(directory)
