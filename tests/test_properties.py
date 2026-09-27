"""Property-based tests for the system's safety invariants.

These encode the rules that must hold for *every* input, not just the examples
someone thought of. They are the last line of defence for the invariants in
AGENTS.md: ambiguous writes are never retried, terminal states are final,
deduplication never merges distinct works, and rights are never invented.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pinterest_art_bot.classification.aliases import normalize_movement
from pinterest_art_bot.classification.classifier import MovementClassifier
from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity
from pinterest_art_bot.domain.movement import ArtMovement, ConfidenceLevel
from pinterest_art_bot.domain.publication import (
    RETRYABLE_PUBLICATION_STATES,
    TERMINAL_STATES,
    InvalidStateTransitionError,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.domain.rights import AUTO_PUBLISHABLE_STATUSES, RightsStatus
from pinterest_art_bot.domain.work_identity import WorkIdentity, is_same_work
from pinterest_art_bot.media.hashing import compute_perceptual_hash, hamming_distance
from pinterest_art_bot.rights.evaluator import RightsEvaluator

STATE_MACHINE = list(PublicationState)
SAFE = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# Words that must never be read as a movement.
AMBIGUOUS_TAGS = ["Popes", "Pope Pius", "People", "Poppies", "Repop", "Popular"]

# Comparable title token sets (two or more distinct tokens).
TITLE_TOKENS = st.frozensets(
    st.text(alphabet="abcdefgh", min_size=3, max_size=8), min_size=2, max_size=4
)


def legal_targets(state: PublicationState) -> frozenset[PublicationState]:
    """States the documented state machine allows after ``state``.

    Mirrors the domain rules on purpose: the tests below assert that the
    implementation agrees with this model, for every pair of states.
    """
    if state == PublicationState.AMBIGUOUS:
        # INVARIANT 3: reconciliation or proof of publication, nothing else.
        return frozenset({PublicationState.RECONCILED, PublicationState.PUBLISHED})
    if state in TERMINAL_STATES:
        return frozenset({state})
    return frozenset(STATE_MACHINE)


class TestPublicationStateMachine:
    @SAFE
    @given(from_state=st.sampled_from(STATE_MACHINE), to_state=st.sampled_from(STATE_MACHINE))
    def test_transitions_match_the_documented_rules(self, from_state, to_state):
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.state = from_state

        if to_state in legal_targets(from_state):
            record.transition_to(to_state)
            assert record.state == to_state
        else:
            with pytest.raises(InvalidStateTransitionError):
                record.transition_to(to_state)

    @SAFE
    @given(state=st.sampled_from(STATE_MACHINE))
    def test_ambiguous_never_leaves_for_retry(self, state):
        """INVARIANT 3: an ambiguous write is never silently resolved."""
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.transition_to(PublicationState.AMBIGUOUS, reason="timeout")

        if state in (PublicationState.RECONCILED, PublicationState.PUBLISHED):
            record.transition_to(state)
            assert record.state == state
        else:
            with pytest.raises(InvalidStateTransitionError, match="AMBIGUOUS"):
                record.transition_to(state)

    @SAFE
    @given(state=st.sampled_from(STATE_MACHINE))
    def test_retryable_set_never_includes_blocked_states(self, state):
        """Only definitive outcomes may be attempted again."""
        expected = state in (PublicationState.FAILED, PublicationState.RECONCILED)
        assert (state in RETRYABLE_PUBLICATION_STATES) is expected
        # The two blocked states must never be retryable.
        assert PublicationState.AMBIGUOUS not in RETRYABLE_PUBLICATION_STATES
        assert PublicationState.PUBLISHED not in RETRYABLE_PUBLICATION_STATES

    @SAFE
    @given(
        path=st.recursive(
            st.sampled_from(STATE_MACHINE),
            lambda children: st.lists(children, max_size=3).map(lambda rest: [rest]),
            max_leaves=4,
        ).filter(lambda p: len(p) > 0),
    )
    def test_history_matches_every_legal_path(self, path):
        """Walk a legal path and assert the audit trail matches at every step."""
        record = PublicationRecord(artwork_canonical_id="met:1")
        # Flatten the nested structure into a plain sequence of states.
        flat: list[PublicationState] = []

        def walk(node) -> None:
            if isinstance(node, PublicationState):
                flat.append(node)
            else:
                for child in node:
                    walk(child)

        walk(path)

        current = PublicationState.DISCOVERED
        for target in flat:
            assume(target in legal_targets(current))
            record.transition_to(target)
            current = target
            assert record.state == current
            assert record.state_history[-1].to_state == current

    @SAFE
    @given(state=st.sampled_from([s for s in STATE_MACHINE if s not in TERMINAL_STATES]))
    def test_published_sets_a_timestamp(self, state):
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.state = state
        record.transition_to(PublicationState.PUBLISHED)
        assert record.published_at is not None


class TestRightsInvariants:
    @SAFE
    @given(
        public_domain=st.one_of(st.none(), st.booleans()),
        open_access=st.one_of(st.none(), st.booleans()),
        license=st.one_of(st.none(), st.sampled_from(["", "weird", "https://example.org/x"])),
        rights_text=st.one_of(st.none(), st.sampled_from(["", "no idea", "???"])),
    )
    def test_only_policy_statuses_are_publishable(
        self, public_domain, open_access, license, rights_text
    ):
        """INVARIANT 1: publishing requires explicit, policy-allowed evidence."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="T",
            museum_name="M",
            public_domain=public_domain,
            open_access=open_access,
            license=license,
            rights_text=rights_text,
        )
        result = RightsEvaluator().evaluate(artwork)
        if result.is_publishable:
            assert result.status in AUTO_PUBLISHABLE_STATUSES
        if result.requires_review:
            assert not result.is_publishable

    @SAFE
    @given(
        public_domain=st.booleans(),
        rights_text=st.sampled_from(
            [
                "Copyright held by the artist. All rights reserved.",
                "Restricted",
                "Permission required for any use",
            ]
        ),
    )
    def test_restriction_always_wins(self, public_domain, rights_text):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="T",
            museum_name="M",
            public_domain=public_domain,
            rights_text=rights_text,
        )
        result = RightsEvaluator().evaluate(artwork)
        assert result.is_publishable is False
        assert result.requires_review is False

    @SAFE
    @given(status=st.sampled_from(list(RightsStatus)))
    def test_relaxed_policy_can_only_widen(self, status):
        """A relaxed policy never denies what the strict policy allows.

        This is a sanity check on the policy mechanism itself, not on the
        artwork: the same record is evaluated under both policies.
        """
        strict = RightsEvaluator()
        relaxed = RightsEvaluator(auto_publishable=set(RightsStatus))
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="T",
            museum_name="M",
            public_domain=status == RightsStatus.PUBLIC_DOMAIN,
        )
        if strict.evaluate(artwork).is_publishable:
            assert relaxed.evaluate(artwork).is_publishable

    def test_default_policy_is_the_strictest(self):
        assert {RightsStatus.PUBLIC_DOMAIN, RightsStatus.CC0} == AUTO_PUBLISHABLE_STATUSES
        for status in (RightsStatus.CC_BY, RightsStatus.CC_BY_SA, RightsStatus.OPEN_ACCESS):
            assert status not in AUTO_PUBLISHABLE_STATUSES


