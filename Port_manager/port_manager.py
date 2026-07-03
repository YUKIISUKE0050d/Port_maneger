#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポート開放 & サーバー起動管理ツール
- ポート番号と起動コマンド(プログラム)を登録
- 「開放/起動」を押すとプログラムを起動し、Windowsファイアウォールにポート開放ルールを追加
- プログラムが終了したら自動でファイアウォールルールを閉鎖(削除)
"""

import os
import sys
import json
import threading
import subprocess
import platform
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import upnp_client
import cloudflare_tunnel
import playit_agent

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers.json")
FW_RULE_PREFIX = "PortMgr_"


def load_servers():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_servers(servers):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(servers, f, ensure_ascii=False, indent=2)


def is_windows():
    return platform.system() == "Windows"


def is_admin():
    if not is_windows():
        return os.geteuid() == 0  # type: ignore
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    """Windowsで管理者権限が無い場合、UACダイアログを表示して自身を再起動する"""
    import ctypes
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )


def open_port_windows(port, protocol="TCP", rule_name=None):
    """Windowsファイアウォールにインバウンド許可ルールを追加"""
    if rule_name is None:
        rule_name = f"{FW_RULE_PREFIX}{port}_{protocol}"
    cmd = [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={rule_name}",
        "dir=in",
        "action=allow",
        f"protocol={protocol}",
        f"localport={port}",
    ]
    subprocess.run(cmd, capture_output=True, text=True, shell=False)
    return rule_name


def close_port_windows(rule_name):
    cmd = [
        "netsh", "advfirewall", "firewall", "delete", "rule",
        f"name={rule_name}",
    ]
    subprocess.run(cmd, capture_output=True, text=True, shell=False)


def open_port_linux(port, protocol="tcp"):
    """ufwが使える場合のみ対応(なければスキップ)"""
    try:
        subprocess.run(["sudo", "ufw", "allow", f"{port}/{protocol}"],
                        capture_output=True, text=True)
    except Exception:
        pass


def close_port_linux(port, protocol="tcp"):
    try:
        subprocess.run(["sudo", "ufw", "delete", "allow", f"{port}/{protocol}"],
                        capture_output=True, text=True)
    except Exception:
        pass


class ServerEntry:
    """登録されたサーバー1件を表すクラス"""

    def __init__(self, name, command, port, protocol="TCP", workdir="", upnp=True,
                 tunnel_mode="none", tunnel_token="", local_https=False):
        self.name = name
        self.command = command  # 起動コマンド(文字列)
        self.port = int(port)
        self.protocol = protocol
        self.workdir = workdir
        self.upnp = upnp  # UPnPでルーターのポートも自動開放するか
        self.tunnel_mode = tunnel_mode  # "none" / "quick" / "named" / "playit"
        self.tunnel_token = tunnel_token  # named tunnel用トークン
        self.local_https = local_https  # ローカルサーバーがHTTPS(自己署名証明書)か

        self.process = None      # subprocess.Popen
        self.fw_rule_name = None
        self.upnp_mapped = False
        self.running = False
        self.monitor_thread = None

        self.tunnel_proc = None
        self.tunnel_url = None

        # playit.gg専用
        self.playit_proc = None
        self.playit_addr = None

    def to_dict(self):
        return {
            "name": self.name,
            "command": self.command,
            "port": self.port,
            "protocol": self.protocol,
            "workdir": self.workdir,
            "upnp": self.upnp,
            "tunnel_mode": self.tunnel_mode,
            "tunnel_token": self.tunnel_token,
            "local_https": self.local_https,
        }

    @staticmethod
    def from_dict(d):
        return ServerEntry(d["name"], d["command"], d["port"],
                            d.get("protocol", "TCP"), d.get("workdir", ""),
                            d.get("upnp", True),
                            d.get("tunnel_mode", "none"),
                            d.get("tunnel_token", ""),
                            d.get("local_https", False))


class PortManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ポート開放・サーバー管理ツール")
        self.geometry("900x600")
        self.minsize(820, 520)

        self.servers = [ServerEntry.from_dict(d) for d in load_servers()]

        self._build_ui()
        self._refresh_list()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------- UI構築
    def _build_ui(self):
        # 上部: 登録フォーム
        frm = ttk.LabelFrame(self, text="サーバー登録")
        frm.pack(fill="x", padx=10, pady=8)

        ttk.Label(frm, text="名前:").grid(row=0, column=0, padx=4, pady=4, sticky="e")
        self.entry_name = ttk.Entry(frm, width=18)
        self.entry_name.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        ttk.Label(frm, text="ポート:").grid(row=0, column=2, padx=4, pady=4, sticky="e")
        self.entry_port = ttk.Entry(frm, width=8)
        self.entry_port.grid(row=0, column=3, padx=4, pady=4, sticky="w")

        ttk.Label(frm, text="プロトコル:").grid(row=0, column=4, padx=4, pady=4, sticky="e")
        self.combo_proto = ttk.Combobox(frm, values=["TCP", "UDP"], width=6, state="readonly")
        self.combo_proto.set("TCP")
        self.combo_proto.grid(row=0, column=5, padx=4, pady=4, sticky="w")

        ttk.Label(frm, text="起動コマンド:").grid(row=1, column=0, padx=4, pady=4, sticky="e")
        self.entry_cmd = ttk.Entry(frm, width=50)
        self.entry_cmd.grid(row=1, column=1, columnspan=4, padx=4, pady=4, sticky="we")

        ttk.Button(frm, text="参照", command=self.browse_command).grid(row=1, column=5, padx=4, pady=4)

        ttk.Label(frm, text="作業フォルダ(任意):").grid(row=2, column=0, padx=4, pady=4, sticky="e")
        self.entry_workdir = ttk.Entry(frm, width=50)
        self.entry_workdir.grid(row=2, column=1, columnspan=4, padx=4, pady=4, sticky="we")
        ttk.Button(frm, text="参照", command=self.browse_workdir).grid(row=2, column=5, padx=4, pady=4)

        self.var_upnp = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, text="UPnPでルーターのポートも自動開放する",
            variable=self.var_upnp
        ).grid(row=3, column=0, columnspan=2, padx=4, pady=4, sticky="w")

        ttk.Label(frm, text="外部公開:").grid(row=3, column=2, padx=4, pady=4, sticky="e")
        self.combo_tunnel = ttk.Combobox(
            frm, values=["なし", "Cloudflare Quick Tunnel", "Cloudflare Named Tunnel", "Playit.gg (TCP/UDP対応)"],
            width=28, state="readonly"
        )
        self.combo_tunnel.set("なし")
        self.combo_tunnel.grid(row=3, column=3, padx=4, pady=4, sticky="w")
        self.combo_tunnel.bind("<<ComboboxSelected>>", self._on_tunnel_mode_change)

        ttk.Label(frm, text="Tunnelトークン:").grid(row=4, column=0, padx=4, pady=4, sticky="e")
        self.entry_tunnel_token = ttk.Entry(frm, width=50, state="disabled")
        self.entry_tunnel_token.grid(row=4, column=1, columnspan=4, padx=4, pady=4, sticky="we")

        self.var_local_https = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm, text="ローカルサーバーはHTTPS(自己署名証明書)で動作している",
            variable=self.var_local_https
        ).grid(row=5, column=0, columnspan=4, padx=4, pady=4, sticky="w")

        ttk.Button(frm, text="登録する", command=self.add_server).grid(row=3, column=5, padx=4, pady=6, sticky="e")
        ttk.Button(frm, text="外部IPを確認", command=self.check_external_ip).grid(row=3, column=4, padx=4, pady=6, sticky="e")
        ttk.Button(frm, text="cloudflaredを準備", command=self.prepare_cloudflared).grid(row=4, column=5, padx=4, pady=6, sticky="e")

        btn_playit_frame = ttk.Frame(frm)
        btn_playit_frame.grid(row=5, column=5, padx=4, pady=2, sticky="e")
        ttk.Button(btn_playit_frame, text="playitを準備", command=self.prepare_playit).pack(side="top", fill="x", pady=1)
        ttk.Button(btn_playit_frame, text="ダッシュボード", command=playit_agent.open_dashboard).pack(side="top", fill="x", pady=1)

        for c in range(6):
            frm.grid_columnconfigure(c, weight=1 if c in (1, 3) else 0)

        # 下部: 操作ボタン(先にbottom配置して隠れないようにする)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=8, side="bottom")

        ttk.Button(btn_frame, text="開放 & 起動", command=self.start_selected).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="閉鎖 & 停止", command=self.stop_selected).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="削除", command=self.delete_selected).pack(side="left", padx=4)

        self.status_label = ttk.Label(btn_frame, text="管理者権限で実行中(自動取得済み)", foreground="green")
        self.status_label.pack(side="right", padx=8)

        # 中央: 登録済みサーバー一覧
        list_frame = ttk.LabelFrame(self, text="登録済みサーバー一覧")
        list_frame.pack(fill="both", expand=True, padx=10, pady=8)

        columns = ("name", "port", "proto", "command", "upnp", "url", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("name", text="名前")
        self.tree.heading("port", text="ポート")
        self.tree.heading("proto", text="プロトコル")
        self.tree.heading("command", text="起動コマンド")
        self.tree.heading("upnp", text="UPnP")
        self.tree.heading("url", text="公開URL")
        self.tree.heading("status", text="状態")

        self.tree.column("name", width=110)
        self.tree.column("port", width=60, anchor="center")
        self.tree.column("proto", width=60, anchor="center")
        self.tree.column("command", width=220)
        self.tree.column("upnp", width=60, anchor="center")
        self.tree.column("url", width=220)
        self.tree.column("status", width=90, anchor="center")

        self.tree.pack(fill="both", expand=True, side="left", padx=(6, 0), pady=6)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        # ダブルクリックでURLをコピー
        self.tree.bind("<Double-1>", self._on_tree_double_click)

    # ---------------------------------------------------------- 補助
    def _on_tunnel_mode_change(self, event=None):
        mode = self.combo_tunnel.get()
        if mode == "Cloudflare Named Tunnel":
            self.entry_tunnel_token.config(state="normal")
        else:
            self.entry_tunnel_token.delete(0, tk.END)
            self.entry_tunnel_token.config(state="disabled")

    def prepare_playit(self):
        if playit_agent.is_installed():
            messagebox.showinfo("確認", "playitは既にインストールされています。")
            return
        if not messagebox.askyesno("確認",
            "Minecraftなど生TCP/UDPの外部公開に使う playit.gg をダウンロードします。\nよろしいですか?"):
            return
        self.status_label.config(text="playitをダウンロード中...", foreground="blue")
        threading.Thread(target=self._download_playit_async, daemon=True).start()

    def _download_playit_async(self):
        try:
            playit_agent.download_playit()
            self.after(0, lambda: self._on_playit_download_done(True, None))
        except Exception as e:
            self.after(0, lambda: self._on_playit_download_done(False, str(e)))

    def _on_playit_download_done(self, ok, err):
        self.status_label.config(text="管理者権限で実行中(自動取得済み)", foreground="green")
        if ok:
            messagebox.showinfo("完了", "playitのダウンロードが完了しました。\n\n"
                "初回起動時にブラウザでクレーム承認が必要です。\n"
                "承認後、playit.ggダッシュボードでトンネルを作成してください。")
        else:
            messagebox.showerror("エラー", f"ダウンロードに失敗しました:\n{err}")

    def prepare_cloudflared(self):
        if cloudflare_tunnel.is_installed():
            messagebox.showinfo("確認", "cloudflaredは既にインストールされています。")
            return

        if not messagebox.askyesno(
            "確認",
            "外部公開機能(Cloudflare Tunnel)を使うために cloudflared をダウンロードします。\n"
            "よろしいですか?"
        ):
            return

        self.status_label.config(text="cloudflaredをダウンロード中...", foreground="blue")
        threading.Thread(target=self._download_cloudflared_async, daemon=True).start()

    def _download_cloudflared_async(self):
        try:
            cloudflare_tunnel.download_cloudflared()
            self.after(0, self._on_download_done, True, None)
        except Exception as e:
            self.after(0, self._on_download_done, False, str(e))

    def _on_download_done(self, ok, err):
        self.status_label.config(text="管理者権限で実行中(自動取得済み)", foreground="green")
        if ok:
            messagebox.showinfo("完了", "cloudflaredのダウンロードが完了しました。")
        else:
            messagebox.showerror("エラー", f"ダウンロードに失敗しました:\n{err}")

    def _on_tree_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        s = self.servers[int(sel[0])]
        addr = s.playit_addr if s.tunnel_mode == "playit" else s.tunnel_url
        if addr:
            self.clipboard_clear()
            self.clipboard_append(addr)
            messagebox.showinfo("コピーしました", f"アドレスをクリップボードにコピーしました:\n{addr}")

    def browse_command(self):
        path = filedialog.askopenfilename(title="起動するプログラムを選択")
        if path:
            if path.lower().endswith(".py"):
                cmd = f'"{sys.executable}" "{path}"'
            else:
                cmd = f'"{path}"'
            self.entry_cmd.delete(0, tk.END)
            self.entry_cmd.insert(0, cmd)
            if not self.entry_workdir.get():
                self.entry_workdir.insert(0, os.path.dirname(path))

    def browse_workdir(self):
        path = filedialog.askdirectory(title="作業フォルダを選択")
        if path:
            self.entry_workdir.delete(0, tk.END)
            self.entry_workdir.insert(0, path)

    def _refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for idx, s in enumerate(self.servers):
            status = "稼働中" if s.running else "停止中"
            upnp_text = "ON" if s.upnp else "OFF"
            if s.running and s.upnp:
                upnp_text = "OK" if s.upnp_mapped else "失敗"

            if s.tunnel_mode == "none":
                url_text = ""
            elif s.tunnel_mode == "playit":
                if s.running:
                    url_text = s.playit_addr if s.playit_addr else "接続中..."
                else:
                    url_text = "(停止中)"
            elif s.running:
                url_text = s.tunnel_url if s.tunnel_url else "起動中..."
            else:
                url_text = "(停止中)"

            self.tree.insert("", "end", iid=str(idx),
                              values=(s.name, s.port, s.protocol, s.command, upnp_text, url_text, status))

    def _persist(self):
        save_servers([s.to_dict() for s in self.servers])

    def _get_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("選択エラー", "一覧からサーバーを選択してください。")
            return None
        return self.servers[int(sel[0])]

    def check_external_ip(self):
        self.status_label.config(text="外部IPを取得中...", foreground="blue")
        threading.Thread(target=self._check_external_ip_async, daemon=True).start()

    def _check_external_ip_async(self):
        try:
            ip = upnp_client.get_external_ip()
            self.after(0, lambda: self._show_external_ip(ip, None))
        except Exception as e:
            self.after(0, lambda: self._show_external_ip(None, str(e)))

    def _show_external_ip(self, ip, err):
        if ip:
            self.status_label.config(text="管理者権限で実行中(自動取得済み)", foreground="green")
            messagebox.showinfo(
                "外部IPアドレス",
                f"ルーターのグローバルIPアドレス:\n{ip}\n\n"
                f"外部からは http(s)://{ip}:ポート番号 でアクセスできます。\n"
                "(プロバイダによってはCGNATのため接続できない場合があります)"
            )
        else:
            self.status_label.config(text="管理者権限で実行中(自動取得済み)", foreground="green")
            messagebox.showwarning("外部IP取得失敗", f"取得に失敗しました:\n{err}")

    # ---------------------------------------------------------- 登録/削除
    def add_server(self):
        name = self.entry_name.get().strip()
        port_str = self.entry_port.get().strip()
        cmd = self.entry_cmd.get().strip()
        proto = self.combo_proto.get()
        workdir = self.entry_workdir.get().strip()

        if not name or not port_str or not cmd:
            messagebox.showerror("入力エラー", "名前・ポート・起動コマンドは必須です。")
            return

        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            messagebox.showerror("入力エラー", "ポート番号は1〜65535の整数で入力してください。")
            return

        tunnel_label = self.combo_tunnel.get()
        tunnel_mode_map = {
            "なし": "none",
            "Cloudflare Quick Tunnel": "quick",
            "Cloudflare Named Tunnel": "named",
            "Playit.gg (TCP/UDP対応)": "playit",
        }
        tunnel_mode = tunnel_mode_map.get(tunnel_label, "none")
        tunnel_token = self.entry_tunnel_token.get().strip()

        if tunnel_mode == "named" and not tunnel_token:
            messagebox.showerror("入力エラー", "Named Tunnelを選択した場合はトークンを入力してください。")
            return

        if tunnel_mode in ("quick", "named") and not cloudflare_tunnel.is_installed():
            if not messagebox.askyesno("確認",
                "cloudflaredが未インストールです。今すぐダウンロードしますか?\n"
                "(後で「cloudflaredを準備」ボタンからも実行できます)"):
                tunnel_mode = "none"
            else:
                try:
                    cloudflare_tunnel.download_cloudflared()
                except Exception as e:
                    messagebox.showerror("エラー", f"cloudflaredのダウンロードに失敗しました:\n{e}")
                    tunnel_mode = "none"

        if tunnel_mode == "playit" and not playit_agent.is_installed():
            if not messagebox.askyesno("確認",
                "playitが未インストールです。今すぐダウンロードしますか?\n"
                "(後で「playitを準備」ボタンからも実行できます)"):
                tunnel_mode = "none"
            else:
                try:
                    playit_agent.download_playit()
                except Exception as e:
                    messagebox.showerror("エラー", f"playitのダウンロードに失敗しました:\n{e}")
                    tunnel_mode = "none"

        entry = ServerEntry(name, cmd, int(port_str), proto, workdir, self.var_upnp.get(),
                             tunnel_mode, tunnel_token, self.var_local_https.get())
        self.servers.append(entry)
        self._persist()
        self._refresh_list()

        self.entry_name.delete(0, tk.END)
        self.entry_port.delete(0, tk.END)
        self.entry_cmd.delete(0, tk.END)
        self.entry_workdir.delete(0, tk.END)
        self.combo_tunnel.set("なし")
        self.entry_tunnel_token.config(state="normal")
        self.entry_tunnel_token.delete(0, tk.END)
        self.entry_tunnel_token.config(state="disabled")
        self.var_local_https.set(False)

    def delete_selected(self):
        s = self._get_selected()
        if s is None:
            return
        if s.running:
            messagebox.showwarning("削除不可", "稼働中のサーバーは先に停止してください。")
            return
        self.servers.remove(s)
        self._persist()
        self._refresh_list()

    # ---------------------------------------------------------- 開放/起動・閉鎖/停止
    def start_selected(self):
        s = self._get_selected()
        if s is None:
            return
        if s.running:
            messagebox.showinfo("情報", "既に起動しています。")
            return

        try:
            if is_windows():
                s.fw_rule_name = open_port_windows(s.port, s.protocol)
            else:
                open_port_linux(s.port, s.protocol.lower())
        except Exception as e:
            messagebox.showerror("ポート開放エラー", f"ファイアウォール設定に失敗しました:\n{e}")
            return

        try:
            cwd = s.workdir if s.workdir else None
            popen_kwargs = {"shell": True, "cwd": cwd}
            if is_windows():
                # Minecraftサーバー等のコンソール出力が見える専用ウィンドウで起動
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            s.process = subprocess.Popen(s.command, **popen_kwargs)
        except Exception as e:
            messagebox.showerror("起動エラー", f"プログラムの起動に失敗しました:\n{e}")
            # 開放したポートを戻す
            if is_windows() and s.fw_rule_name:
                close_port_windows(s.fw_rule_name)
            else:
                close_port_linux(s.port, s.protocol.lower())
            return

        s.running = True
        s.upnp_mapped = False
        self._refresh_list()

        # プロセス終了監視スレッド
        s.monitor_thread = threading.Thread(target=self._monitor_process, args=(s,), daemon=True)
        s.monitor_thread.start()

        # UPnPでルーターのポートも開放(時間がかかるためバックグラウンド実行)
        if s.upnp:
            threading.Thread(target=self._upnp_open_async, args=(s,), daemon=True).start()

        # Cloudflare Tunnelの起動
        if s.tunnel_mode != "none":
            self._start_tunnel(s)

    def _start_tunnel(self, s: ServerEntry):
        # --- Playit.gg (TCP/UDP) ---
        if s.tunnel_mode == "playit":
            if not playit_agent.is_installed():
                messagebox.showwarning("playit未インストール",
                    "生TCP/UDP公開(playit.gg)を行うにはplayitが必要です。\n"
                    "「playitを準備」ボタンからダウンロードしてください。")
                return

            s.playit_addr = None

            def on_claim_url(url):
                self.after(0, self._on_playit_claim, s, url)

            def on_connected():
                self.after(0, self._on_playit_connected, s)

            def on_tunnel_addr(addr):
                if s.playit_addr != addr:
                    s.playit_addr = addr
                    self.after(0, self._refresh_list)

            def on_exit():
                self.after(0, self._on_playit_exit, s)

            try:
                s.playit_proc = playit_agent.start_agent(
                    on_claim_url=on_claim_url,
                    on_connected=on_connected,
                    on_tunnel_addr=on_tunnel_addr,
                    on_exit=on_exit,
                )
            except Exception as e:
                messagebox.showerror("playit起動エラー", f"playitの起動に失敗しました:\n{e}")
                s.playit_proc = None
            self._refresh_list()
            return

        # --- Cloudflare Tunnel (HTTP/HTTPS) ---
        if not cloudflare_tunnel.is_installed():
            messagebox.showwarning("cloudflared未インストール",
                "外部公開(Cloudflare Tunnel)を行うにはcloudflaredが必要です。\n"
                "「cloudflaredを準備」ボタンからダウンロードしてください。")
            return

        s.tunnel_url = None

        def on_url_found(url):
            s.tunnel_url = url
            self.after(0, self._refresh_list)
            self.after(0, lambda: self._notify_tunnel_url(s, url))

        def on_exit():
            self.after(0, self._on_tunnel_exit, s)

        try:
            if s.tunnel_mode == "quick":
                scheme = "https" if s.local_https else "http"
                s.tunnel_proc = cloudflare_tunnel.start_quick_tunnel(
                    s.port, on_url_found=on_url_found, on_exit=on_exit, origin_scheme=scheme)
            elif s.tunnel_mode == "named":
                s.tunnel_url = "(Named Tunnel - ダッシュボードでURL確認)"
                s.tunnel_proc = cloudflare_tunnel.start_named_tunnel(
                    s.tunnel_token, on_exit=on_exit)
        except Exception as e:
            messagebox.showerror("Tunnel起動エラー", f"Cloudflare Tunnelの起動に失敗しました:\n{e}")
            s.tunnel_proc = None

        self._refresh_list()

    def _on_playit_claim(self, s: ServerEntry, url):
        """初回クレームURL → ブラウザを自動起動してユーザーに承認を促す"""
        playit_agent.open_claim_url(url)
        messagebox.showinfo(
            "playit.gg 初回承認が必要",
            f"ブラウザが開きました。playit.ggにログインして「Claim Agent」を押してください。\n\n"
            f"承認後、playit.ggダッシュボード(https://playit.gg/account/tunnels)で\n"
            f"トンネルを作成し、ローカルアドレスに 127.0.0.1:{s.port} を設定してください。\n\n"
            f"一度承認すれば次回以降は自動で繋がります。"
        )

    def _on_playit_connected(self, s: ServerEntry):
        self._refresh_list()
        if not playit_agent.is_claimed():
            return
        if s.playit_addr:
            return  # アドレスが既に判明していれば通知済み

    def _on_playit_exit(self, s: ServerEntry):
        if not s.running:
            return
        s.playit_proc = None
        s.playit_addr = None
        self._refresh_list()
        messagebox.showwarning("playit停止", f"「{s.name}」のplayitエージェントが終了しました。")

    def _notify_tunnel_url(self, s: ServerEntry, url):
        messagebox.showinfo(
            "公開URL発行",
            f"「{s.name}」の公開URLが発行されました:\n\n{url}\n\n"
            "一覧をダブルクリックするとURLをコピーできます。"
        )

    def _on_tunnel_exit(self, s: ServerEntry):
        if not s.running:
            return
        s.tunnel_proc = None
        s.tunnel_url = None
        self._refresh_list()
        if s.tunnel_mode != "none":
            messagebox.showwarning("Tunnel停止", f"「{s.name}」のCloudflare Tunnelが終了しました。")

    def _stop_tunnel(self, s: ServerEntry):
        if s.tunnel_proc is not None:
            cloudflare_tunnel.stop_tunnel(s.tunnel_proc)
            s.tunnel_proc = None
        s.tunnel_url = None
        if s.playit_proc is not None:
            playit_agent.stop_agent(s.playit_proc)
            s.playit_proc = None
        s.playit_addr = None

    def _upnp_open_async(self, s: ServerEntry):
        try:
            upnp_client.add_port_mapping(s.port, s.protocol, description=f"PortMgr-{s.name}")
            ok = True
            err = None
        except Exception as e:
            ok = False
            err = str(e)
        self.after(0, self._on_upnp_open_done, s, ok, err)

    def _on_upnp_open_done(self, s: ServerEntry, ok, err):
        if not s.running:
            return  # すでに停止済み
        s.upnp_mapped = ok
        self._refresh_list()
        if not ok:
            messagebox.showwarning(
                "UPnP開放失敗",
                f"「{s.name}」のルーターポート開放(UPnP)に失敗しました。\n"
                f"ルーターのUPnP設定が無効か、対応していない可能性があります。\n\n詳細: {err}"
            )

    def _monitor_process(self, s: ServerEntry):
        """プロセスの終了を待ち、終了したらポートを自動で閉鎖する"""
        if s.process is None:
            return
        s.process.wait()  # プロセス終了まで待機

        # UIスレッドへ後処理を依頼
        self.after(0, self._on_process_exit, s)

    def _on_process_exit(self, s: ServerEntry):
        if not s.running:
            return  # 既に手動停止済み

        if is_windows() and s.fw_rule_name:
            close_port_windows(s.fw_rule_name)
        else:
            close_port_linux(s.port, s.protocol.lower())

        if s.upnp and s.upnp_mapped:
            try:
                upnp_client.delete_port_mapping(s.port, s.protocol)
            except Exception:
                pass

        self._stop_tunnel(s)

        s.running = False
        s.process = None
        s.fw_rule_name = None
        s.upnp_mapped = False
        self._refresh_list()
        messagebox.showinfo("終了通知", f"「{s.name}」のプログラムが終了したため、ポート {s.port} を自動的に閉鎖しました。")

    def stop_selected(self):
        s = self._get_selected()
        if s is None:
            return
        if not s.running:
            messagebox.showinfo("情報", "起動していません。")
            return

        s.running = False  # 監視スレッドの自動処理を抑止

        if s.process is not None:
            try:
                s.process.terminate()
            except Exception:
                pass

        if is_windows() and s.fw_rule_name:
            close_port_windows(s.fw_rule_name)
        else:
            close_port_linux(s.port, s.protocol.lower())

        if s.upnp and s.upnp_mapped:
            try:
                upnp_client.delete_port_mapping(s.port, s.protocol)
            except Exception:
                pass

        self._stop_tunnel(s)

        s.process = None
        s.fw_rule_name = None
        s.upnp_mapped = False
        self._refresh_list()

    # ---------------------------------------------------------- 終了処理
    def on_close(self):
        # 起動中のサーバーは全て停止・ポート閉鎖してから終了
        for s in self.servers:
            if s.running:
                s.running = False
                if s.process is not None:
                    try:
                        s.process.terminate()
                    except Exception:
                        pass
                if is_windows() and s.fw_rule_name:
                    close_port_windows(s.fw_rule_name)
                else:
                    close_port_linux(s.port, s.protocol.lower())
                if s.upnp and s.upnp_mapped:
                    try:
                        upnp_client.delete_port_mapping(s.port, s.protocol)
                    except Exception:
                        pass
                self._stop_tunnel(s)
        self._persist()
        self.destroy()


if __name__ == "__main__":
    # Windowsで管理者権限が無い場合は自動的にUACダイアログを出して昇格・再起動する
    if is_windows() and not is_admin():
        try:
            relaunch_as_admin()
        except Exception:
            pass
        else:
            sys.exit(0)

    app = PortManagerApp()
    app.mainloop()
