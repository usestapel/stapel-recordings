"""Object policy seam: who may do what to a recording (audit REC-03).

The question "may this user retry / delete / reprocess this recording" was
answered inside each view body by the queryset it happened to build. That is
fine while there is one rule, and it is exactly how a consumer ends up
granting every workspace member the destructive verbs: the read scope gets
widened (members should *see* the workspace's recordings), the mutating
views reuse the same lookup, and edit authority is widened with it —
invisibly, because nothing in the code ever says "this is the authority
check".

So the check gets a name and a seam. One class answers all five verbs, the
default is the module's existing behaviour (owner-only, fail-closed), and a
host that wants a different rule — workspace members read, owners mutate;
role-based; capability tokens — replaces one dotted path instead of editing
view bodies.

    STAPEL_RECORDINGS = {"RECORDING_POLICY": "myapp.policies.MemberReadPolicy"}

Read scope is expressed as a queryset filter (``visible_queryset``) so that
listing and per-object checks cannot drift apart.
"""
from __future__ import annotations

from .conf import recordings_settings
from .models import Recording


class RecordingPolicy:
    """Base policy. Every verb denies unless a subclass says otherwise —
    a policy that forgets a verb refuses it rather than granting it."""

    def visible_queryset(self, user, qs=None):
        """Recordings *user* may read. Defaults to nothing."""
        return (qs if qs is not None else Recording.objects.all()).none()

    def can_read(self, user, recording) -> bool:
        return False

    def can_edit(self, user, recording) -> bool:
        return False

    def can_delete(self, user, recording) -> bool:
        return False

    def can_upload(self, user, recording) -> bool:
        return False

    def can_reprocess(self, user, recording) -> bool:
        return False

    def can_resummarize(self, user, recording) -> bool:
        """Regenerate the summary alone (no re-transcription).

        Delegates to :meth:`can_reprocess` by default, and that default is
        the point: a host that narrowed "may re-run derived work" once
        already answered this question, and a NEW verb that silently
        defaulted to deny (or to allow) would either break that host or widen
        it. Override this method alone for the split hosts actually ask for —
        users may pay to re-summarize, only staff may re-run the pipeline.
        """
        return self.can_reprocess(user, recording)


class OwnerOnlyPolicy(RecordingPolicy):
    """The default: the owner does everything, nobody else does anything.

    Matches what this module has always enforced through its owner-scoped
    querysets; making it explicit is what lets a host widen *reading*
    without silently widening the destructive verbs too."""

    def visible_queryset(self, user, qs=None):
        base = (qs if qs is not None else Recording.objects.all()).filter(
            deleted_at__isnull=True
        )
        if user is None or not getattr(user, "is_authenticated", False):
            return base.none()
        return base.filter(owner=user)

    def _is_owner(self, user, recording) -> bool:
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        return recording is not None and recording.owner_id == user.pk

    def can_read(self, user, recording) -> bool:
        return self._is_owner(user, recording)

    can_edit = can_read
    can_delete = can_read
    can_upload = can_read
    can_reprocess = can_read


def get_policy() -> RecordingPolicy:
    """Resolve the configured policy (``RECORDING_POLICY``)."""
    cls = recordings_settings.RECORDING_POLICY  # import_strings resolves it
    return cls() if isinstance(cls, type) else cls


__all__ = ["RecordingPolicy", "OwnerOnlyPolicy", "get_policy"]
