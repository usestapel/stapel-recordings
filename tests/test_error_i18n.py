"""Localized error catalogs (``translations/errors.<lang>.json``) + provenance gate.

i18n-shipping.md §5. This module owns 15 ``error.*.recording_*`` /
``error.*.share_*`` keys — 0.14.0 added the whole sharing vocabulary — and
shipped no catalog for any of them. Since stapel-core 0.23.1 a reader resolves
a key it does not own from the **owner's** catalog
(:func:`stapel_core.i18n.catalogs.module_catalog`), and since 0.22.0 a writer
may only translate the keys it owns — so "stapel-recordings ships no catalog"
did not mean "a consumer can fix it in its own tree", it meant every consumer
fell back to the English literal, and the one that filled the gap locally was
maintaining a shadow of somebody else's canon. The keys are this module's; so
are their translations.

Provenance of the localized values (honest, per §5):

* the curated ``stapel-translate`` builtin corpus carries none of these keys —
  they are this module's own vocabulary, not the fleet's cross-cutting HTTP
  errors — so the seed pass fills nothing and every value here is a **machine
  translation** recorded per language in :data:`_MACHINE` and written with
  ``origin: llm`` (unreviewed — the gate's W-counter). In a live deployment
  ``translate_catalogs --domain errors --lang <lang> --llm`` produces these
  through the ``STAPEL_I18N["TRANSLATOR"]`` comm seam; offline they come from
  that map so the module regenerates deterministically without a live LLM;
* the seed pass stays wired anyway: the day one of these keys is promoted into
  the corpus, regenerating picks the curated string up as
  ``origin: seed:stapel-builtin`` with no change here.

Languages match what every other stapel library with error keys promises
(stapel-auth, -billing, -gdpr, -notifications, -profiles, -workspaces): en is
the canon in ``errors.py``, ru and es ship as catalogs. Adding a language is a
three-line change: append the tag to :data:`LANGUAGES`, add its
``_MACHINE_<TAG>`` table, and regenerate.

Regenerate after adding/changing an error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``translations/errors.<lang>.json`` + ``translations/.state.json``.
Without the env var the same module is the CI gate.
"""
import os
from pathlib import Path

