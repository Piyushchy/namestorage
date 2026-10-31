"""Path handling: everything the agent touches must stay inside one root folder."""
import os


class PathError(Exception):
    pass


def resolve(root_real: str, rel: str) -> str:
    """Map a client-supplied relative path to a real path inside root_real."""
    if not isinstance(rel, str) or "\x00" in rel or "\\" in rel or len(rel) > 4096:
        raise PathError("invalid path")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("invalid path")
    target = os.path.realpath(os.path.join(root_real, *parts))
    if os.path.commonpath([root_real, target]) != root_real:
        raise PathError("invalid path")
    return target


def valid_name(name) -> bool:
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if len(name.encode("utf-8", "replace")) > 200 or name.endswith(".nspart"):
        return False
    return True
