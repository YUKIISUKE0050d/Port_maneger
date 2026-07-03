# -*- coding: utf-8 -*-
"""
playit.gg エージェント管理ヘルパー。
- playitバイナリの自動ダウンロード(Windows/Linux/Mac対応)
- 初回クレームURL検出・ブラウザ自動起動
- SECRET_KEY / toml設定ファイルによる起動
- TCP/UDP両対応(Minecraftなどゲームサーバーに最適)

【使い方の流れ】
1. download_playit() でバイナリ取得
2. start_agent() で起動 → 初回はクレームURLが出るのでブラウザ承認
3. ダッシュボードでトンネル作成(TCP/UDP・ポート設定)
4. 以降は start_agent() だけで自動接続
"""

import os
import re
import sys
import stat
import platform
import subprocess
import threading
import webbrowser
import urllib.request

BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playit_data")

PLAYIT_DOWNLOAD_URLS = {
    ("Windows", "AMD64"): "https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-windows_64.exe",
    ("Windows", "x86"):   "https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-windows_32.exe",
    ("Linux",   "x86_64"):"https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-linux_64",
    ("Linux",   "aarch64"):"https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-linux_arm64",
    ("Darwin",  "x86_64"):"https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-darwin_64",
    ("Darwin",  "arm64"): "https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-darwin_arm64",
}


def _binary_path():
    name = "playit.exe" if platform.system() == "Windows" else "playit"
    return os.path.join(BIN_DIR, name)


def _toml_path():
    return os.path.join(DATA_DIR, "playit.toml")


def is_installed():
    return os.path.exists(_binary_path())


def is_claimed():
    """tomlが存在 = 既にクレーム済み・シークレットキー保存済み"""
    return os.path.exists(_toml_path())


def download_playit(progress_callback=None):
    """playitバイナリを ./bin にダウンロードする。失敗時は例外を投げる。"""
    system = platform.system()
    machine = platform.machine()

    # アーキテクチャ名の正規化
    if machine in ("AMD64", "x86_64", "amd64"):
        machine_key = "AMD64" if system == "Windows" else "x86_64"
    elif machine in ("ARM64", "arm64", "aarch64"):
        machine_key = "arm64" if system == "Darwin" else "aarch64"
    else:
        machine_key = machine

    key = (system, machine_key)
    if key not in PLAYIT_DOWNLOAD_URLS:
        raise RuntimeError(f"このOS/アーキテクチャには対応していません: {system}/{machine}")

    url = PLAYIT_DOWNLOAD_URLS[key]
    os.makedirs(BIN_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    dest = _binary_path()
    tmp = dest + ".download"

    def _report(blocknum, blocksize, totalsize):
        if progress_callback and totalsize > 0:
            pct = min(100, int(blocknum * blocksize * 100 / totalsize))
            progress_callback(pct)

    urllib.request.urlretrieve(url, tmp, _report)
    os.replace(tmp, dest)

    if platform.system() != "Windows":
        st = os.stat(dest)
        os.chmod(dest, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return dest


def start_agent(
    on_claim_url=None,    # コールバック(url: str) - 初回クレームURL検出時
    on_connected=None,    # コールバック() - エージェント接続完了時
    on_tunnel_addr=None,  # コールバック(addr: str) - トンネルアドレス判明時
    on_log=None,          # コールバック(line: str) - ログ行ごと
    on_exit=None,         # コールバック() - プロセス終了時
):
    """
    playitエージェントを起動する。Popenオブジェクトを返す。
    出力解析は別スレッドで行い、各コールバックで通知する。
    """
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

    # クレームURL・接続状態・トンネルアドレスを出力から解析するパターン
    CLAIM_PATTERN    = re.compile(r"https://playit\.gg/claim/[a-zA-Z0-9]+")
    CONNECTED_PATTERN= re.compile(r"(connected|agent running)", re.IGNORECASE)
    TUNNEL_PATTERN   = re.compile(r"([a-zA-Z0-9\-]+\.(gl\.)?(?:joinmc\.link|ply\.gg|playit\.gg)[:\d]*)", re.IGNORECASE)

    def _reader():
        claim_opened = False
        connected_notified = False
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if on_log:
                    on_log(line)

                # クレームURL
                if not claim_opened:
                    m = CLAIM_PATTERN.search(line)
                    if m:
                        claim_opened = True
                        if on_claim_url:
                            on_claim_url(m.group(0))

                # 接続確立
                if not connected_notified and CONNECTED_PATTERN.search(line):
                    connected_notified = True
                    if on_connected:
                        on_connected()

                # トンネルアドレス
                if on_tunnel_addr:
                    m = TUNNEL_PATTERN.search(line)
                    if m:
                        on_tunnel_addr(m.group(0))

        except Exception:
            pass
        proc.wait()
        if on_exit:
            on_exit()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

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
