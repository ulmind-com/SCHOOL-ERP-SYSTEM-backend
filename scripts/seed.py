"""CLI bootstrap.

    uv run python -m scripts.seed            # plans + platform owner
    uv run python -m scripts.seed --demo     # + a fully populated demo school
    uv run python -m scripts.seed --demo --reset
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.config import settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_mongo_connection, connect_to_mongo

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("seed")

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def line(char: str = "─", width: int = 68) -> str:
    return DIM + char * width + RESET


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the Scholarly database")
    parser.add_argument("--demo", action="store_true", help="also create a demo institution")
    parser.add_argument("--reset", action="store_true",
                        help="DROP the database first (development only)")
    args = parser.parse_args()

    db = await connect_to_mongo()

    if args.reset:
        if settings.is_production:
            raise SystemExit("Refusing to --reset a production database")
        confirm = input(f"{YELLOW}Drop database '{settings.mongodb_db_name}'? [y/N] {RESET}")
        if confirm.strip().lower() != "y":
            raise SystemExit("Cancelled")
        await db.client.drop_database(settings.mongodb_db_name)
        log.info("Dropped %s", settings.mongodb_db_name)

    created = await ensure_indexes()
    log.info("Indexes ensured on %d collections", len(created))

    from scripts.bootstrap import run

    result = await run()

    print()
    print(line())
    print(f"{BOLD}  Scholarly bootstrap complete{RESET}  {DIM}(mode: {settings.deployment_mode}){RESET}")
    print(line())
    print(f"  Plans seeded          : {result['plans_seeded']}")
    if result["platform_owner"]:
        owner = result["platform_owner"]
        state = f"{GREEN}created{RESET}" if owner.get("created") else f"{DIM}exists{RESET}"
        print(f"  Platform owner        : {owner['email']}  [{state}]")
        if owner.get("created"):
            print(f"  Platform password     : {owner['password']}")
    if result["dedicated_tenant"]:
        ded = result["dedicated_tenant"]
        print(f"  Dedicated institution : {ded['slug']}")
        if ded.get("created"):
            print(f"  Licence key           : {ded['license_key']}")
            print(f"  Owner login           : {ded['owner_email']} / {ded['owner_password']}")

    if args.demo:
        from scripts.demo_data import seed_demo

        demo = await seed_demo()
        print(line())
        print(f"{BOLD}  Demo institution{RESET}")
        print(line())
        print(f"  Name                  : {demo['name']}")
        print(f"  Sign-in address       : {demo['slug']}")
        for label, creds in demo["logins"].items():
            print(f"  {label:22}: {creds}")
        print(f"  Records               : {demo['counts']}")

    print(line())
    print(f"  API   {DIM}uv run uvicorn app.main:app --reload{RESET}")
    print(f"  Docs  {DIM}http://localhost:{settings.backend_port}/docs{RESET}")
    print()

    await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(main())
