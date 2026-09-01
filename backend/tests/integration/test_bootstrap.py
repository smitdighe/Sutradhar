"""Bootstrap, seeding, and the first-admin script.

The property that matters most here is idempotency. These scripts get run
against a database somebody is already demoing on, often twice by accident, and
a second run that duplicates categories or crashes on a unique violation is a
worse outcome than one that never ran.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from seeds import SEEDS_DIR, seed_password
from seeds.hashing import SEED_HASH_VERSION
from seeds.loader import load_categories, load_items, load_reputation, load_users
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.attestation import Attestation
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.enums import ItemEventType, UserRole, UserStatus
from app.db.models.scan import Scan
from app.db.models.user import User
from app.provenance.item_hash import PREIMAGE_VERSION

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
API = get_settings().api_prefix


async def count(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def seed_everything(session: AsyncSession) -> None:
    await load_categories(session)
    await load_users(session)
    await load_items(session)
    # Last: flagging an actor disputes everything they registered, so it runs
    # after the items exist. Applied through the real flag_actor path, so the
    # seeded database holds a state the application can actually produce.
    await load_reputation(session)
    await session.commit()


# ---------------------------------------------------------------- category files


class TestCategorySchemas:
    @pytest.mark.parametrize(
        "path", sorted((SEEDS_DIR / "categories").glob("*.json")), ids=lambda p: p.stem
    )
    def test_is_legal_draft_2020_12(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        # check_schema, not validate: this asserts the schema itself is legal,
        # which is what a JSONB column will happily accept and never check.
        Draft202012Validator.check_schema(doc["attribute_schema"])

    @pytest.mark.parametrize(
        "path", sorted((SEEDS_DIR / "categories").glob("*.json")), ids=lambda p: p.stem
    )
    def test_is_closed_and_has_required(self, path: Path) -> None:
        schema = json.loads(path.read_text(encoding="utf-8"))["attribute_schema"]
        # An open schema would silently accept junk attributes on a GI record.
        assert schema["additionalProperties"] is False
        assert schema["required"]
        assert set(schema["required"]) <= set(schema["properties"])

    def test_there_are_exactly_three(self) -> None:
        assert len(list((SEEDS_DIR / "categories").glob("*.json"))) == 3

    def test_one_category_is_not_a_textile(self) -> None:
        # The platform claim in one assertion. If somebody drops Kolhapuri for
        # time, this is what says no.
        docs = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (SEEDS_DIR / "categories").glob("*.json")
        ]
        non_textile = [doc for doc in docs if not doc["is_textile"]]
        assert len(non_textile) == 1
        assert non_textile[0]["slug"] == "kolhapuri-chappal"
        assert non_textile[0]["quantity_unit"] == "pair"

    def test_the_three_categories_have_genuinely_different_shapes(self) -> None:
        # Three near-identical schemas would prove nothing about the engine
        # being category-agnostic.
        by_slug = {
            json.loads(path.read_text(encoding="utf-8"))["slug"]: json.loads(
                path.read_text(encoding="utf-8")
            )["attribute_schema"]["properties"]
            for path in (SEEDS_DIR / "categories").glob("*.json")
        }
        patola = set(by_slug["patola-silk"])
        sambalpuri = set(by_slug["sambalpuri-bandha"])
        kolhapuri = set(by_slug["kolhapuri-chappal"])

        assert patola != sambalpuri
        assert patola & kolhapuri == set()
        # Different types where the shapes do differ, not just different names.
        assert by_slug["patola-silk"]["double_ikat"]["type"] == "boolean"
        assert by_slug["sambalpuri-bandha"]["motif_set"]["type"] == "array"
        assert by_slug["kolhapuri-chappal"]["sole_thickness_mm"]["type"] == "number"


# ---------------------------------------------------------------- seeding


class TestSeeding:
    async def test_empty_database_gets_the_full_dataset(self, session: AsyncSession) -> None:
        await seed_everything(session)

        assert await count(session, GICategory) == 3
        assert await count(session, User) == 7
        assert await count(session, Item) == 7
        assert await count(session, Attestation) == 5
        assert await count(session, Scan) == 6

    async def test_running_twice_changes_nothing(self, session: AsyncSession) -> None:
        await seed_everything(session)
        before = {
            model.__name__: await count(session, model)
            for model in (GICategory, User, Item, Attestation, Scan, ItemEvent)
        }

        await seed_everything(session)
        after = {
            model.__name__: await count(session, model)
            for model in (GICategory, User, Item, Attestation, Scan, ItemEvent)
        }
        assert before == after

    async def test_the_split_tree_does_not_add_up(self, session: AsyncSession) -> None:
        # 12.0m bolt, two 5.5m sarees, 1.0m unallocated. A tidy tree would
        # prove nothing; the remainder is the point.
        await seed_everything(session)

        bolt = (
            await session.execute(
                select(Item).where(Item.parent_id.is_(None), Item.quantity == 12)
            )
        ).scalar_one()
        children = (
            (await session.execute(select(Item).where(Item.parent_id == bolt.id)))
            .scalars()
            .all()
        )
        assert len(children) == 2
        assert sum(child.quantity for child in children) == 11
        assert bolt.quantity - sum(child.quantity for child in children) == 1

    async def test_children_commit_to_their_parent(self, session: AsyncSession) -> None:
        # A child's hash includes the parent's hash, so altering a bolt after
        # cutting sarees from it breaks every child.
        await seed_everything(session)
        children = (
            (await session.execute(select(Item).where(Item.parent_id.is_not(None))))
            .scalars()
            .all()
        )
        assert len(children) == 2
        assert children[0].item_hash != children[1].item_hash
        assert all(child.item_hash.startswith("0x") for child in children)

    async def test_item_hashes_are_real_and_unique(self, session: AsyncSession) -> None:
        # Real keccak digests from the Phase 1 hasher, not invented hex.
        await seed_everything(session)
        hashes = (await session.execute(select(Item.item_hash))).scalars().all()
        assert len(set(hashes)) == len(hashes)
        assert all(len(value) == 66 and value.startswith("0x") for value in hashes)

    async def test_the_inspected_item_has_three_attestations(
        self, session: AsyncSession
    ) -> None:
        await seed_everything(session)
        sambalpuri = (
            await session.execute(
                select(func.count())
                .select_from(Attestation)
                .join(Item, Item.id == Attestation.item_id)
                .where(Item.quantity == 6)
            )
        ).scalar_one()
        # weaver + co-op officer + inspector = the INSPECTED trust level.
        assert sambalpuri == 3

    async def test_the_self_declared_item_has_one_attestation(
        self, session: AsyncSession
    ) -> None:
        await seed_everything(session)
        kolhapuri = (
            await session.execute(
                select(Item)
                .join(GICategory, GICategory.id == Item.category_id)
                .where(GICategory.slug == "kolhapuri-chappal")
            )
        ).scalar_one()
        attestations = (
            await session.execute(
                select(func.count())
                .select_from(Attestation)
                .where(Attestation.item_id == kolhapuri.id)
            )
        ).scalar_one()
        assert attestations == 1

    async def test_the_flagged_weaver_has_items_and_they_are_already_disputed(
        self, session: AsyncSession
    ) -> None:
        # Phase 8 propagates reputation from a flagged weaver to their items.
        # Without items, that has nothing to propagate to -- and a seeded flag
        # that left the items reading clean would be a state the application
        # itself can never reach, which is a trap for whoever debugs against it.
        from app.db.models.attestation import ItemDispute
        from app.db.models.enums import DisputeStatus

        await seed_everything(session)
        flagged = (
            await session.execute(select(User).where(User.fraud_flagged_at.is_not(None)))
        ).scalar_one()
        assert flagged.role is UserRole.WEAVER

        owned = (
            (
                await session.execute(
                    select(Item).where(Item.registered_by == flagged.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(owned) >= 1
        assert all(item.dispute_status is DisputeStatus.DISPUTED for item in owned)

        disputes = (
            (
                await session.execute(
                    select(ItemDispute).where(ItemDispute.triggered_by == flagged.id)
                )
            )
            .scalars()
            .all()
        )
        # Recorded with a source and a trigger, so clearing the flag can lift
        # exactly these and nothing else.
        assert len(disputes) == len(owned)
        assert all(dispute.cleared_at is None for dispute in disputes)

    async def test_the_scan_baseline_is_single_region(self, session: AsyncSession) -> None:
        # Phase 11's anomaly detector needs a believable baseline to deviate
        # from, and no raw location beyond a region ever gets stored.
        await seed_everything(session)
        scans = (await session.execute(select(Scan))).scalars().all()
        assert len(scans) == 6
        assert {scan.region_code for scan in scans} == {"IN-OD"}
        assert all(scan.ip_hash is not None and len(scan.ip_hash) == 64 for scan in scans)

    async def test_seeded_weavers_are_active(self, session: AsyncSession) -> None:
        # Pre-verified so a demo does not stall on a verification step.
        await seed_everything(session)
        weavers = (
            (await session.execute(select(User).where(User.role == UserRole.WEAVER)))
            .scalars()
            .all()
        )
        assert len(weavers) == 4
        assert all(weaver.status is UserStatus.ACTIVE for weaver in weavers)

    async def test_every_role_is_represented(self, session: AsyncSession) -> None:
        await seed_everything(session)
        roles = {
            user.role for user in (await session.execute(select(User))).scalars().all()
        }
        # ADMIN is deliberately absent: create_admin.py is the only path.
        assert roles == {
            UserRole.WEAVER,
            UserRole.COOP_OFFICER,
            UserRole.INSPECTOR,
            UserRole.CONSUMER,
        }

    async def test_seed_files_carry_no_admin(self) -> None:
        users = json.loads((SEEDS_DIR / "users.json").read_text(encoding="utf-8"))["users"]
        assert all(spec["role"] != "ADMIN" for spec in users)

    async def test_no_password_appears_in_the_seed_files(self) -> None:
        # Passwords come from SEED_USER_PASSWORD or the documented dev default,
        # never from a file in the repository.
        for name in ("users.json", "items.json"):
            text = (SEEDS_DIR / name).read_text(encoding="utf-8").lower()
            assert "password" not in text or '"password"' not in text

    async def test_seeded_users_can_log_in(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # The seed path and the login path must agree on hashing. If seeding
        # ever wrote a hash by another route, this is what catches it.
        await seed_everything(session)
        weaver = (
            await session.execute(
                select(User).where(User.email == "ramesh.patel@patanweavers.example.com")
            )
        ).scalar_one()

        response = await client.post(
            f"{API}/auth/login", json={"email": weaver.email, "password": seed_password()}
        )
        assert response.status_code == 200
        assert response.json()["user"]["role"] == UserRole.WEAVER


# ---------------------------------------------------------------- create_admin


def run_script(name: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a script as a subprocess, pointed at the TEST database.

    The scripts read DATABASE_URL, which normally means the development
    database. Overriding it here is what makes "creates nothing" an assertion
    about the same rows the test is counting, rather than a coincidence.
    """
    import os

    settings = get_settings()
    assert settings.test_database_url, "TEST_DATABASE_URL is required for these tests"
    merged = {
        **os.environ,
        "DATABASE_URL": settings.test_database_url,
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, f"scripts/{name}"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


class TestCreateAdmin:
    async def test_creates_exactly_one_admin_and_is_idempotent(
        self, session: AsyncSession
    ) -> None:
        first = run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": "a-real-dev-password"})
        second = run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": "a-real-dev-password"})

        assert first.returncode == 0, first.stdout + first.stderr
        assert second.returncode == 0, second.stdout + second.stderr
        assert "admin created" in first.stdout
        assert "already exists" in second.stdout
        assert "no changes made" in second.stdout

        admins = (
            (await session.execute(select(User).where(User.role == UserRole.ADMIN)))
            .scalars()
            .all()
        )
        assert len(admins) == 1
        assert admins[0].status is UserStatus.ACTIVE

    async def test_second_run_does_not_rewrite_the_password_hash(
        self, session: AsyncSession
    ) -> None:
        # Re-running a bootstrap script must never silently reset a live
        # admin's credentials.
        run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": "a-real-dev-password"})
        admin = (
            await session.execute(select(User).where(User.role == UserRole.ADMIN))
        ).scalar_one()
        original = admin.password_hash

        run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": "a-completely-different-one"})
        await session.refresh(admin)
        assert admin.password_hash == original

    async def test_the_seeded_admin_can_log_in(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # The script path and the login path must agree on hashing.
        password = "a-real-dev-password"
        run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": password})
        admin = (
            await session.execute(select(User).where(User.role == UserRole.ADMIN))
        ).scalar_one()

        response = await client.post(
            f"{API}/auth/login", json={"email": admin.email, "password": password}
        )
        assert response.status_code == 200
        assert response.json()["user"]["role"] == UserRole.ADMIN

    async def test_short_password_is_refused_and_creates_nothing(
        self, session: AsyncSession
    ) -> None:
        before = await count(session, User)
        result = run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": "8charact"})

        assert result.returncode == 1
        assert "minimum is 12" in result.stdout
        assert await count(session, User) == before

    async def test_empty_password_is_refused(self, session: AsyncSession) -> None:
        result = run_script("create_admin.py", {"SEED_ADMIN_PASSWORD": ""})
        assert result.returncode == 1
        assert "not set" in result.stdout

    async def test_production_placeholder_is_refused(self) -> None:
        result = run_script(
            "create_admin.py",
            {"APP_ENV": "production", "SEED_ADMIN_PASSWORD": "change_me_locally"},
        )
        assert result.returncode == 1
        assert "placeholder" in result.stdout


class TestSeedPasswordPolicy:
    def test_dev_default_meets_the_password_policy(self) -> None:
        # Seeded accounts log in through the real login path, so the seed
        # password has to satisfy the same policy every user does.
        from app.auth.password import MIN_PASSWORD_LENGTH

        assert len(seed_password()) >= MIN_PASSWORD_LENGTH

    def test_dev_default_is_not_production_looking(self) -> None:
        from seeds import DEV_PASSWORD

        assert "dev" in DEV_PASSWORD.lower()


class TestItemEventProvenance:
    async def test_every_item_records_its_seed_key_and_hash_version(
        self, session: AsyncSession
    ) -> None:
        # The hash version is written into the database, not only into a
        # docstring, so a row's provenance is identifiable from the row itself.
        # Phase 4 seeded "seed-v1" against a provisional preimage; Phase 6
        # replaced that with the real hasher, and this now tracks
        # PREIMAGE_VERSION so a future preimage bump is visible in the data.
        await seed_everything(session)
        events = (
            (
                await session.execute(
                    select(ItemEvent).where(
                        ItemEvent.event_type == ItemEventType.REGISTERED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 7
        for event in events:
            assert event.payload["seed_key"]
            assert event.payload["seed_hash_version"] == SEED_HASH_VERSION
            assert f"item-preimage-v{PREIMAGE_VERSION}" == SEED_HASH_VERSION
            assert event.payload_hash.startswith("0x")


class TestSeededTags:
    """The demo dataset arrives tagged, through the same path the API uses."""

    async def test_every_item_marked_for_a_tag_gets_one(self, session: AsyncSession) -> None:
        import json

        from app.core.ids import validate_tag_code

        await seed_everything(session)
        expected = {
            spec["key"]
            for spec in json.loads((SEEDS_DIR / "items.json").read_text(encoding="utf-8"))["items"]
            if spec.get("issue_tag")
        }
        assert expected, "the seed file no longer asks for any tags"

        keys = {
            event.item_id: str(event.payload["seed_key"])
            for event in (
                await session.execute(
                    select(ItemEvent).where(ItemEvent.event_type == ItemEventType.REGISTERED)
                )
            )
            .scalars()
            .all()
        }
        tagged = {
            keys[item.id]: item.tag_code
            for item in (await session.execute(select(Item))).scalars().all()
            if item.tag_code is not None
        }

        assert set(tagged) == expected
        for code in tagged.values():
            assert code is not None and validate_tag_code(code)

    async def test_a_seeded_tag_carries_its_issuance_event(
        self, session: AsyncSession
    ) -> None:
        # Setting the column alone would produce a tag with no issuance history
        # -- a state the running application can never reach. The loader goes
        # through app.qr.service for exactly this reason.
        await seed_everything(session)
        tagged = [
            item
            for item in (await session.execute(select(Item))).scalars().all()
            if item.tag_code is not None
        ]
        events = (
            (
                await session.execute(
                    select(ItemEvent).where(ItemEvent.event_type == ItemEventType.TAG_ISSUED)
                )
            )
            .scalars()
            .all()
        )

        assert len(events) == len(tagged)
        by_item = {event.item_id: event for event in events}
        for item in tagged:
            payload = by_item[item.id].payload
            assert payload["tag_code"] == item.tag_code
            assert payload["payload_url"].endswith(f"/v/{item.tag_code}")

    async def test_seeded_tags_are_unique(self, session: AsyncSession) -> None:
        await seed_everything(session)
        codes = [
            item.tag_code
            for item in (await session.execute(select(Item))).scalars().all()
            if item.tag_code is not None
        ]
        assert len(set(codes)) == len(codes)
