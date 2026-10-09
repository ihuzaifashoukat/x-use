"""Durability, account boundaries and opt-out gates for local outreach."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from xuse.mcp.drafts import DraftStore
from xuse.outreach import OutreachError, OutreachStore


@pytest.fixture
def store(tmp_path):
    return OutreachStore(tmp_path / "outreach.sqlite3")


def campaign_with_lead(store, account="a", handle="Ada", **kwargs):
    lead = store.upsert_lead(account, handle, **kwargs)
    campaign = store.create_campaign(account, "Introduction", "Hello {name}, from {handle}!")
    store.add_campaign_leads(account, campaign.campaign_id, [lead.lead_id])
    return lead, campaign


def test_persistence_unique_normalized_handle_and_patch_semantics(store):
    lead = store.upsert_lead("a", "@Ada", display_name="Ada", notes="Private local research",
                             tags=["python", "python"], status="qualified")
    updated = store.upsert_lead("a", "ADA", company="Engineers")
    assert updated.lead_id == lead.lead_id
    assert updated.notes == "Private local research"
    assert updated.display_name == "Ada"
    assert updated.status == "qualified"
    assert updated.tags == ["python"]
    reloaded = OutreachStore(store.path)
    assert reloaded.get_lead("a", lead.lead_id) == updated
    assert reloaded.list_leads("a", tag="py") == []
    assert len(reloaded.list_leads("a", tag="python")) == 1
    assert reloaded.upsert_lead("b", "ada").lead_id != lead.lead_id


def test_account_boundaries_cover_lead_campaign_membership_and_delivery(store):
    lead, campaign = campaign_with_lead(store)
    other = store.upsert_lead("b", "Other")
    assert store.list_leads("b") == [other]
    assert store.list_campaigns("b") == []
    operations = [
        lambda: store.get_lead("b", lead.lead_id),
        lambda: store.update_lead_status("b", lead.lead_id, "qualified"),
        lambda: store.opt_out_lead("b", lead.lead_id),
        lambda: store.get_campaign("b", campaign.campaign_id),
        lambda: store.add_campaign_leads("b", campaign.campaign_id, [other.lead_id]),
        lambda: store.set_campaign_status("b", campaign.campaign_id, "ready"),
        lambda: store.prepare_campaign_messages("b", campaign.campaign_id, DraftStore()),
        lambda: store.validate_delivery("b", lead.lead_id, campaign.campaign_id),
        lambda: store.record_delivery("b", lead.lead_id, campaign.campaign_id, "delivered"),
        lambda: store.get_campaign_summary("b", campaign.campaign_id),
    ]
    for operation in operations:
        with pytest.raises(OutreachError, match="Unknown"):
            operation()
    with pytest.raises(OutreachError, match="Unknown lead"):
        store.add_campaign_leads("a", campaign.campaign_id, [other.lead_id])


def test_suppression_survives_recreation_and_cannot_be_reset(store):
    lead = store.upsert_lead("a", "@Ada")
    store.opt_out_lead("a", lead.lead_id)
    assert store.is_suppressed("a", "ADA")
    with pytest.raises(OutreachError, match="suppressed"):
        store.validate_recipient("a", "ADA")
    assert store.validate_recipient("b", "@ADA") == "ada"
    assert not store.is_suppressed("b", "ada")
    assert store.upsert_lead("a", "ada", status="new").status == "opted_out"
    with pytest.raises(OutreachError, match="cannot remove"):
        store.update_lead_status("a", lead.lead_id, "qualified")
    # Even recreating a deleted contact cannot remove account/handle suppression.
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM leads WHERE lead_id=?", (lead.lead_id,))
    recreated = OutreachStore(store.path).upsert_lead("a", "Ada", status="qualified")
    assert recreated.lead_id != lead.lead_id
    assert recreated.opt_out and recreated.status == "opted_out"


def test_membership_batch_is_atomic_and_duplicates_are_idempotent(store):
    lead = store.upsert_lead("a", "Ada")
    campaign = store.create_campaign("a", "Atomic", "Hello {name}")
    with pytest.raises(OutreachError, match="Unknown lead"):
        store.add_campaign_leads("a", campaign.campaign_id, [lead.lead_id, "unknown"])
    assert store.get_campaign("a", campaign.campaign_id).members == []
    store.add_campaign_leads("a", campaign.campaign_id, [lead.lead_id, lead.lead_id])
    store.add_campaign_leads("a", campaign.campaign_id, [lead.lead_id])
    assert len(store.get_campaign("a", campaign.campaign_id).members) == 1


def test_idempotent_mutations_preserve_record_timestamps(store):
    lead, campaign = campaign_with_lead(store, display_name="Ada", tags=["python"])
    assert store.upsert_lead("a", "@ADA", display_name="Ada", tags=["python"]) == lead
    assert store.update_lead_status("a", lead.lead_id, "new") == lead
    current = store.get_campaign("a", campaign.campaign_id)
    assert store.add_campaign_leads("a", campaign.campaign_id, [lead.lead_id]) == current
    opted_out = store.opt_out_lead("a", lead.lead_id)
    assert store.opt_out_lead("a", lead.lead_id) == opted_out


@pytest.mark.parametrize("template", ["", " ", "Hello", "Hello {notes}", "Hi {name.x}",
                                      "Hi {name!r}", "Hi {name:10}", "Hi {name", "{company}"])
def test_invalid_templates_are_rejected_without_records(store, template):
    with pytest.raises(OutreachError):
        store.create_campaign("a", "Invalid", template)
    assert store.list_campaigns("a") == []


def test_eligible_bounded_personalized_payload_excludes_notes(store, tmp_path):
    campaign = store.create_campaign("a", "Prospects", "Hello {name} ({handle})")
    lead_ids = [store.upsert_lead("a", f"p{i}", display_name=f"Person {i}",
                                  notes="KEEP PRIVATE").lead_id for i in range(22)]
    store.add_campaign_leads("a", campaign.campaign_id, lead_ids)
    store.opt_out_lead("a", lead_ids[-1])
    drafts = DraftStore(tmp_path / "drafts.jsonl")
    with pytest.raises(OutreachError, match="1 and 20"):
        store.prepare_campaign_messages("a", campaign.campaign_id, drafts, 21)
    prepared = store.prepare_campaign_messages("a", campaign.campaign_id, drafts, 20)
    assert len(prepared) == 20
    assert all(draft.action == "send_message" and draft.status == "pending" for draft in prepared)
    assert all(set(draft.payload) == {"recipient", "text", "campaign_id", "lead_id"}
               for draft in prepared)
    assert all("KEEP PRIVATE" not in draft.model_dump_json() for draft in prepared)
    assert all(draft.payload["text"].startswith("Hello Person ") for draft in prepared)
    assert store.get_campaign("a", campaign.campaign_id).status == "ready"
    assert len(store.prepare_campaign_messages("a", campaign.campaign_id, drafts, 20)) == 1
    assert store.prepare_campaign_messages("a", campaign.campaign_id, drafts, 20) == []
    summary = store.get_campaign_summary("a", campaign.campaign_id)
    assert summary["counts"]["pending"] == 21
    assert summary["suppressed_leads"] == 1


def test_pending_and_delivered_reservations_survive_restart(store, tmp_path):
    lead, campaign = campaign_with_lead(store)
    path = tmp_path / "drafts.jsonl"
    drafts = DraftStore(path)
    original = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    reloaded = OutreachStore(store.path)
    assert reloaded.prepare_campaign_messages("a", campaign.campaign_id, DraftStore(path)) == []
    # Missing JSONL must conservatively retain the pending reservation.
    assert reloaded.prepare_campaign_messages("a", campaign.campaign_id, DraftStore()) == []
    assert reloaded.validate_delivery("a", lead.lead_id, campaign.campaign_id).handle == "ada"
    reloaded.record_delivery("a", lead.lead_id, campaign.campaign_id, "delivered")
    assert reloaded.get_lead("a", lead.lead_id).status == "contacted"
    reloaded.record_delivery("a", lead.lead_id, campaign.campaign_id, "delivered")
    assert reloaded.prepare_campaign_messages("a", campaign.campaign_id, drafts) == []
    with pytest.raises(OutreachError):
        reloaded.validate_delivery("a", lead.lead_id, campaign.campaign_id)
    with pytest.raises(OutreachError, match="cannot be changed"):
        reloaded.record_delivery("a", lead.lead_id, campaign.campaign_id, "failed")
    assert original.payload["recipient"] == "@ada"


def test_pending_lead_cannot_be_prepared_in_another_campaign(store):
    lead, campaign = campaign_with_lead(store)
    drafts = DraftStore()
    store.prepare_campaign_messages("a", campaign.campaign_id, drafts)
    another = store.create_campaign("a", "Another", "Hi {handle}")
    store.add_campaign_leads("a", another.campaign_id, [lead.lead_id])
    assert store.prepare_campaign_messages("a", another.campaign_id, drafts) == []
    assert store.get_campaign_summary("a", another.campaign_id)["eligible_to_prepare"] == 0


def test_opt_out_and_pause_block_already_prepared_delivery(store):
    lead, campaign = campaign_with_lead(store)
    drafts = DraftStore()
    store.prepare_campaign_messages("a", campaign.campaign_id, drafts)
    store.set_campaign_status("a", campaign.campaign_id, "paused")
    with pytest.raises(OutreachError, match="paused"):
        store.prepare_campaign_messages("a", campaign.campaign_id, drafts)
    with pytest.raises(OutreachError, match="ready"):
        store.validate_delivery("a", lead.lead_id, campaign.campaign_id)
    store.set_campaign_status("a", campaign.campaign_id, "ready")
    store.opt_out_lead("a", lead.lead_id)
    with pytest.raises(OutreachError, match="opted out"):
        store.validate_delivery("a", lead.lead_id, campaign.campaign_id)
    # A confirmed write racing with the local opt-out must never reset suppression.
    store.record_delivery("a", lead.lead_id, campaign.campaign_id, "delivered")
    assert store.get_lead("a", lead.lead_id).status == "opted_out"


def test_completed_is_terminal_and_ineligible_status_never_gets_draft(store):
    lead, campaign = campaign_with_lead(store, status="replied")
    assert store.prepare_campaign_messages("a", campaign.campaign_id, DraftStore()) == []
    store.set_campaign_status("a", campaign.campaign_id, "completed")
    with pytest.raises(OutreachError, match="transition"):
        store.set_campaign_status("a", campaign.campaign_id, "ready")
    with pytest.raises(OutreachError, match="completed"):
        store.add_campaign_leads("a", campaign.campaign_id, [lead.lead_id])


def test_rejected_drafts_can_be_reviewed_again(store):
    _, campaign = campaign_with_lead(store)
    drafts = DraftStore()
    first = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    drafts.set_status(first.draft_id, "rejected")
    second = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    assert second.draft_id != first.draft_id
    assert store.prepare_campaign_messages("a", campaign.campaign_id, drafts) == []


def test_failed_uncertain_draft_remains_reserved_until_explicit_outcome(store):
    lead, campaign = campaign_with_lead(store)
    drafts = DraftStore()
    first = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    drafts.set_status(first.draft_id, "failed")
    assert store.prepare_campaign_messages("a", campaign.campaign_id, drafts) == []
    assert store.get_campaign_summary("a", campaign.campaign_id)["counts"]["pending"] == 1
    # Only the executor's explicit, confirmed-not-sent outcome releases this reservation.
    store.record_delivery("a", lead.lead_id, campaign.campaign_id, "failed")
    assert len(store.prepare_campaign_messages("a", campaign.campaign_id, drafts)) == 1


@pytest.mark.parametrize("outcome", ["delivered", "failed", "rejected"])
def test_stale_outcome_cannot_change_replacement_draft_reservation(store, outcome):
    lead, campaign = campaign_with_lead(store)
    drafts = DraftStore()
    first = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    store.record_delivery("a", lead.lead_id, campaign.campaign_id, "failed",
                          expected_draft_id=first.draft_id)
    # Reconciliation is idempotent until a different draft owns the membership.
    store.record_delivery("a", lead.lead_id, campaign.campaign_id, "failed",
                          expected_draft_id=first.draft_id)
    drafts.set_status(first.draft_id, "rejected")
    second = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    current_campaign = store.get_campaign("a", campaign.campaign_id)
    current_lead = store.get_lead("a", lead.lead_id)
    reloaded = OutreachStore(store.path)
    with pytest.raises(OutreachError, match="reservation has changed") as caught:
        reloaded.record_delivery("a", lead.lead_id, campaign.campaign_id, outcome,
                                 expected_draft_id=first.draft_id)
    assert first.draft_id not in str(caught.value) and second.draft_id not in str(caught.value)
    assert reloaded.get_campaign("a", campaign.campaign_id) == current_campaign
    assert reloaded.get_lead("a", lead.lead_id) == current_lead
    assert reloaded.prepare_campaign_messages("a", campaign.campaign_id, drafts) == []
    assert reloaded.validate_delivery("a", lead.lead_id, campaign.campaign_id,
                                      expected_draft_id=second.draft_id) == current_lead


def test_stale_or_unknown_draft_cannot_validate_replacement_for_delivery(store):
    lead, campaign = campaign_with_lead(store)
    drafts = DraftStore()
    first = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    drafts.set_status(first.draft_id, "rejected")
    second = store.prepare_campaign_messages("a", campaign.campaign_id, drafts)[0]
    for old_id in (first.draft_id, "PRIVATE_DRAFT_SENTINEL"):
        with pytest.raises(OutreachError, match="reservation has changed") as caught:
            store.validate_delivery("a", lead.lead_id, campaign.campaign_id,
                                    expected_draft_id=old_id)
        assert old_id not in str(caught.value)
    store.validate_delivery("a", lead.lead_id, campaign.campaign_id,
                            expected_draft_id=second.draft_id)
    store.record_delivery("a", lead.lead_id, campaign.campaign_id, "delivered",
                          expected_draft_id=second.draft_id)
    delivered = store.get_campaign("a", campaign.campaign_id)
    store.record_delivery("a", lead.lead_id, campaign.campaign_id, "delivered",
                          expected_draft_id=second.draft_id)
    assert store.get_campaign("a", campaign.campaign_id) == delivered
    assert store.get_lead("a", lead.lead_id).status == "contacted"


def test_render_failure_is_atomic_and_missing_company_is_explicit(store):
    campaign = store.create_campaign("a", "Company", "Hello {name} at {company}")
    first = store.upsert_lead("a", "Ada", company="ACME")
    second = store.upsert_lead("a", "Bob")
    store.add_campaign_leads("a", campaign.campaign_id, [first.lead_id, second.lead_id])
    drafts = DraftStore()
    with pytest.raises(OutreachError, match="missing a value"):
        store.prepare_campaign_messages("a", campaign.campaign_id, drafts)
    assert len(drafts) == 0
    assert store.get_campaign_summary("a", campaign.campaign_id)["counts"]["pending"] == 0


def test_draft_creation_failure_rolls_back_and_rejects_orphans(store):
    _, campaign = campaign_with_lead(store)
    second = store.upsert_lead("a", "Bob")
    store.add_campaign_leads("a", campaign.campaign_id, [second.lead_id])

    class BrokenDraftStore(DraftStore):
        def create(self, *args, **kwargs):
            if len(self):
                raise RuntimeError("simulated local disk failure")
            return super().create(*args, **kwargs)

    drafts = BrokenDraftStore()
    with pytest.raises(RuntimeError, match="simulated"):
        store.prepare_campaign_messages("a", campaign.campaign_id, drafts)
    assert drafts.list()[0].status == "rejected"
    assert store.get_campaign_summary("a", campaign.campaign_id)["counts"]["pending"] == 0
    assert store.get_campaign("a", campaign.campaign_id).status == "draft"


def test_concurrent_handles_and_draft_preparation_are_serialized(store):
    second_store = OutreachStore(store.path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda s: s.upsert_lead("a", "Ada"), [store, second_store]))
    assert results[0].lead_id == results[1].lead_id
    campaign = store.create_campaign("a", "Concurrent", "Hello {name}")
    store.add_campaign_leads("a", campaign.campaign_id, [results[0].lead_id])
    drafts = DraftStore()
    with ThreadPoolExecutor(max_workers=2) as executor:
        prepared = list(executor.map(
            lambda s: s.prepare_campaign_messages("a", campaign.campaign_id, drafts),
            [store, second_store]))
    assert sum(len(batch) for batch in prepared) == 1
    assert len(drafts) == 1
