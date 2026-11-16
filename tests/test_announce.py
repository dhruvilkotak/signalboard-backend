"""pytest tests/test_announce.py — admin announcement email rendering + recipient selection."""

from routers.admin import pick_recipients
from services.email_service import render_announcement


def test_render_sections_bullets_links_and_escaping():
    body = ("🎁 Free 7-day trial — started today\nYou have full access until Nov 23.\n\n"
            "- Manual Trades is free\n- Visit https://example.com/x\n\n"
            "Watch out for <script>")
    html, text = render_announcement("Saloni", "Update <b>", body)
    assert "Hi Saloni," in html and "Hi Saloni," in text
    assert "font-weight:600" in html and "🎁 Free 7-day trial" in html          # section heading
    assert "<li>Manual Trades is free</li>" in html
    assert '<a href="https://example.com/x"' in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "Update &lt;b&gt;" in html
    assert "paper trading only" in html and "paper trading only" in text


def test_render_defaults_name():
    html, text = render_announcement("", "Hello there", "Just a short note for everyone.")
    assert "Hi there," in html


def test_pick_only_selected_approved_users():
    approved = [{"uid": "a", "name": "A", "email": "a@x.com"},
                {"uid": "b", "name": "B", "email": "b@x.com"}]
    assert [u["uid"] for u in pick_recipients(approved, ["b", "zzz", "b"])] == ["b"]
    assert pick_recipients(approved, []) == []


def test_link_does_not_swallow_trailing_punctuation():
    html, _ = render_announcement("A", "Hello there", "Open it (https://app.test/x). Thanks!")
    assert '<a href="https://app.test/x"' in html and "https://app.test/x)" not in html.split('href="')[1].split('"')[0]


def test_signoff_is_not_a_heading():
    html, _ = render_announcement("A", "Hello there", "Some news here.\n\nThanks,\nDhruvil")
    i = html.index("Thanks,")
    assert "font-weight:600" not in html[i - 120:i]
