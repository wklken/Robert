"""In-memory TAPD reads for scan and admission scenarios."""

from robert_agent.adapters.tapd.client import TapdComment, TapdError, TapdItem

STORY, BUG = "1100", "2200"


def comment(comment_id, body, created="2026-11-20 10:00:00", *, author="alice", entry=STORY, kind="story", **extra):
    return TapdComment(comment_id, kind, entry, author, body, created, extra.get("modified", created))


class FakeClient:
    def __init__(self, label="repo-a"):
        self.labels = {STORY: label, BUG: label}
        self.status = {STORY: "status_17", BUG: "new"}
        self.comments = []
        self.fail_items = set()
        self.author_calls = []

    def author_comments(self, workspace_id, author, *, since):
        self.author_calls.append(since)
        rows = [row for row in self.comments if row.author == author and row.created >= since]
        return tuple(sorted(rows, key=lambda row: (row.created, int(row.comment_id)), reverse=True))

    def ticket_comments(self, workspace_id, kind, external_id):
        return tuple(row for row in self.comments if row.entry_id == external_id)

    def get_item(self, workspace_id, kind, external_id):
        if external_id in self.fail_items:
            raise TapdError("command_failed")
        return TapdItem(
            kind, external_id, "Ticket", self.status[external_id], self.labels[external_id], "@robert-wklken in body",
            "1120001234000000001" if kind == "story" else None, False, None,
        )  # fmt: skip

    def terminal_states(self, workspace_id, kind, workitem_type_id=None):
        return {"status_3": "done"} if kind == "story" else {"closed": "done"}
