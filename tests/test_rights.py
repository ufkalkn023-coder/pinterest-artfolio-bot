"""Tests for rights evaluation.

Core invariant: UNKNOWN rights → NOT publishable.
Default policy is DENY.
"""

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity
from pinterest_art_bot.domain.rights import (
    AUTO_PUBLISHABLE_STATUSES,
    PUBLISHABLE_STATUSES,
    RightsEvaluation,
    RightsStatus,
)
from pinterest_art_bot.rights.evaluator import RightsEvaluator

# A relaxed policy, to prove the evaluator is driven by configuration and not
# by a hardcoded set. The default policy stays the strict one.
RELAXED_POLICY = frozenset(PUBLISHABLE_STATUSES)


class TestRightsEvaluator:
    """Rights evaluation must default to deny."""

    def setup_method(self):
        self.evaluator = RightsEvaluator()

    def test_public_domain_is_publishable(self, sample_artwork: Artwork):
        result = self.evaluator.evaluate(sample_artwork)
        assert result.is_publishable is True
        assert result.status == RightsStatus.PUBLIC_DOMAIN

    def test_copyrighted_is_not_publishable(self, copyrighted_artwork: Artwork):
        result = self.evaluator.evaluate(copyrighted_artwork)
        assert result.is_publishable is False
        assert result.status in (RightsStatus.COPYRIGHTED, RightsStatus.RESTRICTED)

    def test_unknown_rights_not_publishable(self, unknown_rights_artwork: Artwork):
        """CRITICAL: unknown rights → DENY."""
        result = self.evaluator.evaluate(unknown_rights_artwork)
        assert result.is_publishable is False
        assert result.status == RightsStatus.UNKNOWN

    def test_no_metadata_defaults_deny(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="No Rights Info",
            museum_name="Test Museum",
        )
        result = self.evaluator.evaluate(artwork)
        assert result.is_publishable is False
        assert result.status == RightsStatus.UNKNOWN

    def test_evidence_is_recorded(self, sample_artwork: Artwork):
        result = self.evaluator.evaluate(sample_artwork)
        assert len(result.evidence) > 0
        assert any(e.field_name == "public_domain" for e in result.evidence)

    def test_cc0_license_is_publishable(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="CC0 Artwork",
            museum_name="Test Museum",
            license="https://creativecommons.org/publicdomain/zero/1.0/",
        )
        result = self.evaluator.evaluate(artwork)
        assert result.is_publishable is True
        assert result.status == RightsStatus.CC0

    def test_cc_by_is_blocked_by_default_policy(self):
        """Attribution-licensed work is not auto-published under the default policy."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="CC BY Artwork",
            museum_name="Test Museum",
            license="https://creativecommons.org/licenses/by/4.0/",
            credit_line="Photo by Museum",
        )
        result = self.evaluator.evaluate(artwork)
        assert result.is_publishable is False
        assert result.requires_review is True
        assert result.requires_attribution is True
        assert "Human review required" in (result.reason or "")

    def test_cc_by_publishes_under_a_relaxed_policy(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="CC BY Artwork",
            museum_name="Test Museum",
            license="https://creativecommons.org/licenses/by/4.0/",
            credit_line="Photo by Museum",
        )
        relaxed = RightsEvaluator(auto_publishable=RELAXED_POLICY)
        result = relaxed.evaluate(artwork)
        assert result.is_publishable is True
        assert result.requires_review is False
        assert result.requires_attribution is True
        assert result.attribution_text == "Photo by Museum"

    def test_open_access_is_blocked_by_default_policy(self):
        """An open-access flag without an explicit licence is not enough."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Open Access Only",
            museum_name="Test Museum",
            open_access=True,
        )
        result = self.evaluator.evaluate(artwork)
        assert result.is_publishable is False
        assert result.requires_review is True

    def test_public_domain_flag_wins_over_cc_by_license(self):
        """A public-domain flag must not be downgraded by a licence string."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Dual Signalled",
            museum_name="Test Museum",
            public_domain=True,
            license="https://creativecommons.org/licenses/by/4.0/",
        )
        result = self.evaluator.evaluate(artwork)
        assert result.status == RightsStatus.PUBLIC_DOMAIN
        assert result.is_publishable is True
        assert result.requires_review is False

    def test_restriction_always_wins(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Restricted Despite Flag",
            museum_name="Test Museum",
            public_domain=True,
            rights_text="Copyright held by the artist. All rights reserved.",
        )
        result = self.evaluator.evaluate(artwork)
        assert result.is_publishable is False
        assert result.requires_review is False

    def test_policy_is_configurable_and_inspectable(self):
        relaxed = RightsEvaluator(auto_publishable={RightsStatus.PUBLIC_DOMAIN})
        assert relaxed.auto_publishable_statuses == frozenset({RightsStatus.PUBLIC_DOMAIN})
        assert RightsStatus.CC0 not in relaxed.auto_publishable_statuses

    def test_default_policy_is_public_domain_and_cc0(self):
        assert (
            frozenset({RightsStatus.PUBLIC_DOMAIN, RightsStatus.CC0}) == AUTO_PUBLISHABLE_STATUSES
        )
        assert RightsEvaluator().auto_publishable_statuses == AUTO_PUBLISHABLE_STATUSES

    def test_reason_is_provided(self, sample_artwork: Artwork):
        result = self.evaluator.evaluate(sample_artwork)
        assert result.reason is not None
        assert len(result.reason) > 0

    def test_deny_factory(self):
        result = RightsEvaluation.deny("test:1")
        assert result.is_publishable is False
        assert result.status == RightsStatus.UNKNOWN
        assert len(result.evidence) == 1

    def test_explicit_restricted_rights_text(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Restricted",
            museum_name="Test Museum",
            rights_text="Copyright held by the artist. Permission required for any use.",
        )
        result = self.evaluator.evaluate(artwork)
        assert result.is_publishable is False
