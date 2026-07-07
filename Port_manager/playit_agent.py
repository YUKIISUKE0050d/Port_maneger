# -*- coding: utf-8 -*-
"""
playit.gg エージェント管理ヘルパー。
- GitHub APIで最新リリースを動的取得してダウンロード
- 初回クレームURL検出・ブラウザ自動起動
- SECRET_KEY / toml設定ファイルによる起動
- TCP/UDP両対応(Minecraftなどゲームサーバーに最適)
"""

import os
import re
import sys
import stat
import json
import platform
import subprocess
import threading
import webbrowser
import urllib.request
import urllib.error

BIN_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playit_data")

GITHUB_API_URL = "https://api.github.com/repos/playit-cloud/playit-agent/releases/latest"

# アセット名のパターン: OS + アーキ → 正規表現で一致させる
ASSET_PATTERNS = {
    ("Windows", "x86_64"): re.compile(r"playit-windows-x86_64.*\.exe$", re.IGNORECASE),
    ("Windows", "x86"):    re.compile(r"playit-windows-x86[^_].*\.exe$", re.IGNORECASE),
    ("Linux",   "x86_64"): re.compile(r"playit-linux-amd64$", re.IGNORECASE),
    ("Linux",   "aarch64"):re.compile(r"playit-linux-arm64$",  re.IGNORECASE),
    ("Darwin",  "x86_64"): re.compile(r"playit-darwin-amd64$", re.IGNORECASE),
    ("Darwin",  "arm64"):  re.compile(r"playit-darwin-arm64$",  re.IGNORECASE),
}


def _binary_path():
    name = "playit.exe" if platform.system() == "Windows" else "playit"
    return os.path.join(BIN_DIR, name)


def _toml_path():
    return os.path.join(DATA_DIR, "playit.toml")


def is_installed():
    return os.path.exists(_binary_path())


def is_claimed():
    return os.path.exists(_toml_path())


def _normalize_arch():
    machine = platform.machine()
    if machine in ("AMD64", "x86_64", "amd64"):
        return "x86_64"
    if machine in ("ARM64", "arm64", "aarch64"):
        return "aarch64" if platform.system() == "Linux" else "arm64"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return machine


def _fetch_latest_asset_url():
    """GitHub APIで最新リリースのアセット一覧を取得し、対応するダウンロードURLを返す"""
    system = platform.system()
    arch   = _normalize_arch()
    key    = (system, arch)

    pattern = ASSET_PATTERNS.get(key)
    if pattern is None:
        raise RuntimeError(f"このOS/アーキテクチャには対応していません: {system}/{arch}")

    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "PortManagerTool/1.0"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    assets = data.get("assets", [])
    if not assets:
        raise RuntimeError("GitHubリリースにアセットが見つかりませんでした")

    # signed版を優先、なければそれ以外
    signed   = [a for a in assets if pattern.match(a["name"]) and "signed" in a["name"]]
    unsigned = [a for a in assets if pattern.match(a["name"]) and "signed" not in a["name"]]
    matched  = signed or unsigned

    if not matched:
        names = [a["name"] for a in assets]
        raise RuntimeError(
            f"{system}/{arch} 向けのバイナリが見つかりませんでした。\n"
            f"利用可能なアセット: {names}"
        )

    return matched[0]["browser_download_url"], data.get("tag_name", "")


def download_playit(progress_callback=None):
    """playitバイナリを ./bin にダウンロードする。失敗時は例外を投げる。"""
    os.makedirs(BIN_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    url, tag = _fetch_latest_asset_url()
    dest = _binary_path()
    tmp  = dest + ".download"

    def _report(blocknum, blocksize, totalsize):
        if progress_callback and totalsize > 0:
            pct = min(100, int(blocknum * blocksize * 100 / totalsize))
            progress_callback(pct)

    urllib.request.urlretrieve(url, tmp, _report)
    os.replace(tmp, dest)

    if platform.system() != "Windows":
        st = os.stat(dest)
        os.chmod(dest, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return dest, tag


def start_agent(
    on_claim_url=None,
    on_connected=None,
    on_tunnel_addr=None,
    on_log=None,
    on_exit=None,
):
    binary = _binary_path()
    if not os.path.exists(binary):
        raise RuntimeError("playitがインストールされていません")

    os.makedirs(DATA_DIR, exist_ok=True)

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NO_WINDOW

    cmd = [binary, "--secret_path", _toml_path()]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        cwd=DATA_DIR,
    )

    CLAIM_PATTERN    = re.compile(r"https://playit\.gg/claim/[a-zA-Z0-9\-]+")
    CONNECTED_PATTERN = re.compile(r"(connected|agent running|tunnel ready)", re.IGNORECASE)
    TUNNEL_PATTERN   = re.compile(
        r"([a-zA-Z0-9][\w\-]*\.(?:joinmc\.link|ply\.gg|playit\.gg)(?::\d+)?)",
        re.IGNORECASE
    )

    def _reader():
        claim_opened       = False
        connected_notified = False
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if on_log:
                    on_log(line)

                if not claim_opened:
                    m = CLAIM_PATTERN.search(line)
                    if m:
                        claim_opened = True
                        if on_claim_url:
                            on_claim_url(m.group(0))

                if not connected_notified and CONNECTED_PATTERN.search(line):
                    connected_notified = True
                    if on_connected:
                        on_connected()

                if on_tunnel_addr:
                    m = TUNNEL_PATTERN.search(line)
                    if m:
                        on_tunnel_addr(m.group(0))
        except Exception:
            pass
        proc.wait()
        if on_exit:
            on_exit()

    threading.Thread(target=_reader, daemon=True).start()
    return proc


def stop_agent(proc):
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:
        pass


def open_dashboard():
    webbrowser.open("https://playit.gg/account/tunnels")


def open_claim_url(url):
    webbrowser.open(url)
