# WoodooGifi Telegram Bot

## Render setup
1. Create a Telegram bot with BotFather and copy its token.
2. In Render, create a Blueprint from this repository, or create a Python worker.
3. Set secret environment variable `BOT_TOKEN`. `ADMIN_USERNAME` defaults to `xoyli`.
4. Keep the persistent disk mounted at `/var/data` so the SQLite database survives deploys.
5. Add the bot as an administrator to every required channel.

## Admin commands (only Telegram username @xoyli)
- `/stats` — overall statistics
- `/setreward 3` — referral reward
- `/addchannel @channel https://t.me/channel` — require channel subscription
- `/delchannel @channel` — remove a required channel
- `/channels` — list required channels
- `/credit USER_ID AMOUNT` — change user balance (negative values deduct)
- `/withdrawals` — pending withdrawal requests
- `/approve ID` / `/reject ID` — process a withdrawal request

A referral is rewarded only after the invited user is confirmed subscribed to all configured channels. The reward is credited once only. Bot must be an admin in the channels for reliable membership checks.

**Important:** the balance is an internal ledger, not an automatic Telegram Stars transfer. Withdrawal requests are reviewed manually.
