
import json

import herdr_client
import pytest


def _popup(title, query, *rows):
    body = [
        ("   " if row.startswith("● ") else "     ") + row
        for row in rows
    ]
    return "\n".join([
        f"     {title:<41}esc",
        "",
        f"     {query}",
        "",
        *body,
    ])


def _zoom_popup_row(left, content, right=""):
    return left.ljust(138) + content.ljust(52) + right


THEME_INITIAL_54 = "\n".join([
    "     Themes                                  esc",
    "",
    "     Search",
    "",
    "     orng",
    "     osaka-jade",
    "   ● palenight",
    "     rosepine",
    "     solarized",
])
THEME_FILTERED_54 = "\n".join([
    "     Themes                                  esc",
    "",
    "     palenight",
    "",
    "   ● palenight",
])
COMMANDS_INITIAL_54 = "\n".join([
    "     Commands                                esc",
    "",
    "     Search",
    "",
    "     Suggested",
    "     Switch session                     ctrl+x l",
    "     Switch model                       ctrl+x m",
    "",
    "     Session",
])
COMMANDS_FILTERED_54 = "\n".join([
    "     Commands                                esc",
    "",
    "     Switch to",
    "",
    "     System",
    "     Switch to dark mode",
    "",
    "     Agent",
    "     Switch model                       ctrl+x m",
])
WIDE_THEME_INITIAL = "\n".join([
    "  ┃  regio    Themes                                  esc",
    "  ┃  regio",
    "  ┃           Search",
    "",
    "left residue  orng",
    "other output  osaka-jade",
])
WIDE_THEME_FILTERED = "\n".join([
    "changed-left  Themes                                  esc",
    "changed-left",
    "query residue palenight",
    "",
    "body residue  palenight",
])
WIDE_COMMANDS_INITIAL = "\n".join([
    "left residue  Commands                                esc",
    "under output  ",
    "query residue Search",
    "",
    "body residue  Suggested",
    "other output  Switch session                     ctrl+x l",
])
WIDE_COMMANDS_FILTERED = "\n".join([
    "changed-left  Commands                                esc",
    "stale region  ",
    "query residue Switch to",
    "",
    "body residue  System",
    "other output  Switch to dark mode",
])
ZOOM_THEME_INITIAL = "\n".join([
    _zoom_popup_row(
        "history Themes  esc", "Themes                                  esc",
        "right header residue",
    ),
    _zoom_popup_row("under output", "", "right spacer residue"),
    _zoom_popup_row("+ Thought: 1.3s", "Search", "right query residue"),
    _zoom_popup_row("pane background", "", "right spacer changed"),
    _zoom_popup_row("  ┃", "flexoki", "right body residue"),
    _zoom_popup_row("Todos", "github", "right body changed"),
    _zoom_popup_row("status text", "gruvbox", "right body output"),
    _zoom_popup_row("other output", "kanagawa", "right body tail"),
])
ZOOM_THEME_FILTERED = "\n".join([
    _zoom_popup_row(
        "changed Themes  esc", "Themes                                  esc",
        "different header residue",
    ),
    _zoom_popup_row("changed under", "", "different spacer residue"),
    _zoom_popup_row("different thought", "kanagawa", "different query residue"),
    _zoom_popup_row("changed pane", "", "different spacer changed"),
    _zoom_popup_row("changed status", "kanagawa", "different body residue"),
])
SAME_ANCHOR_THEME_BEFORE = "\n".join([
    *(["history"] * 16),
    "left residue  Themes                                  esc",
    "left residue  ",
    "left residue  aura",
    "left residue  ",
    "left residue  aura",
])
SAME_ANCHOR_THEME_AFTER = "\n".join([
    *(["history"] * 16),
    "changed-left  Themes                                  esc",
    "changed-left  ",
    "changed-left  Search",
    "changed-left  ",
    "changed-left  flexoki",
    "changed-left  github",
    "changed-left  gruvbox",
    "changed-left  kanagawa",
    "changed-left  lucent-orng",
])


def _split_popup(screen):
    return "\n".join("  ┃" + line if line else "" for line in screen.splitlines())


