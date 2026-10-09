"""Local outreach tools. Campaigns prepare drafts; they never send messages."""
from typing import Any, Dict, List, Optional

from xuse.outreach import OutreachError, OutreachStore

from . import executor as ex
from .annotations import LOCAL_WRITE, LOCAL_WRITE_IDEMPOTENT, READ_ONLY_LOCAL
from .executor import Ctx, ToolError
from .tools import guard, ok_


def _store(ctx: Ctx) -> OutreachStore:
    store = getattr(ctx, "outreach_store", None)
    if store is None:
        raise ToolError("Outreach store is not configured on this server.")
    return store


def _call(method, *args, **kwargs):
    # Expected validation failures bypass guard's unexpected-exception logger.
    try:
        return method(*args, **kwargs)
    except OutreachError as exc:
        raise ToolError(str(exc)) from None


def _account(ctx: Ctx, account: str) -> str:
    return ex.resolve_account(ctx, account)[0]


def register_outreach_tools(server, ctx: Ctx) -> None:
    """Register account-scoped local records and bounded reviewed drafts."""

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def upsert_lead(account: str, handle: str, display_name: Optional[str] = None,
                          company: Optional[str] = None, notes: Optional[str] = None,
                          tags: Optional[List[str]] = None,
                          status: Optional[str] = None) -> Dict[str, Any]:
        """Add/update one local lead by X handle (unique per account). Omitted
        fields retain their values; notes stay local and never enter messages.
        Status: new, qualified, contacted, replied, closed, opted_out. Opt-out
        suppression is permanent and cannot be removed by upserting."""
        lead = _call(_store(ctx).upsert_lead, _account(ctx, account), handle,
                     display_name=display_name, company=company, notes=notes,
                     tags=tags, status=status)
        return ok_(lead=lead.model_dump(mode="json"))

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def list_leads(account: str, status: Optional[str] = None,
                         tag: Optional[str] = None, limit: int = 50,
                         offset: int = 0) -> Dict[str, Any]:
        """List this account's local leads, optionally filtering by status or
        exact tag. limit 1-100; offset supports pagination. No browser starts."""
        leads = _call(_store(ctx).list_leads, _account(ctx, account),
                      status=status, tag=tag, limit=limit, offset=offset)
        return ok_(count=len(leads), leads=[lead.model_dump(mode="json") for lead in leads])

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_lead(account: str, lead_id: str) -> Dict[str, Any]:
        """Read one local lead belonging to this account, including its notes."""
        lead = _call(_store(ctx).get_lead, _account(ctx, account), lead_id)
        return ok_(lead=lead.model_dump(mode="json"))

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def update_lead_status(account: str, lead_id: str, status: str) -> Dict[str, Any]:
        """Change a lead's local status. Only new/qualified leads can receive
        campaign drafts. opted_out permanently suppresses further outreach."""
        lead = _call(_store(ctx).update_lead_status, _account(ctx, account), lead_id, status)
        return ok_(lead=lead.model_dump(mode="json"))

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def opt_out_lead(account: str, lead_id: str) -> Dict[str, Any]:
        """Permanently suppress outreach to this account's lead handle. This
        blocks prepared campaign deliveries as well as future drafts."""
        lead = _call(_store(ctx).opt_out_lead, _account(ctx, account), lead_id)
        return ok_(lead=lead.model_dump(mode="json"))

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def create_campaign(account: str, name: str, message_template: str) -> Dict[str, Any]:
        """Create a local draft campaign. Personalize using {handle}, {name},
        {display_name}, or {company}; include a recipient name/handle placeholder.
        {name} falls back to @handle. Notes cannot be templated. Nothing sends."""
        campaign = _call(_store(ctx).create_campaign, _account(ctx, account), name, message_template)
        return ok_(campaign=campaign.model_dump(mode="json"))

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_campaign(account: str, campaign_id: str, limit: int = 50,
                           offset: int = 0) -> Dict[str, Any]:
        """Read this account's campaign and a membership page. limit 1-100;
        members_next_offset identifies the next page of local outcomes."""
        campaign = _call(_store(ctx).get_campaign, _account(ctx, account), campaign_id,
                         limit=limit, offset=offset)
        return ok_(campaign=campaign.model_dump(mode="json"))

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def list_campaigns(account: str, status: Optional[str] = None,
                             limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """List this account's local campaigns (without membership details).
        Status: draft, ready, paused, completed. limit 1-100, with offset."""
        campaigns = _call(_store(ctx).list_campaigns, _account(ctx, account),
                          status=status, limit=limit, offset=offset)
        return ok_(count=len(campaigns), campaigns=[item.model_dump(mode="json") for item in campaigns])

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def add_campaign_leads(account: str, campaign_id: str,
                                 lead_ids: List[str]) -> Dict[str, Any]:
        """Add 1-100 local leads from this account to a campaign. Repeated ids
        are ignored. The whole batch fails if any id belongs to another account.
        Adding a lead does not make an opted-out or closed lead eligible."""
        campaign = _call(_store(ctx).add_campaign_leads, _account(ctx, account), campaign_id, lead_ids)
        return ok_(campaign=campaign.model_dump(mode="json"))

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def set_campaign_status(account: str, campaign_id: str, status: str) -> Dict[str, Any]:
        """Set local campaign status: draft, ready, paused, completed. Pausing
        immediately blocks preparation and approval of existing drafts. Resume
        a paused campaign with ready. completed is terminal."""
        campaign = _call(_store(ctx).set_campaign_status, _account(ctx, account), campaign_id, status)
        return ok_(campaign=campaign.model_dump(mode="json"))

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def prepare_campaign_messages(account: str, campaign_id: str,
                                        max_messages: int = 5) -> Dict[str, Any]:
        """Prepare at most 20 personalized private-message drafts for eligible
        new/qualified leads on an active account. Skip opt-outs and prior
        pending/delivered members. Preparation marks the campaign ready; review
        every exact payload and approve each draft individually. No bulk sends,
        browser, scraping, or LLM call occurs, even when draft_mode is off."""
        account_id, raw, _ = ex.resolve_account(ctx, account)
        ex.require_active(raw, account_id)
        drafts = _call(_store(ctx).prepare_campaign_messages, account_id, campaign_id,
                       ctx.draft_store, max_messages=max_messages)
        return ok_(account=account_id, campaign_id=campaign_id, count=len(drafts),
                   drafts=[draft.model_dump(mode="json") for draft in drafts],
                   message="Nothing was sent. Review each draft, then approve_draft(draft_id) individually.")

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_campaign_summary(account: str, campaign_id: str) -> Dict[str, Any]:
        """Read local membership/delivery counts and opt-out suppression count.
        These are recorded outcomes; this tool never checks X or sends work."""
        summary = _call(_store(ctx).get_campaign_summary, _account(ctx, account), campaign_id)
        return ok_(**summary)