from stapel_core.i18n import (
    check_translation_catalogs,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
#: Languages this module ships error catalogs in. en is the canon (the
#: registry literals); every other tag needs a catalog.
LANGUAGES = ["en", "ru", "es"]
#: The languages that need a catalog — everything but the source language.
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

#: stapel-translate builtin fixtures (the curated seed corpus). Overridable for
#: an out-of-tree checkout via STAPEL_TRANSLATE_FIXTURES.
_FIXTURES = Path(
    os.environ.get(
        "STAPEL_TRANSLATE_FIXTURES",
        REPO.parent / "stapel-translate" / "fixtures" / "builtin",
    )
)

#: Machine translations (origin: llm) of this module's own error keys. All
#: param-free — edit here + regen when the en text changes.
_MACHINE_RU = {
    # Recordings.
    "error.404.recording_not_found": "Запись не найдена",
    "error.400.recording_invalid_state":
        "Запись находится в состоянии, в котором это действие невозможно",
    "error.409.recording_invalid_state":
        "Запись находится в состоянии, в котором это действие невозможно",
    "error.403.recording_workspace_forbidden":
        "Вы не являетесь участником этого рабочего пространства",
    "error.413.recording_too_large":
        "Загрузка превышает максимально допустимый размер",
    "error.415.recording_unsupported_media":
        "Тип загружаемого файла не поддерживается",
    "error.503.recording_upload_unverifiable":
        "Не удалось проверить загрузку",
    # Summarize-only re-run (0.17.0).
    "error.409.recording_no_transcript":
        "У этой записи пока нет расшифровки для краткого пересказа",
    "error.503.recording_summarize_unavailable":
        "Краткий пересказ недоступен",
    # Media delivery (STORE-01, 0.14.0).
    "error.409.recording_media_not_stored": "У этой записи нет медиафайла",
    "error.503.recording_media_unavailable": "Выдача медиа недоступна",
    # Public share links (SHARE-01, 0.14.0).
    "error.404.share_not_found": "Ссылка для доступа не найдена",
    "error.401.share_passcode_required":
        "Для этой ссылки требуется код доступа",
    "error.403.share_permission_denied":
        "Эта ссылка не даёт такого права",
    "error.429.share_unlock_throttled":
        "Слишком много попыток — повторите позже",
}

_MACHINE_ES = {
    # Recordings.
    "error.404.recording_not_found": "Grabación no encontrada",
    "error.400.recording_invalid_state":
        "La grabación no está en un estado válido para esta acción",
    "error.409.recording_invalid_state":
        "La grabación no está en un estado válido para esta acción",
    "error.403.recording_workspace_forbidden":
        "No eres miembro de este espacio de trabajo",
    "error.413.recording_too_large":
        "La subida supera el tamaño máximo permitido",
    "error.415.recording_unsupported_media":
        "El tipo de archivo subido no es compatible",
    "error.503.recording_upload_unverifiable":
        "No se ha podido verificar la subida",
    # Summarize-only re-run (0.17.0).
    "error.409.recording_no_transcript":
        "Esta grabación todavía no tiene transcripción que resumir",
    "error.503.recording_summarize_unavailable":
        "Los resúmenes no están disponibles",
    # Media delivery (STORE-01, 0.14.0).
    "error.409.recording_media_not_stored":
        "Esta grabación no tiene archivo multimedia",
    "error.503.recording_media_unavailable":
        "La entrega de archivos multimedia no está disponible",
    # Public share links (SHARE-01, 0.14.0).
    "error.404.share_not_found": "Enlace de acceso no encontrado",
    "error.401.share_passcode_required":
        "Este enlace de acceso requiere un código",
    "error.403.share_permission_denied":
        "Este enlace de acceso no concede ese permiso",
    "error.429.share_unlock_throttled":
        "Demasiados intentos: inténtalo de nuevo más tarde",
}

#: language -> machine-translation table, consulted for the keys the curated
#: corpus does not carry (today: all of them). Values land as ``origin: llm``.
_MACHINE = {"ru": _MACHINE_RU, "es": _MACHINE_ES}


class _DictTranslator:
    """Offline translator seam — returns fixed machine translations by key."""

    def __init__(self, table):
        self._table = table

    def translate(self, entries, source_language, target_language):
        return {k: self._table[k] for k in entries if k in self._table}


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    """Flat ``{error.*: text}`` seed from the builtin fixtures for *lang*."""
    import json

    path = _FIXTURES / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def _regen(lang: str):
    """Materialize one target-language catalog from corpus + machine map."""
    return translate_catalog(
        "errors", lang, TRANSLATIONS,
        source_texts=source_texts("errors"),
        seed=_seed_from_fixtures(lang),
        seed_label="stapel-builtin",
        llm=True,
        translator=_DictTranslator(_MACHINE.get(lang, {})),
    )


def test_regen():
    """Regenerate (env-gated) or assert every catalog is a no-op regen (drift)."""
    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        for lang in TARGET_LANGUAGES:
            result = _regen(lang)
            assert not result.missing, f"{lang}: still missing: {result.missing}"
        return

    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        before = path.read_bytes()
        _regen(lang)
        assert path.read_bytes() == before, (
            f"errors.{lang}.json drifted — run "
            f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
        )


def test_catalog_gate_green():
    """E: missing / stale / params-mismatch / not-byte-stable — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
    )
    errors, _warnings = summarize(issues)
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert errors == 0


def test_every_language_covers_every_key_this_module_owns():
    """Coverage is scoped to OWNERSHIP: every recordings key, every language."""
    from stapel_core.i18n import owned_keys, owner_of_dir, source_owners

    source = owned_keys(
        source_texts("errors"),
        source_owners("errors"),
        owner_of_dir(TRANSLATIONS),
    )
    assert source, "ownership resolved to nothing — is stapel_recordings installed?"
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = [k for k in source if k not in catalog]
        assert not missing, (
            f"{lang} catalog missing {len(missing)} key(s): {missing[:8]}"
        )


def test_this_module_owns_only_its_own_keys():
    """The catalogs carry recordings keys and nothing else.

    The mirror image of the gap these catalogs close: a module that
    translates a key it does not own ships a second, drifting copy of
    somebody else's canon.
    """
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        stray = [
            k for k in catalog
            if ".recording_" not in k and ".share_" not in k
        ]
        assert not stray, f"{lang}: not this module's keys: {stray}"


def test_translations_preserve_placeholders():
    """Every localized text keeps exactly the canon's ``{param}`` slots (§3)."""
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        for key, text in catalog.items():
            if key in source:
                assert set(params_of(text)) == set(params_of(source[key])), \
                    f"{lang}: {key}"