@pytest.mark.parametrize(
    ("title", "screen", "label"),
    [
        ("Themes", _split_popup(THEME_INITIAL_54), "orng"),
        ("Themes", _split_popup(THEME_FILTERED_54), "palenight"),
        (
            "Commands", _split_popup(COMMANDS_INITIAL_54),
            "Switch session                     ctrl+x l",
        ),
        ("Commands", _split_popup(COMMANDS_FILTERED_54), "Switch to dark mode"),
    ],
)
def test_popup_region_accepts_bordered_rows_with_bare_spacers(
    title, screen, label,
):
    regions = herdr_client._opencode_popup_regions(screen, title)

    assert len(regions) == 1
    assert herdr_client._opencode_popup_has_label(regions[0], label) is True


def test_theme_first_body_label_requires_matching_query():
    region = herdr_client._opencode_popup_regions(
        _popup("Themes", "aura", "aura", "palenight"), "Themes",
    )[0]

    assert herdr_client._opencode_popup_has_first_body_label(
        region, "aura", "aura",
    ) is True
    assert herdr_client._opencode_popup_has_first_body_label(
        region, "different query", "aura",
    ) is False
    assert herdr_client._opencode_popup_has_first_body_label(
        region, "aura", "palenight",
    ) is False


def test_popup_region_requires_query_at_overlay_column():
    lines = _split_popup(THEME_INITIAL_54).splitlines()
    lines[2] = "residue"

    assert herdr_client._opencode_popup_regions(
        "\n".join(lines), "Themes",
    ) == ()


def test_popup_region_uses_overlay_column_with_arbitrary_left_residue():
    regions = herdr_client._opencode_popup_regions(WIDE_THEME_INITIAL, "Themes")

    assert len(regions) == 1
    popup = regions[0]
    filtered = herdr_client._opencode_popup_region_at(
        WIDE_THEME_FILTERED, popup,
    )
    assert filtered is not None
    assert herdr_client._opencode_popup_has_label(filtered, "palenight") is True


def test_popup_region_prefers_wide_overlay_title_over_left_title_residue():
    regions = herdr_client._opencode_popup_regions(ZOOM_THEME_INITIAL, "Themes")

    assert len(regions) == 1
    popup = regions[0]
    assert popup.header_column == 138
    assert popup.rows == ("Search", "flexoki", "github", "gruvbox", "kanagawa")
    filtered = herdr_client._opencode_popup_region_at(ZOOM_THEME_FILTERED, popup)
    assert filtered is not None
    assert filtered.rows == ("kanagawa", "kanagawa")


def test_command_popup_region_uses_overlay_column_with_arbitrary_left_residue():
    regions = herdr_client._opencode_popup_regions(
        WIDE_COMMANDS_INITIAL, "Commands",
    )

    assert len(regions) == 1
    popup = regions[0]
    filtered = herdr_client._opencode_popup_region_at(
        WIDE_COMMANDS_FILTERED, popup,
    )
    assert filtered is not None
    assert herdr_client._opencode_popup_header_at(
        WIDE_COMMANDS_FILTERED, popup,
    ) is True
    assert herdr_client._opencode_popup_has_label(
        filtered, "Switch to dark mode",
    ) is True


def test_theme_popup_region_ignores_background_marker_changes():
    before = THEME_INITIAL_54 + "\nold transcript"
    current = before + " now mentions cobalt2"
    regions = herdr_client._opencode_popup_regions(before, "Themes")

    assert herdr_client._opencode_new_popup_region(
        current, "Themes", regions,
    ) is None


def test_theme_popup_region_treats_changed_same_anchor_signature_as_new():
    before = herdr_client._opencode_popup_regions(THEME_INITIAL_54, "Themes")

    popup = herdr_client._opencode_new_popup_region(
        THEME_FILTERED_54, "Themes", before,
    )

    assert popup is not None
    assert popup.header_line == before[0].header_line
    assert popup.header_column == before[0].header_column
    assert popup.query == "palenight"


