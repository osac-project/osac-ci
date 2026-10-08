"""Human-readable output of a verdict (job summary / comment body)."""

from __future__ import annotations

from osac_ci.model import Verdict


def render_markdown(verdict: Verdict) -> str:
    lines = [
        f"### OSAC CI: {verdict.state.value}",
        "",
        verdict.headline,
        "",
        f"**Next:** {verdict.next_action} (who: {verdict.who_must_act})",
    ]
    if verdict.blockers:
        lines += ["", "**Blockers:**", *[f"- {b}" for b in verdict.blockers]]
    if verdict.notes:
        lines += ["", "**Notes:**", *[f"- {n}" for n in verdict.notes]]
    if verdict.jobs:
        lines += ["", "| Job | Check | Status | Detail |", "|---|---|---|---|"]
        lines += [f"| {j.job_id} | {j.check} | {j.status.value} | {j.detail} |" for j in verdict.jobs]
    return "\n".join(lines) + "\n"
