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

A refusal has a reason (0.18.0)
-------------------------------
``can_reprocess`` / ``can_resummarize`` may answer with a
:class:`PolicyDecision` instead of a bare ``bool``. A bool is one bit short
of what the caller needs: every denial rendered as the module's fail-closed
``404 error.404.recording_not_found``, which is right for "you may not see
this recording" and a lie for "you are out of credits" — the host cannot
bill, and the UI cannot offer a top-up because it was told the recording
does not exist.

    from stapel_recordings.policy import OwnerOnlyPolicy, PolicyDecision

    class MeteredPolicy(OwnerOnlyPolicy):
        def can_resummarize(self, user, recording):
            if not self.can_read(user, recording):
                return PolicyDecision.deny()          # 404, as before
            if credits_of(user) < 1:
                return PolicyDecision.deny(
                    "error.402.myapp_out_of_credits", status=402
                )
            return PolicyDecision.allow()

The host's ``error_code`` travels into the StapelError envelope unchanged,
so a key the host registered (``register_service_errors``) renders its own
sentence and the UI can branch on it. A denial that names a status but no
code falls back to this module's generic key for that status
(:data:`stapel_recordings.errors.POLICY_DENIAL_CODES`).

Returning a plain ``bool`` stays supported — it is coerced to the old
semantics (``True`` → allowed, ``False`` → 404 / ``error.404.recording_not_found``)
and that is what the shipped :class:`OwnerOnlyPolicy` still returns. It is
deprecated for the two metered verbs: hosts that want anything other than
404 on a denial should return a :class:`PolicyDecision`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from .conf import recordings_settings
from .models import Recording


@dataclass(frozen=True)
class PolicyDecision:
    """A policy verb's answer, with the reason attached when it is "no".

    ``error_code`` is an i18n error key (``error.<status>.<slug>``) — the
    same vocabulary :mod:`stapel_recordings.errors` uses, so a host key
    registered through ``register_service_errors`` renders its own sentence
    and reaches the client in the standard StapelError envelope. ``status``
    is the HTTP status the refusal deserves (402 for an unpaid balance, 403
    for a rule the user could not satisfy by paying, 404 to keep the
    existence of the recording secret).

    Both are ``None`` on an allow, and may be ``None`` on a deny — a denial
    that names neither is exactly the old ``False``.
    """

    allowed: bool
    error_code: Optional[str] = None
    status: Optional[int] = None

    def __bool__(self) -> bool:
        # Truthiness matches ``allowed`` so that a host which returns a
        # decision from a verb whose call site still expects a bool (the
        # read/edit/delete/upload verbs) is not silently GRANTED authority
        # by the object being truthy. Fail-closed survives the mix.
        return self.allowed

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls(True)

    @classmethod
    def deny(
        cls, error_code: Optional[str] = None, status: Optional[int] = None
    ) -> "PolicyDecision":
        """Refuse. With no arguments this is the module's old refusal: 404,
        ``error.404.recording_not_found``."""
        return cls(False, error_code=error_code, status=status)


#: What a policy verb may return. ``bool`` is the pre-0.18 shape, still
#: accepted and coerced by :func:`as_decision`.
PolicyAnswer = Union[bool, PolicyDecision]


def as_decision(answer: PolicyAnswer) -> PolicyDecision:
    """Coerce a policy verb's answer to a :class:`PolicyDecision`.

    A bare ``bool`` means what it always meant: ``True`` allows, ``False``
    refuses with this module's fail-closed 404. Call this at every seam that
    consumes a metered verb, so a host may return either shape.
    """
    if isinstance(answer, PolicyDecision):
        return answer
    return PolicyDecision.allow() if answer else PolicyDecision.deny()


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

    def can_reprocess(self, user, recording) -> PolicyAnswer:
        """Re-run the whole pipeline. May answer ``bool`` or
        :class:`PolicyDecision` (see the module docstring)."""
        return False

    def can_resummarize(self, user, recording) -> PolicyAnswer:
        """Regenerate the summary alone (no re-transcription).

        Delegates to :meth:`can_reprocess` by default, and that default is
        the point: a host that narrowed "may re-run derived work" once
        already answered this question, and a NEW verb that silently
        defaulted to deny (or to allow) would either break that host or widen
        it. Override this method alone for the split hosts actually ask for —
        users may pay to re-summarize, only staff may re-run the pipeline.

        The delegation carries the *reason* too: a :class:`PolicyDecision`
        returned by ``can_reprocess`` is passed through unchanged, so a host
        that meters one verb has metered both.
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


__all__ = [
    "RecordingPolicy",
    "OwnerOnlyPolicy",
    "PolicyDecision",
    "PolicyAnswer",
    "as_decision",
    "get_policy",
]