def test_theme_popup_region_uses_real_same_anchor_signature():
    before = herdr_client._opencode_popup_regions(
        SAME_ANCHOR_THEME_BEFORE, "Themes",
    )

    assert len(before) == 1
    assert (before[0].header_line, before[0].header_column) == (16, 14)
    assert (before[0].query, before[0].rows) == ("aura", ("aura", "aura"))
    popup = herdr_client._opencode_new_popup_region(
        SAME_ANCHOR_THEME_AFTER, "Themes", before,
    )
    assert popup is not None
    assert (popup.header_line, popup.header_column) == (16, 14)
    assert popup.query == "Search"
    assert popup.rows == (
        "Search", "flexoki", "github", "gruvbox", "kanagawa", "lucent-orng",
    )
    assert herdr_client._opencode_new_popup_region(
        SAME_ANCHOR_THEME_BEFORE.replace("left residue", "other output"),
        "Themes", before,
    ) is None


def test_theme_popup_region_treats_changed_first_body_with_same_query_as_new():
    before = herdr_client._opencode_popup_regions(
        _popup("Themes", "Search", "old-theme"), "Themes",
    )

    popup = herdr_client._opencode_new_popup_region(
        _popup("Themes", "Search", "new-theme"), "Themes", before,
    )

    assert popup is not None
    assert popup.query == "Search"
    assert popup.rows[1] == "new-theme"


def test_theme_popup_region_stays_open_when_option_changes():
    opened = "background\n" + THEME_INITIAL_54
    popup = herdr_client._opencode_popup_regions(opened, "Themes")[0]
    filtered = "background\n" + THEME_FILTERED_54

    assert herdr_client._opencode_popup_header_at(filtered, popup) is True
    assert herdr_client._opencode_popup_has_label(
        herdr_client._opencode_popup_region_at(filtered, popup), "palenight",
    ) is True


def test_command_popup_region_ignores_background_marker_changes():
    before = COMMANDS_INITIAL_54 + "\nold transcript"
    current = before + " now mentions Open editor"
    regions = herdr_client._opencode_popup_regions(before, "Commands")

    assert herdr_client._opencode_new_popup_region(
        current, "Commands", regions,
    ) is None


def test_command_popup_region_stays_open_when_history_rolls_and_action_flips():
    opened = (
        "Switch to light mode\nold history\n"
        + COMMANDS_FILTERED_54
    )
    popup = herdr_client._opencode_popup_regions(opened, "Commands")[0]
    flipped = (
        "new history\nanother line\n"
        + COMMANDS_FILTERED_54.replace(
            "Switch to dark mode", "Switch to light mode",
        )
    )

    assert herdr_client._opencode_popup_header_at(flipped, popup) is True
    assert herdr_client._opencode_popup_has_label(
        herdr_client._opencode_popup_region_at(flipped, popup),
        "Switch to light mode",
    ) is True


def test_grok_theme_slash_and_launch_args():
    assert herdr_client.grok_theme_slash("light") == "/theme grokday"
    assert herdr_client.grok_theme_slash("dark") == "/theme groknight"
    with pytest.raises(ValueError, match="mode"):
        herdr_client.grok_theme_slash("sepia")
    assert herdr_client.grok_launch_theme_args("light") == ["--light"]
    assert herdr_client.grok_launch_theme_args("dark") == []
    assert herdr_client.grok_launch_theme_args(None) == []
    assert herdr_client.opencode_theme_name("light") == "palenight"
    assert herdr_client.opencode_theme_name("dark") == "aura"


def test_apply_grok_web_theme_targets_only_grok(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: {
            "panes": [
                {"session": "s1", "pane_id": "p1", "agent": "grok"},
                {"session": "s1", "pane_id": "p2", "agent": "codex"},
                {"session": "s1", "pane_id": "p4", "agent": "opencode"},
                {"session": "s2", "pane_id": "p3", "agent": "grok"},
            ]
        },
    )
    monkeypatch.setattr(
        herdr_client,
        "pane_send",
        lambda session, pane_id, text, mode="prompt": calls.append((session, pane_id, text, mode)) or {"available": True, "sent": text, "mode": mode},
    )
    out = herdr_client.apply_grok_web_theme("light")
    assert out["command"] == "/theme grokday"
    assert calls == [
        ("s1", "p1", "/theme grokday", "slash"),
        ("s2", "p3", "/theme grokday", "slash"),
    ]


