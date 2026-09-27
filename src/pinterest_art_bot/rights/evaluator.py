"""Rights eligibility evaluator.

This is the HARD GATE in the publication pipeline.
Default policy: DENY if unknown.

The evaluator examines all available metadata from the artwork
record and produces a structured RightsEvaluation with evidence.

It does NOT infer reuse rights solely from artwork age.
It distinguishes underlying artwork copyright from digital reproduction rights.
"""

from __future__ import annotations

import re

import structlog

from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.rights import (
    ATTRIBUTION_REQUIRED_STATUSES,
    AUTO_PUBLISHABLE_STATUSES,
    PUBLISHABLE_STATUSES,
    RESTRICTIVE_STATUSES,
    RightsEvaluation,
    RightsEvidence,
    RightsEvidenceSource,
    RightsStatus,
)

logger = structlog.get_logger()

# Known license URL patterns → status
_LICENSE_PATTERNS: list[tuple[str, RightsStatus]] = [
    (r"creativecommons\.org/publicdomain/zero", RightsStatus.CC0),
    (r"creativecommons\.org/licenses/by-sa/", RightsStatus.CC_BY_SA),
    (r"creativecommons\.org/licenses/by/", RightsStatus.CC_BY),
    (r"publicdomain", RightsStatus.PUBLIC_DOMAIN),
]


