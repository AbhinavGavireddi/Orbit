import httpx
import pytest
from orbit_common.routing import CAPABILITIES

from orbit_common.config import Settings
from orbit_automation.handler import AutomationHandler
from orbit_research.report import Report, Source, LocalArtifacts, render_pdf, from_response


class DeviceJob:
    def __init__(self, task):
        self.task, self.actions, self.messages = task, [], []

    async def action(self, kind, params=None, summary=None):
        self.actions.append((kind, params))
        if kind == "screenshot":
            return {"image_base64": "cG5n", "width": 100, "height": 100}
        if kind == "open_app":
            return {"app_bundle_id": params["bundle_id"], "ok": True}
        if kind == "open_url":
            return {"app_bundle_id": "com.google.Chrome", "ok": True}
        return {}

    async def progress(self, text, result=None):
        self.messages.append(text)


async def test_computer_call_completed_does_not_mean_desktop_task_completed():
    replies = [
        {"id": "r1", "status": "completed", "output": [{"type": "computer_call", "call_id": "c1",
          "status": "completed", "actions": [{"type": "click", "x": 5, "y": 5, "button": "left"}]}]},
        {"id": "r2", "status": "completed", "output": [{"type": "function_call", "name": "finish_task",
            "arguments": '{"success":true,"summary":"Task finished.","evidence":"Screenshot shows the requested document."}'}]},
    ]
    async def provider(request):
        return httpx.Response(200, json=replies.pop(0))
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    handler = AutomationHandler(Settings(_env_file=None, openai_api_key="test"), client)
    job = DeviceJob({"kind": "automation", "goal": "open a document"})
    result = await handler.run(job)
    assert [a[0] for a in job.actions] == ["ax_snapshot", "screenshot", "computer", "screenshot"]
    assert result["summary"].startswith("Task finished")
    await client.aclose()


@pytest.mark.parametrize("capability", ["open_notes", "open_chrome", "open_spotify"])
async def test_fast_capabilities_use_native_ack_without_visual_model(capability):
    async def unexpected_provider(request):
        pytest.fail("Typed navigation must not call the visual planner")
    spec = CAPABILITIES[capability]
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_provider)) as client:
        handler = AutomationHandler(Settings(_env_file=None), client)
        job = DeviceJob({"kind": spec.kind, "goal": spec.goal, "capability": capability})
        result = await handler.run(job)
    assert [a[0] for a in job.actions] == (["open_app", "open_url"] if spec.url else ["open_app"])
    assert result["verification"] == "native_application_acknowledgement"
    assert result["capability"] == capability


async def test_general_computer_goal_uses_the_visual_agent():
    replies = [
        {"id": "r1", "status": "completed", "output": [{"type": "function_call", "name": "finish_task",
            "arguments": '{"success":true,"summary":"Calendar is open.","evidence":"Calendar is the front window."}'}]},
    ]
    async def provider(request):
        return httpx.Response(200, json=replies.pop(0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        handler = AutomationHandler(Settings(_env_file=None, openai_api_key="test"), client)
        job = DeviceJob({"kind": "automation", "goal": "Open Calendar", "capability": "computer"})
        result = await handler.run(job)
    assert [action[0] for action in job.actions[:2]] == ["ax_snapshot", "screenshot"]
    assert result["summary"] == "Calendar is open."


async def test_fast_navigation_does_not_claim_success_without_matching_native_ack():
    job = DeviceJob({"kind": "automation", "goal": "Open Notes", "capability": "open_notes"})
    async def wrong_app(*args, **kwargs):
        return {"app_bundle_id": "com.apple.finder", "ok": True}
    job.action = wrong_app
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="acknowledge"):
            await AutomationHandler(Settings(_env_file=None), client).run(job)


def test_report_rejects_unsourced_model_text():
    with pytest.raises(ValueError, match="sources"):
        from_response({"output": [{"type": "message", "content": [{"type": "output_text", "text": "Made up report"}]}]}, "Topic")


def test_report_preserves_citation_links_and_artifact_retry_is_identical(tmp_path):
    response = {"output": [{"type": "web_search_call"}, {"type": "message", "content": [{
        "type": "output_text", "text": "First finding. Second finding.", "annotations": [
            {"type": "url_citation", "start_index": 0, "end_index": 14, "url": "https://example.org/a", "title": "A"},
            {"type": "url_citation", "start_index": 15, "end_index": 30, "url": "https://example.com/b", "title": "B"}]}]}]}
    report = from_response(response, "Topic")
    assert "https://example.org/a" in report.markdown()
    storage = LocalArtifacts(tmp_path)
    first = storage.put("task-a", "report.md", report.markdown().encode())
    assert storage.put("task-a", "report.md", report.markdown().encode()) == first
    with pytest.raises(ValueError):
        storage.put("task-a", "report.md", b"different body")


def test_pdf_has_readable_body_two_to_four_pages_and_link_annotations(tmp_path):
    from pypdf import PdfReader
    report = Report("Orbit research sample", "## Findings\n\n" + "Reliable voice requires careful turn taking. " * 180,
                    [Source("Official reference", "https://example.org/reference")])
    pdf = render_pdf(report)
    assert render_pdf(report) == pdf
    path = tmp_path / "report.pdf"
    path.write_bytes(pdf)
    reader = PdfReader(path)
    assert 2 <= len(reader.pages) <= 4
    assert "Reliable voice" in "".join(p.extract_text() for p in reader.pages)
    assert any(p.get("/Annots") for p in reader.pages)
