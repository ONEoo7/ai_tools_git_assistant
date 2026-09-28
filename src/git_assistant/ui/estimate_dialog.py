"""The pop-up shown before a run spends anything.

Asked after the button is pressed and before the first request goes out, so the
answer to "how much is this about to send" arrives while it can still be
declined. Tokens only -- what they cost depends on a price list this application
does not have, and a made-up figure would be worse than none.

The numbers are estimates and say so. What matters is the order of magnitude:
one call or forty, five thousand tokens or five hundred thousand.
"""

from __future__ import annotations

import html

from PyQt6.QtWidgets import QCheckBox, QMessageBox, QWidget

from git_assistant.estimate import Estimate
from git_assistant.providers import get as provider_of
from git_assistant.providers import is_known

#: What the buttons say. "Run" rather than "OK": the question is whether to
#: spend this, and the answer should read as an instruction.
RUN = "Run"
CANCEL = "Cancel"

#: The box on a commit message's dialog, where sending only names is on offer.
SEND_NAMES_ONLY = "Send only file names"


def _provider_label(estimate: Estimate) -> str:
    """Its name, or the raw key when this build has never heard of it.

    ``providers.get`` falls back to the default rather than failing, which is
    right everywhere else and wrong here: naming the wrong provider in the one
    dialog that says what is about to be spent is worse than an unfamiliar key.
    """
    if not estimate.provider:
        return "no provider selected"
    if not is_known(estimate.provider):
        return f"{estimate.provider} (unknown to this build)"
    return provider_of(estimate.provider).label


def describe(estimate: Estimate) -> str:
    """The whole message, as plain text (what the dialog shows, and the tests read)."""
    where = f"{_provider_label(estimate)} - {estimate.model or 'no model selected'}"
    parts = [estimate.chosen().summary(), "", where, ""]
    parts += [f"- {line}" for line in estimate.lines]
    names = estimate.names_only
    if names is not None:
        # Both runs, whichever the box says: the figure for the other one is what
        # somebody weighing the box needs to see.
        parts += ["", f"{SEND_NAMES_ONLY}: {names.summary()}"]
        parts += [f"- {line}" for line in names.lines]
    parts += [
        "",
        "These are estimates: the provider counts the tokens itself, and what "
        "it reports is what appears under LLM usage.",
    ]
    return "\n".join(parts)


def _headline(estimate: Estimate) -> str:
    return f"<b>{html.escape(estimate.chosen().summary())}</b>"


def confirm(parent: QWidget | None, estimate: Estimate) -> bool:
    """Show what the run will send. ``True`` if the user wants it to go ahead.

    A run with nothing to do is refused here rather than started and failed:
    the estimate already knows why, and the message is the same one the run
    would have produced.

    Where the estimate offers to send only the names of the changed files, a box
    says so, ticked as the estimate starts it; the headline follows it, so what is
    about to be spent is always the run the box describes. Whatever it is left at
    is ``estimate.only_names`` when this returns.
    """
    if estimate.problem:
        QMessageBox.information(parent, f"{estimate.feature}", estimate.problem)
        return False
    if not estimate.calls:
        # Nothing will be sent (an audit written from the measurements alone).
        return True

    box = QMessageBox(parent)
    box.setWindowTitle(f"{estimate.feature} - about to run")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(_headline(estimate))
    box.setInformativeText(describe(estimate).split("\n", 2)[2].strip())
    names = None
    if estimate.names_only is not None:
        names = QCheckBox(SEND_NAMES_ONLY)
        names.setChecked(estimate.only_names)
        names.setToolTip(
            "Write the message from the names of the changed files, grouped by "
            "what happened to each, and send none of their changes: one call, "
            "however large the change."
        )
        box.setCheckBox(names)

        def follow(ticked: bool) -> None:
            estimate.only_names = ticked
            box.setText(_headline(estimate))

        names.toggled.connect(follow)
    run = box.addButton(RUN, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(CANCEL, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(run)
    box.exec()
    return box.clickedButton() is run