class RightsEvaluator:
    """Evaluates whether an artwork is eligible for republication.

    Examines museum-provided metadata flags, license strings,
    and rights text to determine publishability.

    Policy: default-deny. If no positive evidence of reuse permission
    is found, the artwork is marked as UNKNOWN → not publishable.

    Strict mode (default): statuses that require attribution are only
    publishable when a credit line is actually available to attribute.
    """

    def __init__(
        self,
        *,
        enforce_strict: bool = True,
        auto_publishable: frozenset[RightsStatus] | set[RightsStatus] | None = None,
    ) -> None:
        self.enforce_strict = enforce_strict
        self._auto_publishable = (
            frozenset(auto_publishable)
            if auto_publishable is not None
            else AUTO_PUBLISHABLE_STATUSES
        )

    @property
    def auto_publishable_statuses(self) -> frozenset[RightsStatus]:
        """Statuses this evaluator is willing to publish automatically."""
        return self._auto_publishable

    def evaluate(self, artwork: Artwork) -> RightsEvaluation:
        """Produce a complete rights evaluation for an artwork."""
        evidence: list[RightsEvidence] = []
        best_status = RightsStatus.UNKNOWN

        # ── Check explicit public_domain flag ───────────────────────
        if artwork.public_domain is True:
            status = RightsStatus.PUBLIC_DOMAIN
            evidence.append(
                RightsEvidence(
                    source=RightsEvidenceSource.MUSEUM_API_FLAG,
                    field_name="public_domain",
                    raw_value="true",
                    interpretation=status,
                    note="Museum API explicitly marks this as public domain.",
                )
            )
            best_status = _best_of(best_status, status, self._auto_publishable)

        elif artwork.public_domain is False:
            evidence.append(
                RightsEvidence(
                    source=RightsEvidenceSource.MUSEUM_API_FLAG,
                    field_name="public_domain",
                    raw_value="false",
                    interpretation=RightsStatus.RESTRICTED,
                    note="Museum API explicitly marks this as NOT public domain.",
                )
            )
            best_status = _best_of(best_status, RightsStatus.RESTRICTED, self._auto_publishable)

        # ── Check open_access flag ──────────────────────────────────
        if artwork.open_access is True and artwork.public_domain is not True:
            status = RightsStatus.OPEN_ACCESS
            evidence.append(
                RightsEvidence(
                    source=RightsEvidenceSource.MUSEUM_API_FLAG,
                    field_name="open_access",
                    raw_value="true",
                    interpretation=status,
                    note="Museum API marks this as open access.",
                )
            )
            best_status = _best_of(best_status, status, self._auto_publishable)

        # ── Check license string ────────────────────────────────────
        if artwork.license:
            license_status = self._interpret_license(artwork.license)
            evidence.append(
                RightsEvidence(
                    source=RightsEvidenceSource.MUSEUM_API_LICENSE,
                    field_name="license",
                    raw_value=artwork.license,
                    interpretation=license_status,
                )
            )
            best_status = _best_of(best_status, license_status, self._auto_publishable)

        # ── Check rights_text ───────────────────────────────────────
        if artwork.rights_text:
            rights_status = self._interpret_rights_text(artwork.rights_text)
            evidence.append(
                RightsEvidence(
                    source=RightsEvidenceSource.MUSEUM_API_RIGHTS_TEXT,
                    field_name="rights_text",
                    raw_value=artwork.rights_text,
                    interpretation=rights_status,
                )
            )
            best_status = _best_of(best_status, rights_status, self._auto_publishable)

        # ── Determine publishability ────────────────────────────────
        is_publishable = best_status in self._auto_publishable
        requires_attribution = best_status in ATTRIBUTION_REQUIRED_STATUSES
        # Permissive rights, but outside the auto-publish policy.
        requires_review = (not is_publishable) and best_status in PUBLISHABLE_STATUSES

        attribution_text = None
        if requires_attribution and artwork.credit_line:
            attribution_text = artwork.credit_line

        if self.enforce_strict and requires_attribution and not attribution_text:
            is_publishable = False

        reason = self._build_reason(best_status, is_publishable, evidence)
        if self.enforce_strict and requires_attribution and not attribution_text:
            reason = (
                f"Artwork is {best_status.value} but requires attribution and no "
                "credit line is available. Strict rights mode: deny."
            )
        elif requires_review:
            reason = (
                f"Artwork is {best_status.value}, which is permissive but excluded "
                "by the auto-publish rights policy. Human review required."
            )

        logger.info(
            "rights_evaluated",
            artwork_id=artwork.canonical_id,
            status=best_status.value,
            is_publishable=is_publishable,
            evidence_count=len(evidence),
        )

        return RightsEvaluation(
            artwork_id=artwork.canonical_id,
            status=best_status,
            is_publishable=is_publishable,
            requires_attribution=requires_attribution,
            requires_review=requires_review,
            attribution_text=attribution_text,
            evidence=evidence,
            reason=reason,
        )

    def _interpret_license(self, license_str: str) -> RightsStatus:
        """Interpret a license string or URL."""
        normalized = license_str.lower().strip()
        for pattern, status in _LICENSE_PATTERNS:
            if re.search(pattern, normalized):
                return status
        if "copyright" in normalized or "all rights reserved" in normalized:
            return RightsStatus.COPYRIGHTED
        return RightsStatus.UNKNOWN

    def _interpret_rights_text(self, text: str) -> RightsStatus:
        """Interpret a free-text rights/reproduction field."""
        lower = text.lower()
        if "public domain" in lower:
            return RightsStatus.PUBLIC_DOMAIN
        if "cc0" in lower or "creative commons zero" in lower:
            return RightsStatus.CC0
        if "creative commons" in lower and "by-sa" in lower:
            return RightsStatus.CC_BY_SA
        if "creative commons" in lower and "by" in lower:
            return RightsStatus.CC_BY
        if "no known copyright" in lower:
            return RightsStatus.PUBLIC_DOMAIN
        if "copyright" in lower or "restricted" in lower or "permission required" in lower:
            return RightsStatus.RESTRICTED
        return RightsStatus.UNKNOWN

    def _build_reason(
        self,
        status: RightsStatus,
        publishable: bool,
        evidence: list[RightsEvidence],
    ) -> str:
        if not evidence:
            return "No rights metadata available. Default policy: deny."
        if publishable:
            sources = ", ".join(
                e.source.value for e in evidence if e.interpretation in PUBLISHABLE_STATUSES
            )
            return f"Artwork is {status.value} based on: {sources}."
        return f"Artwork is {status.value}. Not eligible for automatic publication."


def _best_of(
    current: RightsStatus,
    candidate: RightsStatus,
    auto_publishable: frozenset[RightsStatus] = AUTO_PUBLISHABLE_STATUSES,
) -> RightsStatus:
    """Pick the most defensible status from two pieces of evidence.

    Precedence:
      1. An explicit restriction always wins.
      2. Between two auto-publishable statuses, the first (strongest) wins.
      3. A status inside the auto-publish policy beats one outside it, so a
         public-domain flag is not overridden by a CC BY license string.
      4. Otherwise prefer any permissive status over a non-permissive one.
    """
    if candidate in RESTRICTIVE_STATUSES:
        return candidate
    if current in RESTRICTIVE_STATUSES:
        return current

    current_allowed = current in auto_publishable
    candidate_allowed = candidate in auto_publishable
    if current_allowed and candidate_allowed:
        return current
    if candidate_allowed:
        return candidate
    if current_allowed:
        return current
    if candidate in PUBLISHABLE_STATUSES:
        return candidate
    return current
