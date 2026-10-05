from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_trust_permissions_concurrency_and_failure_visibility():
    collect = (ROOT / ".github" / "workflows" / "collect.yml").read_text()
    tests = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert 'cron: "7,22,37,52 * * * *"' in collect
    assert "workflow_dispatch:" in collect
    assert "group: public-calendar-collection" in collect
    assert "cancel-in-progress: false" in collect
    assert "github.ref == 'refs/heads/main'" in collect
    assert "ref: main" in collect and "ref: data" in collect
    assert "fetch-depth: 0" in collect
    assert "persist-credentials: false" in collect
    assert "always() && steps.capture.outcome != 'skipped'" in collect
    assert "continue-on-error" not in collect
    assert "contents: write" in collect
    assert "contents: write" not in tests
    assert tests.count("branches: [main]") == 2
    assert "GITHUB_TOKEN" not in collect
    assert "upload-artifact" not in collect
    for workflow in (collect, tests):
        for line in workflow.splitlines():
            if "uses:" in line:
                revision = line.split("@", 1)[1].split()[0]
                assert len(revision) == 40
                assert all(character in "0123456789abcdef" for character in revision)
