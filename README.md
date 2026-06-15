# Port_maneger

`Port_maneger` は、ポート開放、ルーター設定、そして Cloudflare を利用したトンネリングを自動化・簡略化するための Python ツールです。複雑なネットワーク設定を簡単な操作で完結させることを目指しています。

## 🚀 主な機能

* **自動ポート開放 & ルーター設定**: 手動での面倒なルーター設定を自動化。
* **UPnP 対応**: UPnP（Universal Plug and Play）を利用したスムーズなポートマッピング。
* **Cloudflare トンネルの統合**:
    * **Quick Tunnel**: アカウント不要で一時的なパブリックURLを即座に発行。
    * **Named Tunnel**: 固定のドメインや設定を維持したセキュアなトンネル構築。

## 📋 前提条件

本ツールの実行には **Python** が必須です。

* Python 3.8 以上

## ⚙️ インストールと使い方

### 1. リポジトリのクローン
```bash
git clone [https://github.com/](https://github.com/)[ユーザー名]/Port_maneger.git
cd Port_maneger
