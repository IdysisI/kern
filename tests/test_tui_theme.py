"""Theme + chrome regressions from the "Midnight Glass" TUI overhaul.

Three things here were broken *silently* — no exception, no failing test, just
an interface that quietly stopped communicating — so each gets a guard:

1. **Zero-content chrome.**  Textual is border-box: a docked strip with
   ``height: 1`` *and* a border has zero content rows, so ``#topbar`` and
   ``#bar`` painted their border and none of their text. The header, the
   context meter and the keycap hints were invisible.
2. **Hard-coded colours.**  Anything painted with a literal hex cannot be
   re-skinned, so a theme flip would leave a half-dark app behind.
3. **Dead keys behind a modal.**  Textual drops the App from the binding
   chain while a ``ModalScreen`` is on top, so the shortcuts the footer
   advertises did nothing exactly when a dialog was demanding input.
"""
import re

import pytest

from kern import tui as T

pytest.importorskip("textual")


class StubClient:
    """Minimal stand-in for kern.client.Client — enough for the app to mount."""

    async def probe(self, model):
        return None

    async def list_models(self):
        return [{"id": "test-model", "context_length": 200_000}]

    async def stream_chat(self, model, messages, **kw):
        from kern.client import StreamEvent
        yield StreamEvent("text", text="ok")


def _widget_text(widget) -> str:
    """Plain text a widget will paint, measured through rich (no compositor).

    Textual's headless compositor does not produce frames under
    pytest-asyncio, so dumping screen rows comes back blank; rendering the
    widget itself is deterministic and still catches empty/clipped chrome.
    """
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=240, force_terminal=False).print(widget.render())
    return buf.getvalue()


def _app(tmp_path):
    app = T.KernApp(model="test-model", cwd=str(tmp_path))
    app.client = StubClient()
    return app


# ── palette discipline ──────────────────────────────────────────────────────
def test_css_paints_only_theme_variables():
    """A literal hex in CSS is a colour ctrl+t cannot re-skin."""
    hexes = [ln.strip() for ln in T.CSS.splitlines()
             if re.search(r"#[0-9a-fA-F]{3,8}\b", ln)]
    assert not hexes, f"hard-coded colours in CSS: {hexes}"


def test_colours_live_only_in_the_theme_tables():
    """Inline markup must speak in $variables too, not in hex."""
    src = open(T.__file__, encoding="utf-8").read()
    start = src.index("_THEMES = (")
    end = src.index("\n)\n", start)          # closing paren of the theme tuple
    outside = src[:start] + src[end:]
    leaked = re.findall(r"#[0-9a-fA-F]{6}\b", outside)
    assert not leaked, f"colour leaked outside the theme tables: {leaked[:6]}"


def test_both_themes_define_identical_variable_sets():
    dark, light = T._THEMES
    assert (dark.name, light.name) == (T.KERN_DARK, T.KERN_LIGHT)
    assert dark.dark and not light.dark
    assert set(dark.variables) == set(light.variables), "a var missing in one theme paints unpainted"


def test_every_css_variable_is_provided_by_both_themes():
    used = set(re.findall(r"\$([a-z][a-z0-9-]*)", T.CSS))
    builtin = {"primary", "secondary", "success", "warning", "error", "accent",
               "text", "foreground", "background", "surface", "panel", "boost",
               "text-muted", "text-disabled", "primary-muted", "secondary-muted",
               "accent-muted", "success-muted", "warning-muted", "error-muted"}
    for theme in T._THEMES:
        missing = used - set(theme.variables) - builtin
        assert not missing, f"{theme.name} does not define {sorted(missing)}"


# ── the chrome actually paints ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_chrome_strips_have_a_content_row(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    app = _app(tmp_path)
    async with app.run_test(size=(128, 32)) as pilot:
        await pilot.pause(0.2)
        for sel in ("#topbar", "#bar"):
            w = app.query_one(sel)
            assert w.content_region.height >= 1, f"{sel} has no room for its text"
        status = app.query_one("#status")
        status.display = True                     # shown only while a turn runs
        await pilot.pause(0.05)
        assert status.content_region.height >= 1, "#status has no room for its text"


