# Security Notes

`fetchxh` works with live local X session data. Treat that data as sensitive.

## Never commit

- `x_state.json`
- `browser_profile/`
- exported cookies
- copied request headers from a real account
- screenshots or logs that expose tokens or account identifiers

## Where local state lives

By default:

- `%LOCALAPPDATA%\fetchxh\x_state.json`
- `%LOCALAPPDATA%\fetchxh\browser_profile\`

For migration, the app can also read legacy state from `%LOCALAPPDATA%\fetchx`.

## Safe sharing checklist

Before publishing code or opening a pull request:

1. Search for `auth_token`, `ct0`, `Bearer `, and `x_state.json`.
2. Confirm `.gitignore` is active.
3. Review `git diff --cached` before every commit.
4. Do not paste live cookies into issues, README files, or tests.

## Reporting

If you discover a leak in this repository, rotate the affected X session immediately and remove the leaked data from history before sharing the repo further.
