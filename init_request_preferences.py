import argparse
import json
import logging
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import yaml
from folioclient import FolioClient

log = logging.getLogger(__name__)

PREFS_PATH = "/request-preference-storage/request-preference"
COMPARABLE_FIELDS = [
    "holdShelf",
    "fulfillment",
    "delivery",
    "defaultDeliveryAddressTypeId",
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize FOLIO user request preferences."
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["all_users", "new_users"],
        help="'all_users' processes every user by ID prefix; 'new_users' processes only users created since the last run",
    )
    prefix_group = parser.add_mutually_exclusive_group()
    prefix_group.add_argument(
        "--start-prefix",
        default=None,
        metavar="NN",
        help="Two-digit prefix to start from in --mode all_users, overriding saved state (default: resume from state or '00')",
    )
    prefix_group.add_argument(
        "--prefix",
        default=None,
        metavar="NN",
        help="Process only this single two-digit prefix in --mode all_users",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Log what would be created/updated without making any changes",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        log.error("Failed to parse config file %s: %s", config_path, exc)
        sys.exit(1)
    for key in ("folio", "preferences", "state_file"):
        if key not in cfg:
            log.error("Missing required config key '%s' in %s", key, config_path)
            sys.exit(1)
    for key in ("okapi_url", "tenant", "username", "password"):
        if key not in cfg["folio"]:
            log.error("Missing required folio config key '%s' in %s", key, config_path)
            sys.exit(1)
    for key in ("privileged_patron_groups", "campus_address_type"):
        if key not in cfg["preferences"]:
            log.error(
                "Missing required preferences config key '%s' in %s", key, config_path
            )
            sys.exit(1)
    return cfg


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        with open(state_path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning(
            "Could not read state file %s, falling back to init mode: %s",
            state_path,
            exc,
        )
        return {}


def save_state(state_path: Path, state: dict) -> None:
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved: %s", state)


def fetch_reference_data(
    fc: FolioClient,
    privileged_group_names: list[str],
    campus_address_type_name: str,
) -> tuple[set[str], str, dict[str, str]]:
    groups = list(
        fc.folio_get_all("/groups", "usergroups", "cql.allRecords=1", limit=500)
    )
    patron_group_map = {g["id"]: g["group"] for g in groups}
    privileged_group_ids = {
        g["id"] for g in groups if g["group"] in privileged_group_names
    }
    log.info(
        "Resolved %d privileged group ID(s) for: %s",
        len(privileged_group_ids),
        privileged_group_names,
    )
    if not privileged_group_ids:
        log.warning(
            "No patron groups matched privileged_patron_groups config: %s",
            privileged_group_names,
        )

    address_types = list(
        fc.folio_get_all("/addresstypes", "addressTypes", "cql.allRecords=1", limit=500)
    )
    campus_address_type_id = next(
        (
            a["id"]
            for a in address_types
            if a["addressType"] == campus_address_type_name
        ),
        None,
    )
    if campus_address_type_id is None:
        log.error(
            "Campus address type '%s' not found in FOLIO. "
            "Check the campus_address_type value in config.yaml.",
            campus_address_type_name,
        )
        sys.exit(1)
    log.info(
        "Campus address type '%s' resolved to ID %s",
        campus_address_type_name,
        campus_address_type_id,
    )

    return privileged_group_ids, campus_address_type_id, patron_group_map


def _fetch_users_for_prefix(fc: FolioClient, prefix: str, limit: int) -> list[dict]:
    encoded_query = urllib.parse.quote(f'id="{prefix}*"', safe="=*")
    response = fc.folio_get(f"/users?limit={limit}&query={encoded_query}")
    total = response.get("totalRecords", 0)
    if total > limit:
        log.error(
            "Batch '%s': totalRecords=%d exceeds limit=%d; "
            "increase user_limit in config.yaml.",
            prefix,
            total,
            limit,
        )
        sys.exit(1)
    return response.get("users", [])


def process_users(
    fc: FolioClient,
    users: list[dict],
    privileged_group_ids: set[str],
    campus_address_type_id: str,
    patron_group_map: dict[str, str],
    report_only: bool,
) -> tuple[int, int, int, int]:
    """Process a list of users. Returns (created, updated, skipped, errored)."""
    created = updated = skipped = errored = 0
    for user in users:
        user_id = user.get("id")
        try:
            desired = compute_desired_prefs(
                user, privileged_group_ids, campus_address_type_id
            )
            existing = get_existing_prefs(fc, user_id)
            if existing is None:
                barcode = user.get("barcode", "N/A")
                group_name = patron_group_map.get(
                    user.get("patronGroup", ""), "unknown"
                )
                if report_only:
                    log.info(
                        "[REPORT ONLY] Would create preferences for user %s (barcode=%s group=%s)",
                        user_id,
                        barcode,
                        group_name,
                    )
                else:
                    post_prefs(fc, user_id, desired)
                    log.info(
                        "Created preferences for user %s (barcode=%s group=%s)",
                        user_id,
                        barcode,
                        group_name,
                    )
                created += 1
            elif prefs_differ(existing, desired):
                if report_only:
                    log.info(
                        "[REPORT ONLY] Would update preferences for user %s", user_id
                    )
                else:
                    put_prefs(fc, existing["id"], user_id, desired)
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            log.error("Error processing user %s: %s", user_id, exc)
            errored += 1
    return created, updated, skipped, errored


def compute_desired_prefs(
    user: dict,
    privileged_group_ids: set[str],
    campus_address_type_id: str,
) -> dict:
    patron_group_id = user.get("patronGroup")
    addresses = user.get("personal", {}).get("addresses", [])

    desired: dict = {
        "holdShelf": True,
        "fulfillment": "Hold Shelf",
        "delivery": False,
    }

    if patron_group_id in privileged_group_ids:
        campus_address = next(
            (a for a in addresses if a.get("addressTypeId") == campus_address_type_id),
            None,
        )
        if campus_address is not None:
            desired["delivery"] = True
            desired["defaultDeliveryAddressTypeId"] = campus_address_type_id

    return desired


def get_existing_prefs(fc: FolioClient, user_id: str) -> dict | None:
    prefs = fc.folio_get(
        PREFS_PATH, key="requestPreferences", query=f"userId=={user_id}"
    )
    return prefs[0] if prefs else None


def prefs_differ(existing: dict, desired: dict) -> bool:
    for field in COMPARABLE_FIELDS:
        if desired.get(field) != existing.get(field):
            return True
    return False


def post_prefs(fc: FolioClient, user_id: str, desired: dict) -> None:
    payload = {"userId": user_id, **desired}
    fc.folio_post(PREFS_PATH, payload)


def put_prefs(fc: FolioClient, pref_id: str, user_id: str, desired: dict) -> None:
    payload = {"id": pref_id, "userId": user_id, **desired}
    fc.folio_put(f"{PREFS_PATH}/{pref_id}", payload)
    log.info("Updated preferences for user %s (pref %s)", user_id, pref_id)


def main() -> None:
    setup_logging()
    args = parse_args()

    script_dir = Path(__file__).parent
    config = load_config(script_dir / "config.yaml")

    folio_cfg = config["folio"]
    try:
        fc = FolioClient(
            folio_cfg["okapi_url"],
            folio_cfg["tenant"],
            folio_cfg["username"],
            folio_cfg["password"],
        )
    except Exception as exc:
        log.error("Failed to connect to FOLIO: %s", exc)
        sys.exit(1)

    prefs_cfg = config["preferences"]
    privileged_group_ids, campus_address_type_id, patron_group_map = (
        fetch_reference_data(
            fc,
            prefs_cfg["privileged_patron_groups"],
            prefs_cfg["campus_address_type"],
        )
    )

    state_path = script_dir / config["state_file"]
    state = load_state(state_path)
    limit = config.get("user_limit", 10000)

    # Capture start time before any processing to avoid timestamp gaps
    run_start_ts = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"
    )

    total_created = total_updated = total_skipped = total_errored = 0

    if args.mode == "new_users":
        if "last_run" not in state:
            log.error(
                "Cannot run with --mode new_users: no last_run found in state file. "
                "Run with --mode all_users first."
            )
            sys.exit(1)
        last_run_ts = state["last_run"]
        log.info("Incremental mode: fetching users created since %s", last_run_ts)
        query = f'metadata.createdDate > "{last_run_ts}"'
        try:
            users = list(fc.folio_get_all("/users", "users", query, limit=limit))
        except Exception as exc:
            log.error("Failed to fetch users: %s", exc)
            sys.exit(1)
        log.info("Fetched %d user(s)", len(users))
        c, u, s, e = process_users(
            fc,
            users,
            privileged_group_ids,
            campus_address_type_id,
            patron_group_map,
            args.report_only,
        )
        total_created, total_updated, total_skipped, total_errored = c, u, s, e
        if not args.report_only:
            save_state(state_path, {"last_run": run_start_ts})
    else:
        # --mode all_users — batch init by ID prefix
        if "last_run" in state:
            log.error(
                "Cannot run with --mode all_users: last_run already present in state file. "
                "Delete %s to force re-initialization.",
                state_path,
            )
            sys.exit(1)
        if args.prefix is not None:
            prefixes = [args.prefix]
            log.info("Init mode: single prefix batch '%s'", args.prefix)
        else:
            if args.start_prefix is not None:
                start = args.start_prefix
            elif "init_next_prefix" in state:
                start = state["init_next_prefix"]
            else:
                start = "00"
            prefixes = [f"{n:02x}" for n in range(int(start, 16), 256)]
            log.info(
                "Init mode: %d prefix batch(es) starting at '%s'", len(prefixes), start
            )

        for i, prefix in enumerate(prefixes):
            log.info("Batch '%s': starting", prefix)
            try:
                batch = _fetch_users_for_prefix(fc, prefix, limit)
            except Exception as exc:
                log.error("Batch '%s': failed to fetch: %s", prefix, exc)
                sys.exit(1)
            log.info("Batch '%s': fetched %d user(s)", prefix, len(batch))

            c, u, s, e = process_users(
                fc,
                batch,
                privileged_group_ids,
                campus_address_type_id,
                patron_group_map,
                args.report_only,
            )
            total_created += c
            total_updated += u
            total_skipped += s
            total_errored += e

            log.info(
                "Batch '%s': done. created=%d updated=%d skipped=%d errored=%d",
                prefix,
                c,
                u,
                s,
                e,
            )

            if not args.report_only and args.prefix is None:
                is_last = i == len(prefixes) - 1
                next_state = (
                    {"last_run": run_start_ts}
                    if is_last
                    else {"init_next_prefix": prefixes[i + 1]}
                )
                save_state(state_path, next_state)

    log.info(
        "Done. created=%d updated=%d skipped=%d errored=%d",
        total_created,
        total_updated,
        total_skipped,
        total_errored,
    )
    if args.report_only:
        log.info("Report-only mode: no changes were made and state was not saved.")


if __name__ == "__main__":
    main()