@pytest.mark.asyncio
async def test_header_footer_and_ctx_meter_are_visible(tmp_path, monkeypatch):
    """End-to-end: the chrome carries real content, and has a row to put it in."""
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    app = _app(tmp_path)
    async with app.run_test(size=(128, 32)) as pilot:
        await pilot.pause(0.25)
        left = _widget_text(app.query_one("#tleft"))
        right = _widget_text(app.query_one("#tright"))
        bar = _widget_text(app.query_one("#bar"))
        assert "KERN" in left, f"header brand missing: {left!r}"
        assert "test-model" in left, f"header model missing: {left!r}"
        assert "●" in left, f"header state chip missing: {left!r}"
        assert right.strip(), "header right side is empty"
        assert "send" in bar, f"footer keycaps missing: {bar!r}"
        assert "ctx" in bar, f"footer context meter missing: {bar!r}"
        # ...and the strips are tall enough to show it (border-box: a 1-row
        # widget with a border has zero content rows and paints nothing).
        for sel in ("#topbar", "#bar"):
            w = app.query_one(sel)
            assert w.content_region.height >= 1, f"{sel} cannot show its text"


# ── markdown inside replies ─────────────────────────────────────────────────
def test_inline_code_does_not_paint_a_black_chip():
    """Rich's default is `cyan on black` — a foreign slab inside every card."""
    from rich import themes

    style = themes.DEFAULT.styles["markdown.code"]
    assert style.bgcolor is None, f"inline code paints a background: {style}"


def test_fenced_code_lets_the_card_show_through():
    """Pygments paints its own background (Monokai's #272822) by default."""
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=70, force_terminal=True,
                      color_system="truecolor")
    md = T.KernMarkdown("use `x.y`\n\n```python\na = 1\n```\n",
                        justify="left", code_theme=T.CODE_THEME_DARK)
    painted = {str(seg.style.bgcolor)
               for line in console.render_lines(md)
               for seg in line
               if seg.text.strip() and seg.style and seg.style.bgcolor is not None}
    painted = {b for b in painted if "default" not in b}
    assert not painted, f"code paints a foreign background: {sorted(painted)}"


@pytest.mark.asyncio
async def test_code_palette_follows_the_theme_flip(tmp_path, monkeypatch):
    """Monokai's pale tokens on a light card would be unreadable."""
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    from textual.widgets import Static

    app = _app(tmp_path)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0.2)
        assert app._code_theme() == T.CODE_THEME_DARK
        reply = Static(app._md("# Title\n\n```python\nx = 1\n```\n"),
                       classes="assistant", markup=False)
        app.chat.mount(reply)
        await pilot.pause(0.2)
        assert reply.content.code_theme == T.CODE_THEME_DARK
        await pilot.press("ctrl+t")
        await pilot.pause(0.25)
        assert app._code_theme() == T.CODE_THEME_LIGHT
        # the reply already on screen was repainted, not left in dark tokens
        assert reply.content.code_theme == T.CODE_THEME_LIGHT


# ── theming ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ctrl_t_reskins_the_whole_app(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    app = _app(tmp_path)
    async with app.run_test(size=(128, 32)) as pilot:
        await pilot.pause(0.2)
        assert app.theme == T.KERN_DARK
        dark = app.query_one("#topbar").styles.background
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.theme == T.KERN_LIGHT
        light = app.query_one("#topbar").styles.background
        assert dark != light, "theme flip did not change the palette"
        # and it is a real light theme, not a renamed dark one
        assert light.brightness > dark.brightness
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.theme == T.KERN_DARK


@pytest.mark.asyncio
async def test_theme_slash_command_names_a_theme(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    app = _app(tmp_path)
    async with app.run_test(size=(128, 32)) as pilot:
        await pilot.pause(0.2)
        for ch in "/theme light":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.25)
        assert app.theme == T.KERN_LIGHT


@pytest.mark.asyncio
async def test_app_keys_still_work_behind_a_modal(tmp_path, monkeypatch):
    """Textual excludes the App from a ModalScreen's binding chain."""
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    app = _app(tmp_path)
    async with app.run_test(size=(128, 32)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(T.Approve("edit kern/tui.py", None))
        await pilot.pause(0.2)
        assert isinstance(app.screen, T.Approve)
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        assert app.theme == T.KERN_LIGHT, "ctrl+t died behind the approval dialog"


@pytest.mark.asyncio
async def test_approval_buttons_keep_distinct_semantic_colours(tmp_path, monkeypatch):
    """allow / always / deny must not collapse into one anonymous chip."""
    monkeypatch.setenv("KERN_AUTO_APPROVE", "1")
    app = _app(tmp_path)
    async with app.run_test(size=(128, 32)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(T.Approve("edit kern/tui.py", None))
        await pilot.pause(0.25)
        colours = {b.id: str(b.styles.color) for b in app.screen.query("Button")}
        assert set(colours) == {"y", "a", "n"}
        assert len(set(colours.values())) == 3, f"buttons look alike: {colours}"
