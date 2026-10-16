# WoodooGifi Telegram Bot

Telegram referral bot built with aiogram 3, PostgreSQL and aiohttp, prepared for Render.

## Features

- User profile, internal Stars balance and referral statistics.
- Personal referral links (`/start <telegram_id>`).
- Referral reward is granted once after the invited user satisfies all configured sponsor-channel subscriptions.
- Admin panel: edit balances, change referral reward, add/remove sponsor channels.
- Withdrawal requests are sent to configured admins. Rejected requests return the reserved balance.
- `/health` and `/` HTTP endpoints for Render health checks.

## Deploy to Render

1. Push this repository to GitHub and create a new Blueprint on Render using `render.yaml`.
2. In the Render service environment, set `BOT_TOKEN` to the token from BotFather.
3. Set `ADMIN_IDS` to one or more numeric Telegram user IDs, separated by commas, for example `123456789,987654321`.
4. Optionally set `REFERRAL_REWARD` (default `3`) and `MIN_REFERRALS` (default `5`).
5. Deploy. Render will create the PostgreSQL database and inject `DATABASE_URL` from the blueprint.

If the database is created separately instead of through the Blueprint, set `DATABASE_URL` to its connection string.

## Sponsor channels

- Add the bot as an administrator to each channel so Telegram lets it verify memberships.
- In the admin panel, add the numeric channel ID, public or invite URL, and title in the format `ID | https://t.me/channel | Title`.
- For private channels, use a valid invite link. The bot must still be able to check members.

## Important about Stars

The bot tracks an **internal reward balance** and creates withdrawal requests. Approving a request does not itself transfer real Telegram Stars; an administrator must carry out the actual payout separately.

## Notes

- Keep `BOT_TOKEN` and database credentials private; do not commit them to the repository.
- The bot uses long polling and runs the HTTP health server in the same Render web service.
- Use a persistent PostgreSQL database; the in-memory FSM storage is used only for temporary conversation steps and resets on restart.
