# -*- coding: utf-8 -*-
"""
Cloudflare Tunnel (cloudflared) 管理ヘルパー。
- cloudflaredバイナリの自動ダウンロード(Windows/Linux/Mac対応)
- Quick Tunnel(即席URL)の起動・URL取得・停止
- Named Tunnel(トークン指定)の起動・停止
"""

import os
import re
import sys
import stat
import platform
import subprocess
import urllib.request

BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")

CLOUDFLARED_URLS = {
    ("Windows", "AMD64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    ("Windows", "x86"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-386.exe",
    ("Linux", "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    ("Linux", "aarch64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64",
    ("Darwin", "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
    ("Darwin", "arm64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz",
}


def _binary_path():
    name = "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
    return os.path.join(BIN_DIR, name)


def is_installed():
    return os.path.exists(_binary_path())


def get_binary_path():
    return _binary_path()


def download_cloudflared(progress_callback=None):
    """cloudflaredバイナリを ./bin にダウンロードする。失敗時は例外を投げる。"""
    system = platform.system()
    machine = platform.machine()

    key = (system, machine)
    if key not in CLOUDFLARED_URLS:
        # よくある別名に正規化
        if machine in ("AMD64", "x86_64", "amd64"):
            key = (system, "AMD64") if system == "Windows" else (system, "x86_64")
        elif machine in ("ARM64", "arm64", "aarch64"):
            key = (system, "aarch64") if system == "Linux" else (system, "arm64")

    if key not in CLOUDFLARED_URLS:
        raise RuntimeError(f"このOS/アーキテクチャには対応していません: {system}/{machine}")

    url = CLOUDFLARED_URLS[key]
    os.makedirs(BIN_DIR, exist_ok=True)
    dest = _binary_path()
    tmp = dest + ".download"

    def _report(blocknum, blocksize, totalsize):
        if progress_callback and totalsize > 0:
            downloaded = blocknum * blocksize
            percent = min(100, int(downloaded * 100 / totalsize))
            progress_callback(percent)

    if url.endswith(".tgz"):
        import tarfile
        tgz_path = dest + ".tgz"
        urllib.request.urlretrieve(url, tgz_path, _report)
        with tarfile.open(tgz_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("cloudflared"):
                    member.name = os.path.basename(dest)
                    tar.extract(member, BIN_DIR)
        os.remove(tgz_path)
    else:
        urllib.request.urlretrieve(url, tmp, _report)
        os.replace(tmp, dest)

    if platform.system() != "Windows":
        st = os.stat(dest)
        os.chmod(dest, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return dest


def start_quick_tunnel(local_port, on_url_found=None, on_exit=None, origin_scheme="http"):
    """
    Quick Tunnel(トライアル用, https://xxxx.trycloudflare.com)を起動する。
    別プロセスとして起動し、Popenオブジェクトを返す。
    on_url_found(url): URLが判明したら呼ばれるコールバック
    on_exit(): プロセス終了時に呼ばれるコールバック
    origin_scheme: ローカルサーバーが"http"か"https"か。
                   自己署名証明書を使う場合は --no-tls-verify が自動付与される。
    """
    binary = _binary_path()
    if not os.path.exists(binary):
        raise RuntimeError("cloudflaredがインストールされていません")

    cmd = [binary, "tunnel", "--url", f"{origin_scheme}://127.0.0.1:{local_port}", "--no-autoupdate"]
    if origin_scheme == "https":
        # 自己署名証明書を許可(オレオレ証明書のローカルサーバー向け)
        cmd.append("--no-tls-verify")

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )

    import threading

    def _reader():
        url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
        found = False
        try:
            for line in proc.stdout:
                if not found:
                    m = url_pattern.search(line)
                    if m and on_url_found:
                        found = True
                        on_url_found(m.group(0))
        except Exception:
            pass
        proc.wait()
        if on_exit:
            on_exit()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    return proc


def start_named_tunnel(token, on_exit=None):
    """
    Named Tunnel(トークン指定, 事前にダッシュボードで作成したトンネル)を起動する。
    """
    binary = _binary_path()
    if not os.path.exists(binary):
        raise RuntimeError("cloudflaredがインストールされていません")

    cmd = [binary, "tunnel", "run", "--token", token, "--no-autoupdate"]

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    if on_exit:
        import threading

        def _waiter():
            proc.wait()
            on_exit()

        threading.Thread(target=_waiter, daemon=True).start()

    return proc


def stop_tunnel(proc):
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
