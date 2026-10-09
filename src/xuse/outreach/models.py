"""Account-scoped local outreach records; no browser or network dependencies."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

LeadStatus = Literal["new", "qualified", "contacted", "replied", "closed", "opted_out"]
CampaignStatus = Literal["draft", "ready", "paused", "completed"]
MembershipStatus = Literal["unprepared", "pending", "delivered", "failed", "rejected"]

LEAD_STATUSES = ("new", "qualified", "contacted", "replied", "closed", "opted_out")
CAMPAIGN_STATUSES = ("draft", "ready", "paused", "completed")
ELIGIBLE_LEAD_STATUSES = ("new", "qualified")
MAX_PREPARED_MESSAGES = 20


class Lead(BaseModel):
    lead_id: str
    account: str
    handle: str
    display_name: str = ""
    company: str = ""
    notes: str = ""
    tags: List[str] = Field(default_factory=list)
    status: LeadStatus = "new"
    opt_out: bool = False
    created_at: str
    updated_at: str


class CampaignMember(BaseModel):
    lead_id: str
    handle: str
    status: MembershipStatus = "unprepared"
    draft_id: Optional[str] = None
    prepared_at: Optional[str] = None
    delivered_at: Optional[str] = None


class Campaign(BaseModel):
    campaign_id: str
    account: str
    name: str
    message_template: str
    status: CampaignStatus = "draft"
    created_at: str
    updated_at: str
    members: List[CampaignMember] = Field(default_factory=list)
    members_total: Optional[int] = None
    members_offset: int = 0
    members_next_offset: Optional[int] = None


class OutreachError(ValueError):
    """Expected local validation error; messages never include private notes."""
