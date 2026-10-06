"""One https address in a goal is a page read. A click is a separate granted action."""

import re

_URL = re.compile(r"https://[^\s<>'\"]+")


def page_url(goal):
    if not isinstance(goal, str):
        return None
    found = [item.rstrip(".,)") for item in _URL.findall(goal)]
    if len(found) != 1:
        return None
    return found[0]


async def run_page(job, url):
    await job.progress("Reading the page", phase="observing")
    reading = await job.action("browser_read", {"url": url}, "Read the page")
    title = reading.get("title") or ""
    excerpt = reading.get("excerpt") or ""
    return {"outcome": "completed",
            "summary": title or "The page reading is in the record.",
            "evidence": excerpt or title,
            "verification": "fresh page reading"}