class TestClassificationInvariants:
    @SAFE
    @given(tag=st.sampled_from(AMBIGUOUS_TAGS))
    def test_ambiguous_tags_never_classify(self, tag):
        """A tag like "Popes" must not become POP_ART."""
        assert normalize_movement(tag) is None

    @SAFE
    @given(
        style=st.one_of(st.none(), st.text(min_size=1, max_size=40)),
        year=st.one_of(st.none(), st.integers(min_value=1000, max_value=2030)),
    )
    def test_date_only_classification_never_reaches_high(self, style, year):
        """INVARIANT 7: a date is never sufficient to auto-route."""
        assume(style is None or normalize_movement(style) is None)
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="T",
            museum_name="M",
            style_raw=style,
            year_start=year,
        )
        result = MovementClassifier().classify(artwork)
        assert result.confidence_level != ConfidenceLevel.HIGH
        assert result.confidence <= 0.35

    @SAFE
    @given(
        style=st.sampled_from(["Impressionism", "Baroque", "Post-Impressionism", "Rococo"]),
        artist=st.sampled_from(["Vincent van Gogh", "Rembrandt van Rijn", "Claude Monet"]),
    )
    def test_every_classification_carries_evidence(self, style, artist):
        """INVARIANT 6: a bare movement string is never acceptable."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="T",
            museum_name="M",
            style_raw=style,
            artist_name=artist,
            year_start=1880,
        )
        result = MovementClassifier().classify(artwork)
        assert result.evidence, "classification must record evidence"
        assert result.reason
        for evidence in result.evidence:
            assert 0.0 <= evidence.weight <= 1.0


class TestDeduplicationInvariants:
    @SAFE
    @given(candidate=TITLE_TOKENS, existing=TITLE_TOKENS)
    def test_identity_matching_is_symmetric(self, candidate, existing):
        left = WorkIdentity(artist_tokens=frozenset({"degas"}), title_tokens=candidate)
        right = WorkIdentity(artist_tokens=frozenset({"degas"}), title_tokens=existing)
        assert is_same_work(left, right) == is_same_work(right, left)

    @SAFE
    @given(candidate=TITLE_TOKENS, existing=TITLE_TOKENS)
    def test_different_artists_never_merge(self, candidate, existing):
        left = WorkIdentity(artist_tokens=frozenset({"degas"}), title_tokens=candidate)
        right = WorkIdentity(artist_tokens=frozenset({"monet"}), title_tokens=existing)
        assert is_same_work(left, right) is False

    @SAFE
    @given(candidate=TITLE_TOKENS)
    def test_incomparable_identities_never_merge(self, candidate):
        left = WorkIdentity(artist_tokens=frozenset(), title_tokens=candidate)
        right = WorkIdentity(artist_tokens=frozenset({"degas"}), title_tokens=candidate)
        assert is_same_work(left, right) is False

    @SAFE
    @given(
        year=st.integers(min_value=1400, max_value=2020),
        delta=st.integers(min_value=6, max_value=200),
    )
    def test_distant_years_never_merge(self, year, delta):
        left = WorkIdentity(
            artist_tokens=frozenset({"degas"}),
            title_tokens=frozenset({"dance", "class"}),
            year=year,
        )
        right = WorkIdentity(
            artist_tokens=frozenset({"degas"}),
            title_tokens=frozenset({"dance", "class"}),
            year=year + delta,
        )
        assert is_same_work(left, right) is False


class TestPerceptualHashInvariants:
    @SAFE
    @given(seed=st.integers(min_value=0, max_value=10_000))
    def test_hash_is_stable_and_comparable(self, seed):
        import io

        from PIL import Image, ImageDraw

        img = Image.new("RGB", (64, 64), (seed % 256, (seed // 7) % 256, (seed // 13) % 256))
        draw = ImageDraw.Draw(img)
        for i in range(0, 64, 8 + seed % 5):
            draw.line([(i, 0), (i + 64, 64)], fill=(255, 255, 255), width=3)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()

        first = compute_perceptual_hash(data)
        second = compute_perceptual_hash(data)
        assert first is not None
        assert first == second
        assert len(first) == 16
        assert hamming_distance(first, second) == 0
        assert hamming_distance(first, first) == 0

    @SAFE
    @given(
        left=st.text(alphabet="0123456789abcdef", min_size=16, max_size=16),
        right=st.text(alphabet="0123456789abcdef", min_size=16, max_size=16),
    )
    def test_hamming_distance_is_bounded_and_symmetric(self, left, right):
        distance = hamming_distance(left, right)
        assert 0 <= distance <= 64
        assert distance == hamming_distance(right, left)


def test_movement_enum_values_are_stable():
    """Board configuration depends on these identifiers never changing."""
    for movement in ArtMovement:
        assert movement.value == movement.value.lower()
        assert movement.value.replace(" ", "_") == movement.value
