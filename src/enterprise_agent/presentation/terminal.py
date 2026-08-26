"""Render safe operator-facing summaries without application or provider dependencies."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text


class TerminalState(StrEnum):
    """Stable outcome labels supplied by commands before rendering."""

    SUCCEEDED = "succeeded"
    PENDING_APPROVAL = "pending_approval"
    MANUAL_REVIEW = "manual_review"
    IN_PROGRESS = "in_progress"
    RECOVERY_REQUIRED = "recovery_required"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """Return an accessible textual label that never depends on terminal color."""
        return self.replace("_", " ").capitalize()


class EvidenceDisposition(StrEnum):
    """A command-selected evidence outcome fit for concise rendering."""

    INCLUDED = "included"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class TerminalTheme:
    """Visual choices for one injected console; it makes no business decisions."""

    title_style: str = "bold cyan"
    subtitle_style: str = "dim"
    label_style: str = "bold"
    table_header_style: str = "bold cyan"
    muted_style: str = "dim"
    success_style: str = "green"
    attention_style: str = "yellow"
    failure_style: str = "red"

    def status_style(self, state: TerminalState) -> str:
        """Map an already-selected semantic state to a nonessential visual style."""
        if state is TerminalState.SUCCEEDED:
            return self.success_style
        if state in {
            TerminalState.PENDING_APPROVAL,
            TerminalState.MANUAL_REVIEW,
            TerminalState.IN_PROGRESS,
        }:
            return self.attention_style
        return self.failure_style


@dataclass(frozen=True)
class StatusSummary:
    """Safe, command-owned outcome text for an operator."""

    state: TerminalState
    summary: str
    next_action: str | None = None


@dataclass(frozen=True)
class ConfirmationSummary:
    """Safe, command-owned facts an operator must see before a consequential write."""

    action: str
    target: str
    effect: str
    freshness: str
    write_consequence: str
    confirmation_word: str


@dataclass(frozen=True)
class ApprovalSummary:
    """Copyable approval facts selected by a command or application read model."""

    approval_id: str
    plan_id: str
    requester: str
    approver: str
    decision_state: str
    expires_at: str


@dataclass(frozen=True)
class WorkflowSummary:
    """Copyable workflow facts selected by a command or application read model."""

    workflow_id: str
    status: str
    current_step: str
    idempotency_key_prefix: str
    recovery_state: str


@dataclass(frozen=True)
class EvidenceSummary:
    """A concise evidence reference, never a source body or provider payload."""

    evidence_id: str
    source: str
    summary: str
    disposition: EvidenceDisposition


@dataclass(frozen=True)
class AuditTimelineEntry:
    """A read-only, human-safe audit event summary."""

    occurred_at: datetime
    event: str
    summary: str


@dataclass(frozen=True)
class BoundedProgress:
    """A finite local activity update suitable for a terminal progress bar."""

    label: str
    completed: int
    total: int

    def __post_init__(self) -> None:
        """Reject misleading progress values before Rich renders them."""
        if self.total <= 0:
            raise ValueError("total must be positive")
        if self.completed < 0:
            raise ValueError("completed cannot be negative")
        if self.completed > self.total:
            raise ValueError("completed cannot exceed total")


@dataclass(frozen=True)
class TerminalPresenter:
    """Render typed, sanitized terminal summaries through an injected Rich console."""

    console: Console
    theme: TerminalTheme

    def render_header(self, *, title: str, subtitle: str | None = None) -> None:
        """Render a concise command or demo heading."""
        content = Text(title, style=self.theme.title_style)
        if subtitle:
            content.append("\n")
            content.append(subtitle, style=self.theme.subtitle_style)
        self.console.print(Panel.fit(content, border_style=self.theme.title_style))

    def render_status(self, summary: StatusSummary) -> None:
        """Render a semantic outcome with an optional next safe action."""
        details = self._detail_grid()
        details.add_row(
            "Status", Text(summary.state.label, style=self.theme.status_style(summary.state))
        )
        details.add_row("Summary", summary.summary)
        if summary.next_action is not None:
            details.add_row("Next", summary.next_action)
        self.console.print(details)

    def render_confirmation(self, summary: ConfirmationSummary) -> None:
        """Render the command-owned decision receipt before a human confirms a write."""
        details = self._detail_grid()
        details.add_row("Action", summary.action)
        details.add_row("Target", summary.target)
        details.add_row("Effect", summary.effect)
        details.add_row("Freshness", summary.freshness)
        details.add_row("Writes", summary.write_consequence)
        details.add_row(
            "Confirm",
            f"Type {summary.confirmation_word} to continue or cancel to stop.",
        )
        self.console.print(
            Panel(details, title="Confirm action", border_style=self.theme.attention_style)
        )

    def render_approvals(self, approvals: tuple[ApprovalSummary, ...]) -> None:
        """Render approval facts, preserving every durable identifier in full."""
        if self._is_narrow():
            self._render_narrow_records(
                "Approvals",
                tuple(
                    (
                        ("Approval ID", approval.approval_id),
                        ("Plan ID", approval.plan_id),
                        ("Requester", approval.requester),
                        ("Approver", approval.approver),
                        ("State", approval.decision_state),
                        ("Expires", approval.expires_at),
                    )
                    for approval in approvals
                ),
            )
            return
        table = self._table(
            "Approvals",
            "Approval ID",
            "Plan ID",
            "Requester",
            "Approver",
            "State",
            "Expires",
            copyable_columns=(0, 1),
        )
        for approval in approvals:
            table.add_row(
                approval.approval_id,
                approval.plan_id,
                approval.requester,
                approval.approver,
                approval.decision_state,
                approval.expires_at,
            )
        self.console.print(table)

    def render_workflows(self, workflows: tuple[WorkflowSummary, ...]) -> None:
        """Render workflow state without loading a workflow service or database."""
        if self._is_narrow():
            self._render_narrow_records(
                "Workflows",
                tuple(
                    (
                        ("Workflow ID", workflow.workflow_id),
                        ("Status", workflow.status),
                        ("Current step", workflow.current_step),
                        ("Idempotency key", workflow.idempotency_key_prefix),
                        ("Recovery", workflow.recovery_state),
                    )
                    for workflow in workflows
                ),
            )
            return
        table = self._table(
            "Workflows",
            "Workflow ID",
            "Status",
            "Current step",
            "Idempotency key",
            "Recovery",
            copyable_columns=(0,),
        )
        for workflow in workflows:
            table.add_row(
                workflow.workflow_id,
                workflow.status,
                workflow.current_step,
                workflow.idempotency_key_prefix,
                workflow.recovery_state,
            )
        self.console.print(table)

    def render_evidence(self, evidence: tuple[EvidenceSummary, ...]) -> None:
        """Render concise included and excluded evidence decisions."""
        if self._is_narrow():
            self._render_narrow_records(
                "Evidence",
                tuple(
                    (
                        ("Evidence ID", item.evidence_id),
                        ("Source", item.source),
                        ("Disposition", item.disposition.value),
                        ("Summary", item.summary),
                    )
                    for item in evidence
                ),
            )
            return
        table = self._table(
            "Evidence",
            "Evidence ID",
            "Source",
            "Disposition",
            "Summary",
            copyable_columns=(0,),
        )
        for item in evidence:
            table.add_row(item.evidence_id, item.source, item.disposition.value, item.summary)
        self.console.print(table)

    def render_audit_timeline(self, events: tuple[AuditTimelineEntry, ...]) -> None:
        """Render chronological audit summaries without original event payloads."""
        if self._is_narrow():
            self._render_narrow_records(
                "Audit timeline",
                tuple(
                    (
                        ("Time", event.occurred_at.isoformat()),
                        ("Event", event.event),
                        ("Summary", event.summary),
                    )
                    for event in events
                ),
            )
            return
        table = self._table("Audit timeline", "Time", "Event", "Summary")
        for event in events:
            table.add_row(event.occurred_at.isoformat(), event.event, event.summary)
        self.console.print(table)

    def render_progress(self, progress: BoundedProgress) -> None:
        """Render one finite progress update without a spinner or cursor control."""
        indicator = Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            console=self.console,
        )
        indicator.add_task(progress.label, total=progress.total, completed=progress.completed)
        self.console.print(indicator)

    def _detail_grid(self) -> Table:
        """Create a compact, accessible label-value layout."""
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style=self.theme.label_style, no_wrap=True)
        grid.add_column()
        return grid

    def _is_narrow(self) -> bool:
        """Keep durable values intact when a conventional table would compress them."""
        return self.console.width < 80

    def _render_narrow_records(
        self,
        title: str,
        records: tuple[tuple[tuple[str, str], ...], ...],
    ) -> None:
        """Print label-value records without Rich folding durable values at a narrow width."""
        self.console.print(Text(title, style=self.theme.table_header_style))
        for record in records:
            for label, value in record:
                self.console.print(
                    Text.assemble((f"{label}: ", self.theme.label_style), value),
                    soft_wrap=True,
                )
            self.console.print()

    def _table(
        self,
        title: str,
        *columns: str,
        copyable_columns: tuple[int, ...] = (),
    ) -> Table:
        """Create a shared narrow-terminal-friendly data table."""
        table = Table(title=title, box=box.SIMPLE_HEAVY, header_style=self.theme.table_header_style)
        for index, column in enumerate(columns):
            is_copyable = index in copyable_columns
            table.add_column(
                column, no_wrap=is_copyable, overflow="ignore" if is_copyable else "fold"
            )
        return table
