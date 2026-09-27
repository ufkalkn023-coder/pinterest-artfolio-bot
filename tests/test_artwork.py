"""Tests for artwork domain model and identity."""

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity


class TestArtworkIdentity:
    """Artwork identity must be stable and deterministic."""

    def test_canonical_id_format(self):
        identity = ArtworkIdentity(source="met", source_object_id="436535")
        assert identity.canonical_id == "met:436535"

    def test_canonical_id_different_sources(self):
        met = ArtworkIdentity(source="met", source_object_id="436535")
        artic = ArtworkIdentity(source="artic", source_object_id="436535")
        assert met.canonical_id != artic.canonical_id
        assert met.canonical_id == "met:436535"
        assert artic.canonical_id == "artic:436535"

    def test_same_source_same_id_yields_same_canonical(self):
        a = ArtworkIdentity(source="met", source_object_id="12345")
        b = ArtworkIdentity(source="met", source_object_id="12345")
        assert a.canonical_id == b.canonical_id

    def test_identity_is_not_title_based(self):
        """Identity must NOT rely on title."""
        a = ArtworkIdentity(source="met", source_object_id="100")
        b = ArtworkIdentity(source="met", source_object_id="100")
        # Same identity regardless of what title the artwork has
        assert a.canonical_id == b.canonical_id


class TestArtwork:
    """Canonical artwork model."""

    def test_minimal_artwork(self):
        """An artwork can be created with minimal required fields."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Test",
            museum_name="Test Museum",
        )
        assert artwork.canonical_id == "test:1"
        assert artwork.title == "Test"
        assert artwork.artist_name is None
        assert artwork.public_domain is None

    def test_optional_fields_default_none(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Test",
            museum_name="Test Museum",
        )
        assert artwork.movement_raw is None
        assert artwork.style_raw is None
        assert artwork.period_raw is None
        assert artwork.description is None
        assert artwork.license is None

    def test_image_url_from_primary(self, sample_artwork: Artwork):
        assert sample_artwork.image_url is not None
        assert "436535" in sample_artwork.image_url

    def test_best_image_url_prefers_primary(self, sample_artwork: Artwork):
        assert sample_artwork.best_image_url == sample_artwork.primary_image.url

    def test_best_image_url_falls_back_to_thumbnail(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Test",
            museum_name="Test Museum",
            thumbnail_url="https://example.com/thumb.jpg",
        )
        assert artwork.best_image_url == "https://example.com/thumb.jpg"

    def test_compute_image_sha256(self):
        hash1 = Artwork.compute_image_sha256(b"test image bytes")
        hash2 = Artwork.compute_image_sha256(b"test image bytes")
        hash3 = Artwork.compute_image_sha256(b"different bytes")
        assert hash1 == hash2  # Same bytes = same hash
        assert hash1 != hash3  # Different bytes = different hash
        assert len(hash1) == 64  # SHA-256 hex digest length
        assert Artwork.compute_image_hash(b"test image bytes") == hash1

    def test_metadata_fingerprint_distinct_from_image_hash(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="met", source_object_id="123"),
            title="The Starry Night",
            artist_name="Vincent van Gogh",
            museum_name="MoMA",
        )
        fp1 = artwork.compute_metadata_fingerprint()
        fp2 = artwork.compute_metadata_fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 64
        # Metadata fingerprint is computed from metadata, not image bytes
        image_hash = Artwork.compute_image_sha256(b"image binary data")
        assert fp1 != image_hash

    def test_tags_default_empty(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Test",
            museum_name="Test Museum",
        )
        assert artwork.tags == []

    def test_retrieved_at_is_set(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Test",
            museum_name="Test Museum",
        )
        assert artwork.retrieved_at is not None
