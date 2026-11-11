"""網頁靜態檔。開發時沿目錄往上找 web/，安裝後用套件內的 ymzz/web。"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_web_dir(search: list[Path], packaged: Path | None = None) -> Path:
    for candidate in search:
        if (candidate / "index.html").is_file() or (candidate / "dashboard.html").is_file():
            return candidate
    if packaged is not None and ((packaged / "index.html").is_file() or (packaged / "dashboard.html").is_file()):
        return packaged
    raise FileNotFoundError("找不到網頁靜態檔。安裝後的 ymzz-web 需要套件裡的 web/index.html。")


def web_dir() -> Path:
    search: list[Path] = []
    env = os.environ.get("YMZZ_ROOT")
    if env:
        search.append(Path(env) / "web")
    here = Path(__file__).resolve()
    for parent in here.parents:
        search.append(parent / "web")
    packaged = here.parent / "web"
    try:
        from importlib.resources import files

        resource = files("ymzz").joinpath("web")
        candidate = Path(str(resource))
        if (candidate / "index.html").is_file() or (candidate / "dashboard.html").is_file():
            packaged = candidate
    except (ModuleNotFoundError, TypeError, FileNotFoundError):
        pass
    return resolve_web_dir(search, packaged)