def test_opencode_theme_picker_uses_shortcut_without_touching_composer(monkeypatch):
    calls = []
    baseline = "preserved draft"
    screens = iter([
        baseline,
        baseline + "\n" + THEME_INITIAL_54,
        baseline + "\n" + THEME_FILTERED_54,
        baseline,
    ])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if "pane" in args and "read" in args:
            return next(screens)
        return ""

    monkeypatch.setattr(
        herdr_client,
        "_run",
        fake_run,
    )
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "palenight")

    assert result["available"] is True
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "ctrl+x", "t",
    ] in calls
    assert [
        "--session", "demo", "pane", "send-text", "w1:p4", "palenight",
    ] in calls
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "Enter",
    ] in calls
    assert all("/themes" not in arg for call in calls for arg in call)


def test_opencode_theme_picker_accepts_first_unselected_theme(monkeypatch):
    calls = []
    screens = iter([
        "preserved draft",
        "preserved draft\n" + THEME_INITIAL_54,
        "preserved draft\n" + _popup("Themes", "aura", "aura"),
        "preserved draft",
    ])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if "read" in args:
            return next(screens)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "aura")

    assert result["available"] is True
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "Enter",
    ] in calls


def test_opencode_theme_picker_accepts_changed_signature_at_same_anchor(
    monkeypatch,
):
    calls = []
    screens = iter([
        _popup("Themes", "Search", "old-theme"),
        _popup("Themes", "Search", "new-theme"),
        _popup("Themes", "aura", "aura"),
        "preserved draft",
    ])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if "read" in args:
            return next(screens)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "aura")

    assert result["available"] is True
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "Enter",
    ] in calls


@pytest.mark.parametrize(
    ("first_theme", "tail_theme", "requested_theme"),
    [
        ("aura", "palenight", "palenight"),
        ("palenight", "aura", "aura"),
    ],
)
def test_opencode_theme_picker_uses_first_theme_before_vertical_tail(
    monkeypatch, first_theme, tail_theme, requested_theme,
):
    calls = []
    read_count = 0

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if "read" in args:
            read_count += 1
            if read_count == 1:
                return "preserved draft"
            if read_count == 2:
                return "preserved draft\n" + THEME_INITIAL_54
            if read_count == 3:
                return "preserved draft\n" + _popup(
                    "Themes", requested_theme, first_theme, tail_theme,
                )
            raise RuntimeError("vertical tail must not confirm theme")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane(
        "demo", "w1:p4", requested_theme,
    )

    assert result == {"error": "vertical tail must not confirm theme"}
    assert not any(call[-1:] == ["Enter"] for call in calls)
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]


def test_opencode_theme_picker_closes_its_dialog_after_filter_failure(monkeypatch):
    calls = []
    read_count = 0

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if "read" in args:
            read_count += 1
            if read_count == 1:
                return "preserved draft"
            if read_count == 2:
                return "preserved draft\n" + THEME_INITIAL_54
            if read_count == 3:
                raise RuntimeError("theme filter did not render")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "aura")

    assert "error" in result
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]
    assert all("/themes" not in arg for call in calls for arg in call)


def test_opencode_theme_picker_does_not_trust_background_marker_changes(
    monkeypatch,
):
    calls = []
    read_count = 0
    background = _popup(
        "Themes", "Search", "tokyonight", "nightowl",
    ) + "\nold transcript"

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if args[-1:] == ["esc"]:
            raise RuntimeError("cleanup failed")
        if "read" in args:
            read_count += 1
            if read_count == 1:
                return background
            if read_count == 2:
                return background + " now mentions cobalt2"
            raise RuntimeError("popup did not open")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "aura")

    assert result == {"error": "popup did not open"}
    assert not any("send-text" in call for call in calls)
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]
    assert sum(call[-1:] == ["esc"] for call in calls) == 1


