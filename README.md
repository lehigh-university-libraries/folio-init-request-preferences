# folio-init-request-preferences

A utility to initialize and monitor FOLIO user request preferences, ensuring consistent delivery and hold shelf settings across all patron accounts.

## Overview

On first run (`--mode all_users`) the script processes every user account in FOLIO, setting default request preferences. It then runs on a cron schedule (`--mode new_users`) to apply the same defaults to newly-created accounts only. Circulation staff may edit a user's preferences after the fact without risk of them being reset.

For every user processed, the script sets:

| Field | Value |
|---|---|
| Hold Shelf | Enabled |
| Fulfillment | Hold Shelf |
| Default pickup service point | Not set |
| Delivery | Enabled (faculty, staff, graduate only) |
| Default delivery address | Campus (if the user has a campus address) |

## Requirements

- Python 3.10+
- A FOLIO service account with the following permissions:
  - Circulation storage - get request preference collection (`circulation-storage.request-preferences.collection.get`)
  - Circulation storage - post individual request preference (`circulation-storage.request-preferences.item.post`)
  - Circulation storage - put individual request preference (`circulation-storage.request-preferences.item.put`)
  - Settings (Users): Can view address types (`ui-users.settings.addresstypes.view`)
  - Settings (Users): Can view patron groups (`ui-users.settings.usergroups.view`)
  - Users: Can view user profile (`ui-users.view`)

## Setup

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your FOLIO credentials and site-specific values. See [Configuration](#configuration) below.

## Usage

### Initial run — all users

Process every user account. Must be run before `--mode new_users` is used.

```bash
python init_request_preferences.py --mode all_users
```

Due to the number of user records, this processes accounts in batches by the first two hex characters of the user ID (`00` through `ff`). Progress is saved to `state.json` after each batch completes, so if the job is interrupted it can be resumed automatically by re-running the same command.

To resume from a specific batch (e.g. if you need to skip ahead):

```bash
python init_request_preferences.py --mode all_users --start-prefix 42
```

To process a single prefix without touching `state.json` (useful for spot-checks or re-processing one batch):

```bash
python init_request_preferences.py --mode all_users --prefix 42
```

`--prefix` and `--start-prefix` are mutually exclusive.

### Ongoing runs — new users only

Process only accounts created since the last run. Intended to run on a cron schedule (every 15 minutes is typical).

```bash
python init_request_preferences.py --mode new_users
```

This mode requires that `--mode all_users` has been run at least once (it checks for `last_run` in `state.json` and aborts if it is absent).

### Report-only mode

Preview what the script would change without making any modifications. Works with either mode.

```bash
python init_request_preferences.py --mode all_users --report-only
python init_request_preferences.py --mode new_users --report-only
```

## Configuration

Copy `config.yaml.example` to `config.yaml` and fill in the values. The file is excluded from version control.


## State file

`state.json` is created automatically and tracks run progress. It is excluded from version control.

| State | Meaning |
|---|---|
| `{"init_next_prefix": "43"}` | `all_users` run in progress; next run resumes from prefix `43` |
| `{"last_run": "2026-05-14T..."}` | `all_users` complete; `new_users` mode is now available |

To force a full re-initialization, delete `state.json` and re-run with `--mode all_users`.

## Cron example

```
*/15 * * * * cd /path/to/folio-init-request-preferences && python init_request_preferences.py --mode new_users >> /var/log/folio-init-request-preferences.log 2>&1
```
