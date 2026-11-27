"""Structured questions an interactive agent can ask during a run."""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


def _displayable(text: str, *, multiline: bool = False) -> bool:
    for character in text:
        if multiline and character == "\n":
            continue
        code = ord(character)
        if code < 32 or 127 <= code <= 159:
            return False
        # Bidi/format controls can visually reverse or hide terminal choices.
        if unicodedata.category(character) in {"Cf", "Zl", "Zp"}:
            return False
    return True


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=350)] | None = (
        None
    )

    @model_validator(mode="after")
    def displayable(self) -> QuestionOption:
        if not _displayable(self.label) or not _displayable(self.description or ""):
            raise ValueError("Option labels and descriptions must be single-line terminal text")
        return self


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    header: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)] | None
    ) = Field(default=None, description="Very short label for the question.")
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)
    ]
    options: list[QuestionOption] = Field(
        default_factory=list, max_length=8, description="Distinct choices, in display order."
    )
    multiple: bool = Field(default=False, description="Allow selecting multiple choices.")
    allow_custom: bool = Field(
        default=True, description="Allow an answer other than a listed choice."
    )

    @model_validator(mode="after")
    def validate_choices(self) -> Question:
        if not _displayable(self.question, multiline=True):
            raise ValueError("The question must be displayable terminal text")
        if self.header is not None and not _displayable(self.header):
            raise ValueError("The header must be single-line terminal text")
        labels = {option.label.casefold() for option in self.options}
        if len(labels) != len(self.options):
            raise ValueError("Option labels must be unique")
        if not self.allow_custom and not self.options:
            raise ValueError("Provide choices when custom answers are disabled")
        return self

    def validate_answer(self, answer: str) -> str:
        value = answer.strip()
        if not value or len(value) > 4000 or not _displayable(value, multiline=True):
            raise ValueError("Enter an answer between 1 and 4000 displayable characters")
        if not self.multiple:
            if not self.allow_custom and all(value != option.label for option in self.options):
                raise ValueError("Select one of the listed answers")
            return value
        selected = list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
        if not selected:
            raise ValueError("Select at least one answer")
        if not self.allow_custom:
            labels = {option.label for option in self.options}
            if any(line not in labels for line in selected):
                raise ValueError("Select only the listed answers")
        return "\n".join(selected)


AskUser = Callable[[Question, str | None], Awaitable[str | None]]