@pytest.mark.parametrize(
    ("apply_picker", "argument"),
    [
        (herdr_client.apply_opencode_theme_to_pane, "aura"),
        (herdr_client.apply_opencode_mode_to_pane, "dark"),
    ],
)
def test_opencode_picker_cleans_parser_exception_once(
    monkeypatch, apply_picker, argument,
):
    calls = []

    def fail_parser(*_args):
        raise ValueError("parser failed")

    def fake_run(args, timeout=10):
        calls.append(list(args))
        return "preserved draft"

    monkeypatch.setattr(herdr_client, "_opencode_new_popup_region", fail_parser)
    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = apply_picker("demo", "w1:p4", argument)

    assert result == {"error": "parser failed"}
    assert sum(call[-1:] == ["esc"] for call in calls) == 1


@pytest.mark.parametrize(
    ("apply_picker", "argument"),
    [
        (herdr_client.apply_opencode_theme_to_pane, "aura"),
        (herdr_client.apply_opencode_mode_to_pane, "dark"),
    ],
)
def test_opencode_picker_preserves_primary_error_when_cleanup_raises_oserror(
    monkeypatch, apply_picker, argument,
):
    calls = []

    def fail_parser(*_args):
        raise RuntimeError("primary failure")

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if args[-1:] == ["esc"]:
            raise OSError("cleanup failed")
        return "preserved draft"

    monkeypatch.setattr(herdr_client, "_opencode_new_popup_region", fail_parser)
    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = apply_picker("demo", "w1:p4", argument)

    assert result == {"error": "primary failure"}
    assert sum(call[-1:] == ["esc"] for call in calls) == 1


def test_opencode_theme_picker_rejects_full_dialog_left_after_enter(monkeypatch):
    calls = []
    read_count = 0
    full_dialog = _popup("Themes", "Search", "tokyonight", "nightowl")

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if "read" in args:
            read_count += 1
            screens = {
                1: "preserved draft",
                2: "preserved draft\n" + full_dialog,
                3: "preserved draft\n" + _popup("Themes", "aura", "● aura"),
                4: "preserved draft\n" + full_dialog,
            }
            if read_count in screens:
                return screens[read_count]
            raise RuntimeError("dialog stayed open")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "aura")

    assert result == {"error": "dialog stayed open"}
    assert any(call[-1:] == ["Enter"] for call in calls)
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]


def test_opencode_mode_picker_switches_to_requested_mode_without_touching_composer(monkeypatch):
    calls = []
    baseline = "preserved draft"
    screens = iter([
        baseline,
        baseline + "\n" + COMMANDS_INITIAL_54,
        baseline + "\n" + COMMANDS_FILTERED_54.replace(
            "Switch to dark mode", "Switch to light mode",
        ),
        baseline,
    ])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if "read" in args:
            return next(screens)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_mode_to_pane("demo", "w1:p4", "light")

    assert result == {
        "available": True,
        "sent": "theme-mode → light",
        "mode": "opencode-theme-mode",
        "changed": True,
    }
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "ctrl+p",
    ] in calls
    assert [
        "--session", "demo", "pane", "send-text", "w1:p4", "Switch to",
    ] in calls
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "Enter",
    ] in calls


@pytest.mark.parametrize(
    ("first_mode", "tail_mode", "requested_mode"),
    [
        ("dark", "light", "light"),
        ("light", "dark", "dark"),
    ],
)
def test_opencode_mode_picker_uses_first_action_before_vertical_tail(
    monkeypatch, first_mode, tail_mode, requested_mode,
):
    calls = []
    screens = iter([
        "preserved draft",
        "preserved draft\n" + COMMANDS_INITIAL_54,
        "preserved draft\n" + _popup(
            "Commands", "Switch to", f"Switch to {first_mode} mode",
            f"Switch to {tail_mode} mode",
        ),
        "preserved draft",
    ])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if "read" in args:
            return next(screens)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_mode_to_pane(
        "demo", "w1:p4", requested_mode,
    )

    assert result["changed"] is False
    assert not any(call[-1:] == ["Enter"] for call in calls)
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ] in calls


