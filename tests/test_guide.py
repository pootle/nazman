from nazman.utils import guide as guide_mod


def test_guide_root_serves_overview(client):
    resp = client.get("/guide")
    assert resp.status_code == 200
    assert "NAZMan User Guide" in resp.text
    assert "User Guide" in resp.text


def test_guide_pages_all_render(client):
    for slug, title in guide_mod.GUIDE_PAGES:
        resp = client.get(f"/guide/{slug}")
        assert resp.status_code == 200, slug
        assert resp.text.startswith("<!DOCTYPE html>")
        assert title.replace("&", "&amp;") in resp.text, slug
        assert "User Guide" in resp.text


def test_guide_active_nav_marked(client):
    resp = client.get("/guide/backup")
    assert resp.status_code == 200
    assert 'class="guide-nav-link active"' in resp.text


def test_guide_prev_next_links_present(client):
    resp = client.get("/guide/backup")
    assert resp.status_code == 200
    assert '/guide/snapshots"' in resp.text
    assert 'Set Up Backups' in resp.text


def test_guide_unknown_page_404(client):
    resp = client.get("/guide/does-not-exist")
    assert resp.status_code == 404


def test_guide_registry_matches_docs_on_disk():
    assert len(guide_mod.GUIDE_PAGES) >= 8
    for slug, _title in guide_mod.GUIDE_PAGES:
        assert (guide_mod.DOCS_DIR / f"{slug}.md").is_file(), slug