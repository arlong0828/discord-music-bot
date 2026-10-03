# Discord Music Bot

獨立運作的 Discord 音樂 Bot，支援 YouTube 搜尋、網址、播放清單、投票跳歌與繁體中文 Slash Commands。

## 功能

- `/play`：播放歌名、YouTube 網址或播放清單
- `/queue`：顯示目前曲目與等待佇列
- `/skip`：點播者／管理員直接跳過，其他聽眾採過半投票
- `/pause`、`/resume`
- `/clearqueue`：清空佇列並立即離開語音頻道
- `/leave`：停止並離開
- 歌曲播放完且沒有下一首時，自動離開語音頻道
- 播放前重新取得串流網址，並使用 FFmpeg reconnect 選項降低來源中斷問題

## 安裝

需要 Python 3.11+ 與系統可執行的 `ffmpeg`：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

把 Discord Bot Token 寫入本機 `.env`：

```env
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
DISCORD_GUILD_ID=
```

`DISCORD_GUILD_ID` 可留空；設定後，開發期間 Slash Commands 會立即同步到該伺服器。

啟動：

```bash
python bot.py
```

## Discord Developer Portal

1. 建立 Application 與 Bot。
2. 邀請 Bot 時加入 `bot`、`applications.commands` scopes。
3. Bot 至少需要：View Channels、Send Messages、Connect、Speak。
4. 本專案只使用 Slash Commands，不需要 Message Content Intent。

## 安全

- 真實 Token 只能放在 `.env`；`.env` 已排除於 Git。
- 不要把 Token 寫入程式碼、README、systemd unit 或提交歷史。
- Bot 回覆停用 mentions，避免曲名觸發不必要通知。

## 測試

```bash
python -m unittest discover -s tests -v
```

## 授權

MIT