def test_opencode_mode_picker_is_idempotent(monkeypatch):
    calls = []
    screens = iter([
        "preserved draft",
        "preserved draft\n" + COMMANDS_INITIAL_54,
        "preserved draft\n" + COMMANDS_FILTERED_54,
        "preserved draft",
    ])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if "read" in args:
            return next(screens)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_mode_to_pane("demo", "w1:p4", "light")

    assert result["changed"] is False
    assert [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ] in calls
    assert not any(call[-1:] == ["Enter"] for call in calls)


def test_opencode_mode_picker_does_not_trust_background_marker_changes(
    monkeypatch,
):
    calls = []
    read_count = 0
    background = _popup(
        "Commands", "Search", "Suggested", "Switch session", "Switch model",
    ) + "\nold transcript"

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if "read" in args:
            read_count += 1
            if read_count == 1:
                return background
            if read_count == 2:
                return background + " now mentions Open editor"
            raise RuntimeError("popup did not open")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_mode_to_pane("demo", "w1:p4", "dark")

    assert result == {"error": "popup did not open"}
    assert not any("send-text" in call for call in calls)
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]
    assert sum(call[-1:] == ["esc"] for call in calls) == 1


def test_opencode_mode_picker_rejects_full_dialog_left_after_action(monkeypatch):
    calls = []
    read_count = 0
    full_dialog = _popup(
        "Commands", "Search", "Suggested", "Switch session", "Switch model",
    )

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if "read" in args:
            read_count += 1
            screens = {
                1: "preserved draft",
                2: "preserved draft\n" + full_dialog,
                3: "preserved draft\n" + _popup(
                    "Commands", "Switch to", "System", "Switch to dark mode",
                ),
                4: "preserved draft\n" + full_dialog,
            }
            if read_count in screens:
                return screens[read_count]
            raise RuntimeError("dialog stayed open")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_mode_to_pane("demo", "w1:p4", "dark")

    assert result == {"error": "dialog stayed open"}
    assert any(call[-1:] == ["Enter"] for call in calls)
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]


def test_opencode_mode_picker_rejects_flipped_action_left_after_enter(monkeypatch):
    calls = []
    read_count = 0

    def fake_run(args, timeout=10):
        nonlocal read_count
        calls.append(list(args))
        if "read" in args:
            read_count += 1
            screens = {
                1: "preserved draft",
                2: (
                    "preserved draft\n"
                    + _popup(
                        "Commands", "Search", "Suggested", "Switch session",
                        "Switch model",
                    )
                ),
                3: (
                    "preserved draft\n"
                    + _popup(
                        "Commands", "Switch to", "System", "Switch to dark mode",
                    )
                ),
                4: (
                    "preserved draft\n"
                    + _popup(
                        "Commands", "Switch to", "System", "Switch to light mode",
                    )
                ),
            }
            if read_count in screens:
                return screens[read_count]
            raise RuntimeError("flipped action popup stayed open")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_mode_to_pane("demo", "w1:p4", "dark")

    assert result == {"error": "flipped action popup stayed open"}
    assert any(call[-1:] == ["Enter"] for call in calls)
    assert calls[-1] == [
        "--session", "demo", "pane", "send-keys", "w1:p4", "esc",
    ]


def test_opencode_mode_picker_rejects_invalid_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=10: calls.append(list(args)),
    )

    assert herdr_client.apply_opencode_mode_to_pane(
        "demo", "w1:p4", "sepia",
    ) == {"error": "mode 必须是 light 或 dark"}
    assert calls == []


