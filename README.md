# fetchxh

`fetchxh` is a terminal-first X home timeline reader that pulls raw text over HTTP.

It reads the `For You` and `Following` timelines, extracts post text from the GraphQL payload, and can renew the local X session when the stored request state goes stale.

## Features

- Pure HTTP timeline reads with `requests`
- Text-focused terminal output
- Automatic retry after stale session/query-id failures
- Interactive `Renew auth` menu action
- Local session export to `x_state.json` without committing secrets

## Security

This repository intentionally does **not** ship any live cookies, CSRF tokens, auth tokens, or account-specific state.

- Runtime session state is loaded from `%LOCALAPPDATA%\\fetchxh\\x_state.json`
- Legacy state from `%LOCALAPPDATA%\\fetchx` is also detected for migration
- You can override paths and request metadata with `FETCHXH_*` environment variables
- Do not commit `x_state.json`, browser profiles, or exported cookies

More details are in [`SECURITY.md`](SECURITY.md).

## Installation

Base install:

```bash
python -m pip install -e .
```

Install the optional browser-based renewal flow:

```bash
python -m pip install -e .[renew]
```

## Usage

```bash
fetchxh
fetchxh --count 20
python -m fetchxh --count 20
```

Menu actions:

- `1` reads `For You` and `Following`
- `2` renews the local X session and refreshes query ids
- `q` exits

## Configuration

`fetchxh` discovers runtime state in this order:

1. `FETCHXH_*` environment variables
2. Local exported session state from `x_state.json`
3. Chrome code-cache scans for current GraphQL query ids and bearer metadata

Supported environment variables:

- `FETCHXH_HOME`
- `FETCHXH_X_STATE_PATH`
- `FETCHXH_HOME_TIMELINE_QUERY_ID`
- `FETCHXH_HOME_LATEST_TIMELINE_QUERY_ID`
- `FETCHXH_AUTHORIZATION_BEARER`
- `FETCHXH_X_CSRF_TOKEN`
- `FETCHXH_COOKIE_HEADER`
- `FETCHXH_USER_AGENT`
- `FETCHXH_CHROME_BIN`
- `FETCHXH_CHROME_VERSION`

## Development

Run tests:

```bash
python -m unittest discover -s tests -v
```

## Notes

- The X HomeTimeline query ids change over time.
- `Renew auth` is optional and requires the `renew` extra.
- The tool is intended for local, personal session use.
