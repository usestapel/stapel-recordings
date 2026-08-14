"""Settings that decide trust are not readable from the environment.

``AppSettings._raw`` falls back to ``os.environ.get(key)`` for every key not
listed in ``no_env``. The keys below decide which code runs and what gets
handed out — with names generic enough (``STORAGE``, ``NORMALIZER``,
``RECORDING_POLICY``) to collide in a shared pod or a compose file — so they
are stated in settings, where a reviewer can see them, or not at all.

The second half of this file is the other trap in the same seam:
``AppSettings`` does no coercion, so a value that arrives as a *string* is
truthy for every non-empty spelling, and ``bool("false")`` silently reverses
a security switch.
"""
import pytest
from django.test import override_settings

from stapel_recordings.conf import recordings_settings

pytestmark = pytest.mark.django_db


@pytest.fixture
def env_setting(monkeypatch):
    """Set an environment variable and re-read the settings cache around it."""

    def _set(key, value):
        monkeypatch.setenv(key, value)
        recordings_settings.reload()

    yield _set
    recordings_settings.reload()


# ── the four dotted-path seams: an env var must not swap imported code ────


def test_env_cannot_swap_the_object_policy(env_setting):
    """The one that decides who may read and mutate a recording."""
    from stapel_recordings.policy import OwnerOnlyPolicy, get_policy

    env_setting("RECORDING_POLICY", "stapel_recordings.tests.test_policy.ReadOnlyForEveryonePolicy")
    assert isinstance(get_policy(), OwnerOnlyPolicy)
    assert type(get_policy()) is OwnerOnlyPolicy


def test_env_cannot_swap_the_storage_backend(env_setting):
    """The one that decides where the bytes go and who can sign for them."""
    from stapel_recordings.storage import DjangoStorageBackend, get_storage, reset_storage_cache

    env_setting("STORAGE", "stapel_recordings.tests.fakes.UnsignedFakeStorage")
    reset_storage_cache()
    assert type(get_storage()) is DjangoStorageBackend
    reset_storage_cache()


def test_env_cannot_swap_the_normalizer(env_setting):
    """The subprocess entrance: NORMALIZER is what the convert stage calls."""
    from stapel_recordings.normalize import ffmpeg_normalize

    env_setting("NORMALIZER", "stapel_recordings.normalize.passthrough_normalize")
    assert recordings_settings.NORMALIZER is ffmpeg_normalize


def test_env_cannot_swap_the_pipeline_resolver(env_setting):
    """The one that decides which stages run at all."""
    from stapel_recordings.pipeline import default_pipeline_resolver

    env_setting("PIPELINE_RESOLVER", "stapel_recordings.tests.fakes.only_record_resolver")
    assert recordings_settings.PIPELINE_RESOLVER is default_pipeline_resolver


# ── and the gates named by a value rather than a dotted path ──────────────


def test_env_cannot_disable_the_upload_content_gate(env_setting):
    from stapel_recordings import media_types

    env_setting("UPLOAD_CONTENT_POLICY", media_types.POLICY_OFF)
    assert recordings_settings.UPLOAD_CONTENT_POLICY == media_types.POLICY_REJECT_KNOWN_BAD


def test_env_cannot_vouch_that_the_backend_signs_urls(env_setting):
    """``STORAGE_SIGNS_GET_URLS`` is a host VOUCHING for its backend. From the
    environment it would turn the 503 that protects a permanent URL into a
    permanent URL."""
    from stapel_recordings import media
    from stapel_recordings.tests.fakes import UnsignedFakeStorage

    env_setting("STORAGE_SIGNS_GET_URLS", "1")
    assert media.storage_signs_get_urls(UnsignedFakeStorage()) is False


def test_env_cannot_open_the_create_membership_gate(env_setting):
    from stapel_recordings.conf import flag

    env_setting("REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE", "false")
    assert flag("REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE") is True


def test_env_cannot_widen_the_workspace_listing(env_setting):
    from stapel_recordings.conf import flag

    env_setting("WORKSPACE_LISTING_MEMBERS_SEE_ALL", "true")
    assert flag("WORKSPACE_LISTING_MEMBERS_SEE_ALL") is False


# ── reversed booleans: bool("false") is True ──────────────────────────────


def test_a_string_false_does_not_vouch_for_an_unsigned_backend():
    """The hazard with teeth: a host writing the STRING "false" would, under
    a bare ``bool()``, be read as vouching that its backend signs — and get
    the permanent URL instead of the 503."""
    from stapel_recordings import media
    from stapel_recordings.tests.fakes import UnsignedFakeStorage

    with override_settings(STAPEL_RECORDINGS={"STORAGE_SIGNS_GET_URLS": "false"}):
        recordings_settings.reload()
        assert media.storage_signs_get_urls(UnsignedFakeStorage()) is False
    recordings_settings.reload()


def test_a_string_false_closes_a_boolean_switch():
    from stapel_recordings.conf import flag

    with override_settings(
        STAPEL_RECORDINGS={"REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE": "false"}
    ):
        recordings_settings.reload()
        assert flag("REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE") is False
    recordings_settings.reload()


def test_unreadable_text_falls_back_to_the_closed_default():
    """Garbage is not an instruction to open anything."""
    from stapel_recordings.conf import flag

    with override_settings(
        STAPEL_RECORDINGS={
            "REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE": "maybe",
            "WORKSPACE_LISTING_MEMBERS_SEE_ALL": "maybe",
        }
    ):
        recordings_settings.reload()
        assert flag("REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE") is True
        assert flag("WORKSPACE_LISTING_MEMBERS_SEE_ALL") is False
    recordings_settings.reload()