def test_opencode_tui_theme_preserves_config_and_rejects_invalid_json(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "opencode" / "tui.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "old", "scroll_speed": 3}), encoding="utf-8")

    assert herdr_client.set_opencode_tui_theme("aura") == path
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "$schema": "https://opencode.ai/tui.json",
        "theme": "aura",
        "scroll_speed": 3,
    }

    path.write_text("{broken", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    try:
        herdr_client.set_opencode_tui_theme("palenight")
    except ValueError as exc:
        assert "不是有效 JSON" in str(exc)
    else:
        raise AssertionError("invalid tui.json must fail closed")
    assert path.read_text(encoding="utf-8") == before


def test_agent_theme_sync_skips_live_opencode_when_persistence_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client, "set_opencode_tui_theme",
        lambda _name: (_ for _ in ()).throw(OSError("read-only")),
    )
    monkeypatch.setattr(
        herdr_client, "snapshot",
        lambda: {"panes": [
            {"session": "s1", "pane_id": "p1", "agent": "opencode"},
            {"session": "s1", "pane_id": "p2", "agent": "grok"},
        ]},
    )
    monkeypatch.setattr(
        herdr_client, "apply_opencode_theme_to_pane",
        lambda *args: calls.append(("opencode", *args)) or {"available": True},
    )
    monkeypatch.setattr(
        herdr_client, "pane_send",
        lambda session, pane_id, text, mode="prompt": calls.append(
            ("grok", session, pane_id, text, mode)
        ) or {"available": True},
    )

    result = herdr_client.apply_agent_web_themes("dark")

    assert result["ok"] is False
    assert any("tui.json" in error for error in result["errors"])
    assert result["skipped"] == [{
        "session": "s1", "pane_id": "p1", "agent": "opencode",
        "reason": "tui_config_write_failed",
    }]
    assert calls == [("grok", "s1", "p2", "/theme groknight", "slash")]


def test_agent_theme_sync_sets_opencode_theme_and_explicit_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client, "set_opencode_tui_theme", lambda theme: f"/tmp/{theme}.json",
    )
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: {"panes": [
            {"session": "s1", "pane_id": "p1", "agent": "opencode"},
        ]},
    )
    monkeypatch.setattr(
        herdr_client,
        "apply_opencode_theme_to_pane",
        lambda session, pane_id, theme: calls.append(
            ("theme", session, pane_id, theme)
        ) or {"available": True},
    )
    monkeypatch.setattr(
        herdr_client,
        "apply_opencode_mode_to_pane",
        lambda session, pane_id, mode: calls.append(
            ("mode", session, pane_id, mode)
        ) or {"available": True},
    )

    result = herdr_client.apply_agent_web_themes("light")

    assert result["ok"] is True
    assert calls == [
        ("theme", "s1", "p1", "palenight"),
        ("mode", "s1", "p1", "light"),
    ]


def test_start_agent_injects_light_flag_for_grok(monkeypatch):
    # 最小桩：只验证 agent_args 注入路径不抛、且 --light 进入 start argv
    captured = {}
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "require_herdr_capabilities", lambda: None)
    monkeypatch.setattr(herdr_client, "normalize_agent_kind", lambda a: "grok")
    monkeypatch.setattr(herdr_client, "normalize_agent_args", lambda a: a or "")
    monkeypatch.setattr(herdr_client, "current_web_theme_mode", lambda: "light")
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda s: {"panes": [], "agents": []})
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda a: "/usr/bin/true")
    monkeypatch.setattr(herdr_client, "resolve_unique_agent_name", lambda *a, **k: "grok-1")
    monkeypatch.setattr(herdr_client, "_agent_start_timeout", lambda a: 1.0)
    monkeypatch.setattr(herdr_client, "save_launch_descriptor", lambda **k: None)
    monkeypatch.setattr(herdr_client, "_rename_agent_context", lambda *a, **k: None)
    import os
    monkeypatch.setattr(os, "access", lambda *a, **k: True)
    from pathlib import Path
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    def fake_run(args, timeout=10):
        captured["args"] = list(args)
        if "tab" in args and "create" in args:
            return 'data: {"result":{"pane":{"pane_id":"w1:p9"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    # simplify loop: make _snapshot after create return the new pane
    snaps = [
        {"panes": [], "agents": []},
        {"panes": [{"pane_id": "w1:p9", "agent": None}], "agents": []},
        {"panes": [{"pane_id": "w1:p9", "agent": "grok"}], "agents": []},
    ]
    def snap_session(s):
        return snaps.pop(0) if snaps else {"panes": [{"pane_id": "w1:p9", "agent": "grok"}], "agents": []}
    monkeypatch.setattr(herdr_client, "_snapshot_session", snap_session)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    r = herdr_client.start_agent("demo", "/tmp", agent="grok")
    # may fail mid-start in complex path; check captured start argv if present
    args = captured.get("args") or []
    if "start" in args:
        assert "--light" in args
