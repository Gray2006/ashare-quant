# -*- coding: utf-8 -*-
"""从 GitHub 仓库链接检查并安装更新。

用户把仓库地址(GitHub 网页链接、git 地址、`owner/repo` 都行)粘进设置页,
这里负责:

    parse_repo()     链接 → (owner, repo)
    check()          问 GitHub API 要最新 release(没有 release 就退回 tag)
    download()       下源码 zip 到临时目录
    stage()          解压并生成 apply 脚本,程序退出后由脚本覆盖文件

边界:
  * 只认 github.com 的公开仓库;私有库没有 token,拿到 404 就是拿不到
  * 打包版(frozen)不做自替换,只返回下载地址让用户手动装
  * 覆盖时排除 .venv / data / .git / config.json,不会把用户数据和配置冲掉
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests

API = "https://api.github.com"
_UA = {"User-Agent": "ashare-quant-updater", "Accept": "application/vnd.github+json"}
TIMEOUT = 15

# requests 在这台机器上会被本地代理/系统证书链挡掉(SSLError),而系统 curl
# 走的是 Windows 证书存储,能通。所以网络层一律先 requests、失败退 curl。
_CURL = shutil.which("curl") or ("curl.exe" if os.name == "nt" else "curl")

_RE_FULL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$")
_RE_SLUG = re.compile(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?$")


class UpdateError(RuntimeError):
    pass


def parse_repo(text: str) -> tuple[str, str]:
    """把各种写法的链接拆成 (owner, repo)。拆不出就抛 UpdateError。"""
    s = (text or "").strip().rstrip("/")
    if not s:
        raise UpdateError("先粘贴 GitHub 仓库链接")
    m = _RE_FULL.search(s) or _RE_SLUG.match(s)
    if not m:
        raise UpdateError("认不出这个链接,格式要像 https://github.com/用户名/仓库名")
    owner, repo = m.group(1), m.group(2)
    if not owner or not repo:
        raise UpdateError("认不出这个链接,格式要像 https://github.com/用户名/仓库名")
    return owner, repo


def _ver_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """最新版是否比当前版新。纯数字比较,v1.2.3 与 1.2.3 等价。"""
    return _ver_tuple(latest) > _ver_tuple(current)


def _curl(args: list[str], timeout: float) -> str:
    cmd = [_CURL, "-sS", "--max-time", str(int(timeout)), "-L",
           "-H", f"User-Agent: {_UA['User-Agent']}",
           "-H", f"Accept: {_UA['Accept']}"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"curl 调用失败:{str(e)[:80]}") from e
    if proc.returncode != 0:
        raise UpdateError(f"curl 失败({proc.returncode}):"
                          f"{proc.stderr.decode('utf-8', 'ignore')[:100]}")
    return proc.stdout.decode("utf-8", "replace")


def _api(path: str, allow_404: bool = False) -> tuple[int, object]:
    """GET 一个 API 路径,返回 (状态码, JSON)。先 requests,失败退 curl。"""
    url = f"{API}{path}"
    status, data = 0, None
    try:
        r = requests.get(url, headers=_UA, timeout=TIMEOUT)
        status = r.status_code
        try:
            data = r.json()
        except ValueError:
            data = None
    except requests.RequestException:
        body = _curl(["-w", "\n%{http_code}", url], TIMEOUT)
        text, _, code = body.rpartition("\n")
        status = int(code or 0)
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = None

    if status == 404:
        if allow_404:
            return status, data
        raise UpdateError("仓库不存在,或者是私有库(需要登录才能看)")
    if status == 403:
        raise UpdateError("GitHub 限流了,过几分钟再试")
    if status == 0:
        raise UpdateError("连不上 GitHub,检查网络或代理")
    if status >= 400:
        raise UpdateError(f"GitHub 返回 {status}")
    return status, data


def check(text: str) -> dict:
    """查最新版本。返回 dict,拿不到 release 时用 tag 兜底。"""
    owner, repo = parse_repo(text)
    info: dict = {"owner": owner, "repo": repo, "source": "release"}

    status, d = _api(f"/repos/{owner}/{repo}/releases/latest", allow_404=True)
    if status == 200 and isinstance(d, dict) and d.get("tag_name"):
        info.update({
            "tag": str(d.get("tag_name") or ""),
            "version": str(d.get("tag_name") or "").lstrip("vV"),
            "name": str(d.get("name") or d.get("tag_name") or ""),
            "notes": str(d.get("body") or "")[:4000],
            "html_url": str(d.get("html_url") or ""),
            "published": str(d.get("published_at") or ""),
        })
        for a in d.get("assets") or []:
            name = str(a.get("name") or "").lower()
            if name.endswith(".zip"):
                info["asset_url"] = str(a.get("browser_download_url") or "")
                info["asset_name"] = str(a.get("name") or "")
                break
        return info

    # 没有 release(404 已在 _api 里抛掉,这里只处理 200 但内容不对的情况)
    _, tags = _api(f"/repos/{owner}/{repo}/tags?per_page=1")
    if not isinstance(tags, list) or not tags:
        raise UpdateError("这个仓库没有 release,也没有 tag,没法判断版本")
    tag = str(tags[0].get("name") or "")
    info.update({
        "source": "tag",
        "tag": tag,
        "version": tag.lstrip("vV"),
        "name": tag,
        "notes": "",
        "html_url": f"https://github.com/{owner}/{repo}/releases",
        "published": "",
    })
    return info


def download(info: dict, dest_dir: str | None = None) -> Path:
    """把源码 zip 下到本地,返回 zip 路径。优先用 release 里的 zip 资产。"""
    owner, repo = info["owner"], info["repo"]
    tag = info.get("tag") or ""
    if info.get("asset_url"):
        url = info["asset_url"]
        name = info.get("asset_name") or "update.zip"
    else:
        if not tag:
            raise UpdateError("没有可下载的 tag")
        url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip"
        name = f"{repo}-{tag}.zip"

    out_dir = Path(dest_dir or tempfile.mkdtemp(prefix="ashare-quant-upd-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / re.sub(r"[^\w.-]+", "_", name)

    try:
        with requests.get(url, headers=_UA, timeout=60, stream=True,
                          allow_redirects=True) as r:
            if r.status_code >= 400:
                raise UpdateError(f"下载失败:{r.status_code}")
            with open(out, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException:
        # 和 _api 同样的理由:requests 的证书链可能被本地代理挡掉
        cmd = [_CURL, "-sS", "-L", "--max-time", "300",
               "-H", f"User-Agent: {_UA['User-Agent']}", "-o", str(out), url]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=320,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            raise UpdateError(f"下载中断:{str(e)[:100]}") from e
        if proc.returncode != 0:
            raise UpdateError(f"下载失败(curl {proc.returncode}):"
                              f"{proc.stderr.decode('utf-8', 'ignore')[:100]}")
    if out.stat().st_size < 1024:
        raise UpdateError("下载到的文件太小,可能不是有效包")
    return out


def extract(zip_path: Path, dest_dir: Path | None = None) -> Path:
    """解压,返回真正的源码根目录(GitHub 的 zip 会套一层 repo-tag/)。"""
    dest = Path(dest_dir or (Path(zip_path).parent / "src"))
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(dest)
    except zipfile.BadZipFile as e:
        raise UpdateError("下载到的不是有效 zip") from e

    items = [p for p in dest.iterdir() if p.is_dir()]
    if len(items) == 1 and (items[0] / "run.py").exists():
        return items[0]
    if (dest / "run.py").exists():
        return dest
    # 套得比一层深:找路径最短的 run.py
    hits = sorted(dest.rglob("run.py"), key=lambda p: len(p.parts))
    if hits:
        return hits[0].parent
    raise UpdateError("压缩包里没有 run.py,这个 tag 不是桌面版源码,换一个 release 再试")


def _bat_quote(p: str) -> str:
    return f'"{p}"'


def stage(src: Path, target: Path, launcher: str = "start.bat") -> Path:
    """生成「程序退出后替换文件再启动」的批处理,返回脚本路径。

    覆盖用 robocopy /E:保留用户改过的 config.json / data / .venv。
    脚本先等 3 秒给当前进程退出,再覆盖、再启动、最后删自己。
    """
    src = Path(src).resolve()
    target = Path(target).resolve()
    bat = Path(tempfile.gettempdir()) / "ashare-quant-apply-update.bat"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title 量化工作台 - 正在更新",
        "timeout /t 3 /nobreak >nul",
        f"robocopy {_bat_quote(str(src))} {_bat_quote(str(target))} /E /NFL /NDL /NJH /NJS "
        "/XD .venv .git data memory __pycache__ /XF config.json *.db *.pyc",
        "if errorlevel 8 (",
        "  echo [X] 更新失败,原程序没动。按任意键关闭。",
        "  pause >nul",
        "  del \"%~f0\"",
        "  exit /b 1",
        ")",
        f"cd /d {_bat_quote(str(target))}",
        f"start \"\" {_bat_quote(launcher)}",
        "del \"%~f0\"",
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return bat


def apply_now(bat: Path) -> None:
    """把更新脚本拉起来(独立窗口),调用方随后应尽快退出程序。"""
    subprocess.Popen(["cmd.exe", "/c", str(bat)],
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                     cwd=str(bat.parent))


def is_frozen() -> bool:
    import sys
    return bool(getattr(sys, "frozen", False))
