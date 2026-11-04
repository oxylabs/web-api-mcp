"""The bundled agent skill, served as a resource and as a prompt."""

from __future__ import annotations

from pathlib import Path

from .app import mcp

# The agent skill ships inside this package, so connecting the server is the whole
# install: a client that never adds the web-api-skills repo still gets the judgment for
# using these tools well. `scripts/sync-skill.sh` refreshes it from its canonical home.
SKILL_PATH = Path(__file__).parent / "skills" / "oxylabs-web-api.md"
_skill_cache: str | None = None


def _skill_text() -> str:
    """The bundled skill, without its frontmatter — that is for a skill loader, not here."""
    global _skill_cache
    if _skill_cache is None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3 :].lstrip()
        _skill_cache = text
    return _skill_cache


@mcp.resource(
    "oxylabs://skill/web-api",
    name="oxylabs_web_api_skill",
    title="Using the Oxylabs Web API well",
    description=(
        "How to use these tools: search to find and scrape to read, when JavaScript "
        "rendering is worth its cost, what to do with an empty page, and the citation "
        "rules that keep an answer honest. Read it before a research task."
    ),
    mime_type="text/markdown",
)
def skill_resource() -> str:
    return _skill_text()


@mcp.prompt(
    name="web_research",
    title="Research a question from live sources",
    description="Load the Web API skill and start a cited research loop on a question.",
)
def web_research_prompt(question: str = "") -> str:
    """Hand the agent the skill plus the task, so the method arrives with the request."""
    task = (
        f"Research this question and answer it with citations:\n\n{question}"
        if question.strip()
        else "Ask me what to research, then follow the method below."
    )
    return f"{task}\n\n---\n\n{_skill_text()}"
