"""Rights domain model.

Rights eligibility is a HARD GATE in the publication pipeline.
An artwork must have explicit evidence of reuse permission before
it can proceed to publication.

Key distinctions:
  - Underlying artwork copyright (the painting itself)
  - Digital reproduction rights (the photograph/scan)

Default policy: DENY if unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RightsStatus(StrEnum):
    """Normalized rights classification.

    Ordered roughly from most to least permissive.
    """

    PUBLIC_DOMAIN = "public_domain"
    OPEN_ACCESS = "open_access"
    CC0 = "cc0"
    CC_BY = "cc_by"
    CC_BY_SA = "cc_by_sa"
    ATTRIBUTION_REQUIRED = "attribution_required"
    RESTRICTED = "restricted"
    COPYRIGHTED = "copyrighted"
    UNKNOWN = "unknown"


# ── Which statuses are publishable ──────────────────────────────────

# Statuses that are permissive enough to publish in principle. This is
# informational only: whether a permissive status may be published
# AUTOMATICALLY is a policy decision (see AUTO_PUBLISHABLE_STATUSES).
PUBLISHABLE_STATUSES: frozenset[RightsStatus] = frozenset(
    {
        RightsStatus.PUBLIC_DOMAIN,
        RightsStatus.OPEN_ACCESS,
        RightsStatus.CC0,
        RightsStatus.CC_BY,
        RightsStatus.CC_BY_SA,
    }
)

# The default-deny auto-publish policy.
#
# Only rights that are unambiguous and carry no obligations survive it.
# Attribution-licensed and open-access-only works are NOT auto-published:
# they require an explicit human decision, and this system deliberately offers
# no override path. Operators may widen the set via
# `RIGHTS_AUTO_PUBLISHABLE_STATUSES`; narrowing further is always safe.
AUTO_PUBLISHABLE_STATUSES: frozenset[RightsStatus] = frozenset(
    {
        RightsStatus.PUBLIC_DOMAIN,
        RightsStatus.CC0,
    }
)

# Attribution-required statuses need credit but are still publishable
ATTRIBUTION_REQUIRED_STATUSES: frozenset[RightsStatus] = frozenset(
    {
        RightsStatus.CC_BY,
        RightsStatus.CC_BY_SA,
        RightsStatus.ATTRIBUTION_REQUIRED,
    }
)

# Restrictive statuses always win over any permissive signal.
RESTRICTIVE_STATUSES: frozenset[RightsStatus] = frozenset(
    {
        RightsStatus.RESTRICTED,
        RightsStatus.COPYRIGHTED,
    }
)


class RightsEvidenceSource(StrEnum):
    """Where did we learn about this artwork's rights status?"""

    MUSEUM_API_FLAG = "museum_api_flag"
    MUSEUM_API_LICENSE = "museum_api_license"
    MUSEUM_API_RIGHTS_TEXT = "museum_api_rights_text"
    MANUAL_OVERRIDE = "manual_override"
    POLICY_DEFAULT = "policy_default"


class RightsEvidence(BaseModel):
    """A single piece of evidence about an artwork's rights."""

    source: RightsEvidenceSource
    field_name: str | None = None
    raw_value: str | None = None
    interpretation: RightsStatus
    note: str | None = None


class RightsEvaluation(BaseModel):
    """Complete rights assessment for a single artwork.

    The evaluator examines all available metadata, records evidence,
    and produces a final determination.
    """

    artwork_id: str = Field(
        ...,
        description="Canonical artwork ID, e.g. 'met:436535'.",
    )
    status: RightsStatus = Field(
        default=RightsStatus.UNKNOWN,
        description="Final determined rights status.",
    )
    is_publishable: bool = Field(
        default=False,
        description="Whether this artwork is eligible for publication.",
    )
    requires_attribution: bool = Field(
        default=False,
        description="Whether publication requires credit/attribution.",
    )
    requires_review: bool = Field(
        default=False,
        description=(
            "Whether the artwork is permissive but excluded by the auto-publish "
            "policy and therefore blocked pending a human decision."
        ),
    )
    attribution_text: str | None = Field(
        default=None,
        description="Required attribution text, if any.",
    )
    evidence: list[RightsEvidence] = Field(
        default_factory=list,
        description="All evidence used to reach the determination.",
    )
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = Field(
        default=None,
        description="Human-readable explanation of the determination.",
    )

    @staticmethod
    def deny(artwork_id: str, reason: str = "No evidence of reuse permission.") -> RightsEvaluation:
        """Factory for a default-deny evaluation."""
        return RightsEvaluation(
            artwork_id=artwork_id,
            status=RightsStatus.UNKNOWN,
            is_publishable=False,
            reason=reason,
            evidence=[
                RightsEvidence(
                    source=RightsEvidenceSource.POLICY_DEFAULT,
                    interpretation=RightsStatus.UNKNOWN,
                    note=reason,
                )
            ],
        )
