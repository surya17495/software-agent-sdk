"""Stable DTOs for ACP session model metadata.

These live in a standalone module — *not* ``acp_agent`` — so the agent-server
can import them for its public ``ConversationInfo`` schema without importing
``ACPAgent``, which would eagerly register it in the agent
``DiscriminatedUnion`` (see ``openhands/sdk/agent/__init__.py``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ACPModelInfo(BaseModel):
    """One model an ACP server offers for a session.

    A normalized, stable mirror of the ACP protocol's ``ModelInfo``. The
    protocol ``models`` capability is flagged **UNSTABLE**, so we re-map it
    into our own type at the SDK boundary rather than re-serializing the
    vendored ``acp.schema`` type onto the agent-server's public API — clients
    get a stable shape regardless of upstream protocol churn.

    Carries everything a client needs to render a picker and resolve a
    ``current_model_id`` to a display label *itself*; the SDK deliberately
    does no name curation.
    """

    # ``model_id`` collides with pydantic's protected ``model_`` namespace;
    # opt out (the name mirrors the protocol field and the persisted shape).
    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(
        description=(
            "Server-assigned model identifier. May be concrete "
            '(e.g. ``"gpt-5.5"``) or an opaque alias '
            '(e.g. ``"default"``, ``"auto"``). This is the value to pass back '
            "to the server to switch to this model."
        ),
    )
    name: str | None = Field(
        default=None,
        description='Human-readable label, e.g. ``"GPT-5.5"``.',
    )
    description: str | None = Field(
        default=None,
        description="Optional longer description supplied by the server.",
    )

    @classmethod
    def from_protocol(cls, raw: Any, *, id_attr: str = "model_id") -> ACPModelInfo:
        """Build from a raw ACP ``ModelInfo`` (or any duck-typed object).

        Tolerant of partial/malformed entries: non-string fields degrade to
        ``""`` (``model_id``) or ``None`` (``name``/``description``) rather
        than raising, since the source is an UNSTABLE protocol capability that
        older or half-implemented agents may emit incompletely.

        ``id_attr`` names the attribute carrying the model id — ``"model_id"``
        for a ``models``-capability ``ModelInfo``, ``"value"`` for a
        ``configOptions`` select option.
        """
        model_id = getattr(raw, id_attr, None)
        name = getattr(raw, "name", None)
        description = getattr(raw, "description", None)
        return cls(
            model_id=model_id if isinstance(model_id, str) else "",
            name=name if isinstance(name, str) else None,
            description=description if isinstance(description, str) else None,
        )


class ACPConfigOptionChoice(BaseModel):
    """One selectable value of a ``select`` :class:`ACPConfigOption`.

    Normalized mirror of the ACP ``SessionConfigSelectOption``. Groups
    (``SessionConfigSelectGroup``) are flattened into a flat choice list at the
    SDK boundary — the optional ``group`` label is preserved so a client can
    re-group for display without knowing the vendored ``acp.schema`` shape.
    """

    value: str = Field(
        description=(
            "Stable identifier for this choice — the value to pass back to "
            "``session/set_config_option`` to select it."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Human-readable label for this choice.",
    )
    description: str | None = Field(
        default=None,
        description="Optional longer description supplied by the server.",
    )
    group: str | None = Field(
        default=None,
        description=(
            "Label of the group this choice belonged to when the server "
            "returned grouped options; ``None`` for ungrouped choices."
        ),
    )


class ACPConfigOption(BaseModel):
    """One session configuration option an ACP server advertises.

    A normalized, stable mirror of the ACP protocol's ``SessionConfigOption``
    union (``configOptions`` on ``session/new`` / ``load_session``). Re-mapped
    into our own type at the SDK boundary so the agent-server's public
    ``ConversationInfo`` schema is insulated from upstream protocol churn — a
    client gets a stable shape it can render as a dynamic picker (a dropdown
    for ``select``, a toggle for ``boolean``) and resolve the current value
    without any server-side curation.

    ``current_value`` is a ``str`` for ``select`` options (matching a
    ``choices[].value``) and a ``bool`` for ``boolean`` options. To change it,
    pass ``id`` + the new value to ``session/set_config_option`` (exposed as
    the agent-server ``set_acp_config_option`` route).
    """

    id: str = Field(
        description=(
            "Stable identifier for the option — the ``configId`` passed to "
            "``session/set_config_option``."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Human-readable label for the option.",
    )
    type: Literal["select", "boolean"] = Field(
        description="Renderer hint: a ``select`` dropdown or a ``boolean`` toggle.",
    )
    description: str | None = Field(
        default=None,
        description="Optional description the client may show to the user.",
    )
    category: str | None = Field(
        default=None,
        description="Optional semantic category for grouping in the UI (UX only).",
    )
    current_value: str | bool | None = Field(
        default=None,
        description=(
            "Currently selected value: a ``choices[].value`` for ``select`` "
            "options, ``True``/``False`` for ``boolean`` options. ``None`` when "
            "the server didn't report one."
        ),
    )
    choices: list[ACPConfigOptionChoice] = Field(
        default_factory=list,
        description=(
            "Selectable values for a ``select`` option (empty for ``boolean``)."
        ),
    )

    @classmethod
    def from_protocol(cls, raw: Any) -> ACPConfigOption | None:
        """Build from a raw ACP ``SessionConfigOption`` (or duck-typed object).

        Returns ``None`` when the entry lacks a usable ``id`` or carries an
        unrecognized ``type`` — an unrenderable option is dropped rather than
        surfaced as a broken picker. ``getattr`` keeps the helper tolerant of
        partial structures emitted by older/half-implemented agents.

        A ``select`` option's ``options`` may be a flat list of choices or a
        list of groups (``SessionConfigSelectGroup``); both are flattened into
        ``choices`` with the group label carried on each choice.
        """
        opt = getattr(raw, "root", raw)
        opt_id = getattr(opt, "id", None)
        if not isinstance(opt_id, str) or not opt_id:
            return None
        opt_type = getattr(opt, "type", None)
        name = getattr(opt, "name", None)
        description = getattr(opt, "description", None)
        category = getattr(opt, "category", None)
        current = getattr(opt, "current_value", None)

        # ``boolean`` options carry no ``type`` discriminator on the wire (only
        # ``select`` does), so infer boolean from a bool ``current_value``.
        if opt_type == "select":
            choices = cls._flatten_choices(getattr(opt, "options", None))
            current_value = current if isinstance(current, str) else None
            return cls(
                id=opt_id,
                name=name if isinstance(name, str) else None,
                type="select",
                description=description if isinstance(description, str) else None,
                category=category if isinstance(category, str) else None,
                current_value=current_value,
                choices=choices,
            )
        if opt_type == "boolean" or isinstance(current, bool):
            return cls(
                id=opt_id,
                name=name if isinstance(name, str) else None,
                type="boolean",
                description=description if isinstance(description, str) else None,
                category=category if isinstance(category, str) else None,
                current_value=current if isinstance(current, bool) else None,
                choices=[],
            )
        return None

    @staticmethod
    def _flatten_choices(options: Any) -> list[ACPConfigOptionChoice]:
        """Normalize a select option's ``options`` into a flat choice list.

        Accepts either flat ``SessionConfigSelectOption`` entries or
        ``SessionConfigSelectGroup`` entries (each carrying its own nested
        ``options``); groups are flattened with their ``name`` recorded on each
        member choice. Malformed entries (no ``value``) are skipped.
        """
        result: list[ACPConfigOptionChoice] = []
        for entry in options or []:
            nested = getattr(entry, "options", None)
            if nested is not None:
                group_label = getattr(entry, "name", None)
                for choice in nested:
                    built = ACPConfigOption._build_choice(
                        choice,
                        group=group_label if isinstance(group_label, str) else None,
                    )
                    if built is not None:
                        result.append(built)
                continue
            built = ACPConfigOption._build_choice(entry, group=None)
            if built is not None:
                result.append(built)
        return result

    @staticmethod
    def _build_choice(raw: Any, *, group: str | None) -> ACPConfigOptionChoice | None:
        value = getattr(raw, "value", None)
        if not isinstance(value, str) or not value:
            return None
        name = getattr(raw, "name", None)
        description = getattr(raw, "description", None)
        return ACPConfigOptionChoice(
            value=value,
            name=name if isinstance(name, str) else None,
            description=description if isinstance(description, str) else None,
            group=group,
        )
